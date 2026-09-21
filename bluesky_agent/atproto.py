"""Small, idempotent AT Protocol client used by the research runner."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import struct
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

try:
    import regex
except ModuleNotFoundError:  # Packaging installs regex; source checkouts retain a safe fallback.
    regex = None


_GRAPHEME_RE = regex.compile(r"\X") if regex is not None else None

MAX_GRAPHEMES = 300


class AtprotoError(RuntimeError):
    """An AT Protocol request failed or an existing record conflicted."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AtprotoError(f"AT Protocol returned invalid JSON (HTTP {self.status})") from exc
        if not isinstance(value, dict):
            raise AtprotoError(f"AT Protocol returned a non-object JSON value (HTTP {self.status})")
        return value


class Transport(Protocol):
    """Narrow HTTP seam; tests can replace the network without an SDK."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HTTPResponse: ...


class UrllibTransport:
    """Standard-library HTTP transport."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HTTPResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return HTTPResponse(
                    status=response.status,
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            return HTTPResponse(
                status=exc.code,
                body=exc.read(),
                headers=dict(exc.headers.items()) if exc.headers else {},
            )


@dataclass(frozen=True)
class Session:
    did: str
    handle: str
    access_jwt: str
    refresh_jwt: str


@dataclass(frozen=True)
class RecordRef:
    uri: str
    cid: str


@dataclass(frozen=True)
class PostView:
    uri: str
    cid: str
    author_did: str
    record: Mapping[str, Any]
    raw: Mapping[str, Any]


class AtprotoClient:
    """The precise subset of XRPC needed by the autonomous runner."""

    def __init__(
        self,
        *,
        service: str,
        public_api: str,
        identifier: str,
        app_password: str,
        transport: Transport | None = None,
    ) -> None:
        self.service = service.rstrip("/")
        self.public_api = public_api.rstrip("/")
        self.identifier = identifier
        self.app_password = app_password
        self.transport = transport or UrllibTransport()
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        return self.login()

    def login(self) -> Session:
        if self._session is not None:
            return self._session
        payload = self._json_request(
            "POST",
            self.service,
            "com.atproto.server.createSession",
            json_body={"identifier": self.identifier, "password": self.app_password},
        )
        try:
            session = Session(
                did=str(payload["did"]),
                handle=str(payload["handle"]),
                access_jwt=str(payload["accessJwt"]),
                refresh_jwt=str(payload["refreshJwt"]),
            )
        except KeyError as exc:
            raise AtprotoError(f"session response is missing {exc.args[0]}") from exc
        self._session = session
        return session

    def resolve_handle(self, handle: str) -> str:
        payload = self._json_request(
            "GET",
            self.public_api,
            "com.atproto.identity.resolveHandle",
            query={"handle": handle.lstrip("@")},
        )
        did = payload.get("did")
        if not isinstance(did, str) or not did.startswith("did:"):
            raise AtprotoError(f"could not resolve handle {handle!r} to a DID")
        return did

    def author_feed(self, actor: str, *, since: str) -> list[dict[str, Any]]:
        """Return every feed item newer than ``since``, oldest first."""

        cutoff = _parse_timestamp(since)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_posts: set[str] = set()
        items: list[dict[str, Any]] = []
        while True:
            query: dict[str, Any] = {
                "actor": actor,
                "filter": "posts_with_replies",
                "limit": 100,
            }
            if cursor:
                query["cursor"] = cursor
            payload = self._json_request(
                "GET",
                self.public_api,
                "app.bsky.feed.getAuthorFeed",
                query=query,
            )
            page = payload.get("feed")
            if not isinstance(page, list):
                raise AtprotoError("author feed response has no feed list")
            reached_cutoff = False
            for item in page:
                if not isinstance(item, dict):
                    continue
                post = item.get("post")
                if not isinstance(post, dict):
                    continue
                record = post.get("record")
                if not isinstance(record, dict):
                    continue
                indexed_at = post.get("indexedAt")
                if not isinstance(indexed_at, str):
                    record = post.get("record")
                    indexed_at = record.get("createdAt") if isinstance(record, dict) else None
                if not isinstance(indexed_at, str):
                    continue
                if _parse_timestamp(indexed_at) <= cutoff:
                    reached_cutoff = True
                    continue
                uri = post.get("uri")
                if isinstance(uri, str) and uri not in seen_posts:
                    seen_posts.add(uri)
                    items.append(item)
            next_cursor = payload.get("cursor")
            if reached_cutoff or not page or not isinstance(next_cursor, str):
                break
            if next_cursor in seen_cursors:
                raise AtprotoError("author feed returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        items.sort(key=_feed_sort_key)
        return items

    def thread_ancestry(self, uri: str) -> list[PostView]:
        """Return direct parent through root for a post URI."""

        payload = self._json_request(
            "GET",
            self.public_api,
            "app.bsky.feed.getPostThread",
            query={"uri": uri, "depth": 0, "parentHeight": 100},
        )
        thread = payload.get("thread")
        if not isinstance(thread, dict):
            raise AtprotoError("thread response has no thread object")
        result: list[PostView] = []
        parent = thread.get("parent")
        while isinstance(parent, dict):
            post = parent.get("post")
            if isinstance(post, dict):
                view = _post_view(post)
                if view is not None:
                    result.append(view)
            parent = parent.get("parent")
        return result

    def ensure_like(self, subject: RecordRef) -> RecordRef:
        """Create or reconcile the permanent Working Mark for a turn."""

        collection = "app.bsky.feed.like"
        rkey = deterministic_rkey("like", subject.uri)
        existing = self._get_own_record(collection, rkey)
        if existing is not None:
            self._validate_like(existing[1], subject)
            return existing[0]
        record = {
            "$type": collection,
            "subject": {"uri": subject.uri, "cid": subject.cid},
            "createdAt": _now(),
        }
        return self._create_idempotent(
            collection,
            rkey,
            record,
            lambda value: self._validate_like(value, subject),
        )

    def ensure_image_reply(
        self,
        *,
        rkey_seed: str,
        parent: RecordRef,
        root: RecordRef,
        brief: str,
        page_url: str,
        image_path: str | Path,
        alt_text: str,
    ) -> RecordRef:
        """Create or reconcile one deterministic image reply."""

        text, facets = compose_reply(brief, page_url)
        if not alt_text.strip():
            raise ValueError("image alt text must not be empty")
        path = Path(image_path)
        width, height = image_dimensions(path)
        collection = "app.bsky.feed.post"
        rkey = deterministic_rkey("reply", rkey_seed)
        stable = {
            "$type": collection,
            "text": text,
            "facets": facets,
            "reply": {
                "root": {"uri": root.uri, "cid": root.cid},
                "parent": {"uri": parent.uri, "cid": parent.cid},
            },
        }
        existing = self._get_own_record(collection, rkey)
        if existing is not None:
            self._validate_reply(existing[1], stable, alt_text, width, height)
            return existing[0]

        blob = self.upload_blob(path)
        record: dict[str, Any] = {
            **stable,
            "createdAt": _now(),
            "embed": {
                "$type": "app.bsky.embed.images",
                "images": [
                    {
                        "alt": alt_text,
                        "image": blob,
                        "aspectRatio": {"width": width, "height": height},
                    }
                ],
            },
        }
        return self._create_idempotent(
            collection,
            rkey,
            record,
            lambda value: self._validate_reply(value, stable, alt_text, width, height),
        )

    def upload_blob(self, path: str | Path) -> Mapping[str, Any]:
        image = Path(path)
        data = image.read_bytes()
        content_type = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
        payload = self._authorized_json_request(
            "POST",
            "com.atproto.repo.uploadBlob",
            body=data,
            content_type=content_type,
        )
        blob = payload.get("blob")
        if not isinstance(blob, dict):
            raise AtprotoError("blob upload response has no blob object")
        return blob

    def _get_own_record(
        self, collection: str, rkey: str
    ) -> tuple[RecordRef, Mapping[str, Any]] | None:
        try:
            payload = self._authorized_json_request(
                "GET",
                "com.atproto.repo.getRecord",
                query={"repo": self.session.did, "collection": collection, "rkey": rkey},
            )
        except AtprotoError as exc:
            if exc.status in {400, 404} and exc.code in {
                None,
                "RecordNotFound",
                "NotFound",
            }:
                return None
            raise
        value = payload.get("value")
        uri = payload.get("uri")
        cid = payload.get("cid")
        if not isinstance(value, dict) or not isinstance(uri, str) or not isinstance(cid, str):
            raise AtprotoError("record lookup returned an incomplete record")
        return RecordRef(uri=uri, cid=cid), value

    def _create_idempotent(
        self,
        collection: str,
        rkey: str,
        record: Mapping[str, Any],
        validate: Any,
    ) -> RecordRef:
        original_error: Exception | None = None
        try:
            payload = self._authorized_json_request(
                "POST",
                "com.atproto.repo.createRecord",
                json_body={
                    "repo": self.session.did,
                    "collection": collection,
                    "rkey": rkey,
                    "record": record,
                },
            )
            uri = payload.get("uri")
            cid = payload.get("cid")
            if not isinstance(uri, str) or not isinstance(cid, str):
                raise AtprotoError("record creation returned no strong reference")
            return RecordRef(uri=uri, cid=cid)
        except Exception as exc:
            original_error = exc

        try:
            existing = self._get_own_record(collection, rkey)
        except Exception:
            raise original_error
        if existing is None:
            raise original_error
        validate(existing[1])
        return existing[0]

    @staticmethod
    def _validate_like(record: Mapping[str, Any], subject: RecordRef) -> None:
        expected = {"uri": subject.uri, "cid": subject.cid}
        if record.get("$type") != "app.bsky.feed.like" or record.get("subject") != expected:
            raise AtprotoError("deterministic like rkey is occupied by a different record")

    @staticmethod
    def _validate_reply(
        record: Mapping[str, Any],
        stable: Mapping[str, Any],
        alt_text: str,
        width: int,
        height: int,
    ) -> None:
        for key, value in stable.items():
            if record.get(key) != value:
                raise AtprotoError("deterministic reply rkey is occupied by a different record")
        embed = record.get("embed")
        if not isinstance(embed, dict) or embed.get("$type") != "app.bsky.embed.images":
            raise AtprotoError("deterministic reply has no image embed")
        images = embed.get("images")
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
            raise AtprotoError("deterministic reply has an unexpected image embed")
        image = images[0]
        if image.get("alt") != alt_text or image.get("aspectRatio") != {
            "width": width,
            "height": height,
        }:
            raise AtprotoError("deterministic reply image metadata does not match")

    def _authorized_json_request(
        self,
        method: str,
        nsid: str,
        *,
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        session = self.login()
        try:
            return self._json_request(
                method,
                self.service,
                nsid,
                query=query,
                json_body=json_body,
                body=body,
                content_type=content_type,
                token=session.access_jwt,
            )
        except AtprotoError as exc:
            if exc.status != 401:
                raise
        refreshed = self._refresh_session(session.refresh_jwt)
        return self._json_request(
            method,
            self.service,
            nsid,
            query=query,
            json_body=json_body,
            body=body,
            content_type=content_type,
            token=refreshed.access_jwt,
        )

    def _refresh_session(self, refresh_jwt: str) -> Session:
        payload = self._json_request(
            "POST",
            self.service,
            "com.atproto.server.refreshSession",
            token=refresh_jwt,
        )
        try:
            session = Session(
                did=str(payload["did"]),
                handle=str(payload["handle"]),
                access_jwt=str(payload["accessJwt"]),
                refresh_jwt=str(payload["refreshJwt"]),
            )
        except KeyError as exc:
            raise AtprotoError(f"refresh response is missing {exc.args[0]}") from exc
        self._session = session
        return session

    def _json_request(
        self,
        method: str,
        base: str,
        nsid: str,
        *,
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str = "application/json",
        token: str | None = None,
    ) -> dict[str, Any]:
        url = f"{base}/xrpc/{nsid}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            if body is not None:
                raise ValueError("request cannot have both JSON and raw bodies")
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        if body is not None:
            headers["Content-Type"] = content_type
        response = self.transport.request(method, url, headers=headers, body=body)
        payload = response.json()
        if response.status < 200 or response.status >= 300:
            code = payload.get("error") if isinstance(payload.get("error"), str) else None
            message = payload.get("message")
            if not isinstance(message, str):
                message = f"AT Protocol request failed with HTTP {response.status}"
            raise AtprotoError(message, status=response.status, code=code, payload=payload)
        return payload


def deterministic_rkey(kind: str, key: str) -> str:
    """Return an AT-compatible stable record key for one logical side effect."""

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"{kind}-{digest}"


def compose_reply(brief: str, page_url: str) -> tuple[str, list[dict[str, Any]]]:
    """Build reply text and its byte-indexed page link facet."""

    clean_brief = brief.strip()
    clean_url = page_url.strip()
    if not clean_brief:
        raise ValueError("brief reply must not be empty")
    if "\n" in clean_brief or "\r" in clean_brief:
        raise ValueError("brief reply must be exactly one line")
    parsed = urllib.parse.urlparse(clean_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or any(character.isspace() for character in clean_url)
    ):
        raise ValueError("page URL must be an absolute HTTP(S) URL without whitespace")
    text = f"{clean_brief}\n{clean_url}"
    count = grapheme_count(text)
    if count > MAX_GRAPHEMES:
        raise ValueError(f"reply is {count} graphemes; Bluesky permits at most {MAX_GRAPHEMES}")
    byte_start = len(f"{clean_brief}\n".encode("utf-8"))
    byte_end = len(text.encode("utf-8"))
    facets = [
        {
            "$type": "app.bsky.richtext.facet",
            "index": {"byteStart": byte_start, "byteEnd": byte_end},
            "features": [
                {"$type": "app.bsky.richtext.facet#link", "uri": clean_url}
            ],
        }
    ]
    return text, facets


def grapheme_count(text: str) -> int:
    """Count Unicode extended grapheme clusters without allocating a match list."""

    if _GRAPHEME_RE is not None:
        return sum(1 for _ in _GRAPHEME_RE.finditer(text))
    return _fallback_grapheme_count(text)


def image_dimensions(path: str | Path) -> tuple[int, int]:
    """Read PNG, JPEG, GIF, or WebP dimensions without decoding the image."""

    data = Path(path).read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
    elif data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
    elif data.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(data)
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        width, height = _webp_dimensions(data)
    else:
        raise ValueError(f"unsupported image format: {path}")
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid image dimensions: {width}x{height}")
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        if length < 2 or offset + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length < 7:
                break
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return width, height
        offset += length
    raise ValueError("JPEG has no readable size marker")


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise ValueError("WebP has no readable size header")


def _post_view(post: Mapping[str, Any]) -> PostView | None:
    uri = post.get("uri")
    cid = post.get("cid")
    author = post.get("author")
    record = post.get("record")
    did = author.get("did") if isinstance(author, dict) else None
    if not all(isinstance(value, str) for value in (uri, cid, did)) or not isinstance(record, dict):
        return None
    return PostView(uri=uri, cid=cid, author_did=did, record=record, raw=post)


def _feed_sort_key(item: Mapping[str, Any]) -> tuple[datetime, str]:
    post = item.get("post")
    if not isinstance(post, dict):
        return datetime.max.replace(tzinfo=timezone.utc), ""
    indexed_at = post.get("indexedAt")
    if not isinstance(indexed_at, str):
        record = post.get("record")
        indexed_at = record.get("createdAt") if isinstance(record, dict) else None
    timestamp = (
        _parse_timestamp(indexed_at)
        if isinstance(indexed_at, str)
        else datetime.max.replace(tzinfo=timezone.utc)
    )
    uri = post.get("uri") if isinstance(post.get("uri"), str) else ""
    return timestamp, uri


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AtprotoError(f"invalid AT Protocol timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise AtprotoError(f"AT Protocol timestamp has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fallback_grapheme_count(text: str) -> int:
    count = 0
    previous = ""
    regional_run = 0
    hangul_previous = ""
    for character in text:
        codepoint = ord(character)
        hangul = _hangul_class(codepoint)
        regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        extends = (
            unicodedata.category(character) in {"Mn", "Mc", "Me"}
            or 0xFE00 <= codepoint <= 0xFE0F
            or 0xE0100 <= codepoint <= 0xE01EF
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or 0xE0020 <= codepoint <= 0xE007F
            or codepoint == 0x200D
            or previous == "\u200d"
            or (previous == "\r" and character == "\n")
            or _hangul_extends(hangul_previous, hangul)
        )
        if regional:
            extends = regional_run % 2 == 1
            regional_run += 1
        else:
            regional_run = 0
        if not extends:
            count += 1
        previous = character
        hangul_previous = hangul
    return count


def _hangul_class(codepoint: int) -> str:
    if 0x1100 <= codepoint <= 0x115F or 0xA960 <= codepoint <= 0xA97C:
        return "L"
    if 0x1160 <= codepoint <= 0x11A7 or 0xD7B0 <= codepoint <= 0xD7C6:
        return "V"
    if 0x11A8 <= codepoint <= 0x11FF or 0xD7CB <= codepoint <= 0xD7FB:
        return "T"
    if 0xAC00 <= codepoint <= 0xD7A3:
        return "LV" if (codepoint - 0xAC00) % 28 == 0 else "LVT"
    return ""


def _hangul_extends(previous: str, current: str) -> bool:
    return (
        (previous == "L" and current in {"L", "V", "LV", "LVT"})
        or (previous in {"LV", "V"} and current in {"V", "T"})
        or (previous in {"LVT", "T"} and current == "T")
    )


