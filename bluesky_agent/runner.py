"""Eligibility polling and crash-resumable sequential turn processing."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, TYPE_CHECKING

from .atproto import AtprotoClient, PostView, RecordRef
from .config import Config
from .models import Publication, Turn
from .state import Phase, StateError, StateStore, TurnState

if TYPE_CHECKING:
    from .research import ResearchPipeline


class Pipeline(Protocol):
    def run(self, turn: Turn, parent_session: Path | None) -> Publication: ...


@dataclass(frozen=True)
class RunReport:
    discovered: int
    settled: int
    failures: tuple[str, ...]


@dataclass(frozen=True)
class _FeedPost:
    uri: str
    cid: str
    author_did: str
    text: str
    created_at: str
    indexed_at: str
    parent_uri: str | None
    parent_cid: str | None
    root_uri: str
    root_cid: str
    raw: dict[str, Any]

    @property
    def rkey(self) -> str:
        return self.uri.rsplit("/", 1)[-1]


class Runner:
    """One persistent-volume worker; all public side effects are idempotent."""

    def __init__(
        self,
        config: Config,
        *,
        state: StateStore | None = None,
        client: Any | None = None,
        pipeline: Pipeline | None = None,
    ) -> None:
        self.config = config
        self.state = state or StateStore(config.state_dir / "state.sqlite3")
        self.client = client or AtprotoClient(
            service=config.bluesky_service,
            public_api=config.bluesky_public_api,
            identifier=config.agent_handle,
            app_password=config.agent_app_password,
        )
        self._pipeline = pipeline

    @property
    def pipeline(self) -> Pipeline:
        if self._pipeline is None:
            # These imports stay here: research imports shared runtime types and
            # must not make state inspection or CLI startup require OMP/browser.
            from .image import PageScreenshot
            from .research import ResearchPipeline

            screenshot = PageScreenshot(
                self.config.chromium_bin,
                self.config.state_dir / "screenshots",
                readiness_timeout=self.config.wiki_ready_timeout,
                readiness_interval=self.config.wiki_ready_interval,
            )
            self._pipeline = ResearchPipeline(self.config, screenshot)
        return self._pipeline

    def poll(self) -> int:
        """Discover every eligible Operator post since Activation."""

        activation = self.state.require_activation()
        cutoff = _parse_timestamp(activation)
        feed = self.client.author_feed(self.config.operator_did, since=activation)
        posts: list[_FeedPost] = []
        for item in feed:
            post = _feed_post(item)
            if post is None:
                continue
            if post.author_did != self.config.operator_did:
                continue
            if _parse_timestamp(post.indexed_at) <= cutoff:
                continue
            posts.append(post)
        posts.sort(key=lambda post: (_parse_timestamp(post.indexed_at), post.uri))

        accepted = 0
        for post in posts:
            if post.parent_uri is not None:
                continue
            turn = Turn(
                uri=post.uri,
                cid=post.cid,
                rkey=post.rkey,
                text=post.text,
                created_at=post.created_at,
                parent_uri=None,
                root_uri=post.uri,
                parent_turn_uri=None,
                raw=post.raw,
            )
            accepted += int(self.state.accept(turn))

        pending = [post for post in posts if post.parent_uri is not None]
        ancestry_cache: dict[str, list[Any]] = {}
        while pending:
            made_progress = False
            pending_uris = {post.uri for post in pending}
            deferred: list[_FeedPost] = []
            for post in pending:
                if not self.state.root_known(post.root_uri):
                    continue
                resolution = self._nearest_parent_turn(
                    post, pending_uris=pending_uris, ancestry_cache=ancestry_cache
                )
                if resolution is _WAITING:
                    deferred.append(post)
                    continue
                if not isinstance(resolution, str):
                    continue
                turn = Turn(
                    uri=post.uri,
                    cid=post.cid,
                    rkey=post.rkey,
                    text=post.text,
                    created_at=post.created_at,
                    parent_uri=post.parent_uri,
                    root_uri=post.root_uri,
                    parent_turn_uri=resolution,
                    raw=post.raw,
                )
                accepted += int(self.state.accept(turn))
                made_progress = True
            if not deferred or not made_progress:
                break
            pending = deferred
        return accepted

    def run_once(self) -> RunReport:
        """Poll once, then advance every currently eligible turn once."""

        discovered = self.poll()
        counts = self.state.counts()
        remaining = sum(count for phase, count in counts.items() if phase != Phase.SETTLED.value)
        settled = 0
        failures: list[str] = []
        lease = self.config.omp_timeout + self.config.wiki_ready_timeout + 300.0
        for _ in range(remaining):
            claimed = self.state.claim_next(lease_seconds=lease)
            if claimed is None:
                break
            try:
                self._process_claim(claimed)
                settled += 1
            except Exception as exc:
                token = claimed.claim_token
                if token is None:
                    raise StateError(f"claimed turn {claimed.turn.uri} has no token") from exc
                try:
                    self.state.fail_claim(claimed.turn.uri, token, str(exc))
                except StateError:
                    raise
                failures.append(f"{claimed.turn.uri}: {exc}")
        return RunReport(discovered=discovered, settled=settled, failures=tuple(failures))

    def run_forever(self) -> None:
        while True:
            self.run_once()
            time.sleep(self.config.poll_interval)

    def doctor(self) -> dict[str, str]:
        """Check identity bindings and durable state before autonomous work."""

        self.state.check()
        operator_did = self.client.resolve_handle(self.config.operator_handle)
        if operator_did != self.config.operator_did:
            raise RuntimeError(
                f"operator handle resolves to {operator_did}, expected {self.config.operator_did}"
            )
        session = self.client.login()
        agent_did = self.client.resolve_handle(self.config.agent_handle)
        if agent_did != session.did:
            raise RuntimeError(
                f"agent handle resolves to {agent_did}, session belongs to {session.did}"
            )
        return {
            "state": "ok",
            "activation": self.state.activation() or "not activated",
            "operator_did": operator_did,
            "agent_did": session.did,
        }

    def status(self, *, limit: int = 20) -> dict[str, Any]:
        turns = self.state.list_turns(limit=limit)
        return {
            "activation": self.state.activation(),
            "counts": self.state.counts(),
            "turns": [
                {
                    "uri": item.turn.uri,
                    "created_at": item.turn.created_at,
                    "root_uri": item.turn.root_uri,
                    "parent_turn_uri": item.turn.parent_turn_uri,
                    "phase": item.phase.value,
                    "attempts": item.attempts,
                    "reply_uri": item.reply_uri,
                    "last_error": item.last_error,
                }
                for item in turns
            ],
        }

    def _process_claim(self, claimed: TurnState) -> None:
        token = claimed.claim_token
        if token is None:
            raise StateError(f"claimed turn {claimed.turn.uri} has no token")
        current = claimed
        if current.phase is Phase.ACCEPTED:
            self.client.ensure_like(RecordRef(current.turn.uri, current.turn.cid))
            current = self.state.mark_working(current.turn.uri, token)
        if current.phase is Phase.MARKED:
            parent_session: Path | None = None
            if current.turn.parent_turn_uri is not None:
                parent_session = self.state.session_for_turn(current.turn.parent_turn_uri)
                if parent_session is None:
                    raise StateError(
                        f"parent turn {current.turn.parent_turn_uri} has no settled session"
                    )
            publication = self.pipeline.run(current.turn, parent_session)
            current = self.state.record_publication(current.turn.uri, token, publication)
        if current.phase is Phase.RESEARCHED:
            publication = current.publication()
            root_state = self.state.get(current.turn.root_uri)
            if root_state is None:
                raise StateError(f"research root {current.turn.root_uri} is missing")
            reply = self.client.ensure_image_reply(
                rkey_seed=current.turn.uri,
                parent=RecordRef(current.turn.uri, current.turn.cid),
                root=RecordRef(root_state.turn.uri, root_state.turn.cid),
                brief=publication.brief,
                page_url=publication.page_url,
                image_path=publication.image_path,
                alt_text=publication.alt_text,
            )
            current = self.state.settle(
                current.turn.uri,
                token,
                reply_uri=reply.uri,
                reply_cid=reply.cid,
            )
        if current.phase is not Phase.SETTLED:
            raise StateError(
                f"turn {current.turn.uri} stopped in unexpected phase {current.phase.value}"
            )

    def _nearest_parent_turn(
        self,
        post: _FeedPost,
        *,
        pending_uris: set[str],
        ancestry_cache: dict[str, list[Any]],
    ) -> str | object | None:
        if post.parent_uri is None:
            return None
        direct = self.state.parent_turn_for_post(post.parent_uri)
        if direct is not None:
            return direct
        if post.parent_uri in pending_uris:
            return _WAITING
        if post.uri not in ancestry_cache:
            ancestry_cache[post.uri] = list(self.client.thread_ancestry(post.uri))
        for ancestor in ancestry_cache[post.uri]:
            uri = _ancestor_uri(ancestor)
            if uri is None:
                continue
            known = self.state.parent_turn_for_post(uri)
            if known is not None:
                return known
            if uri in pending_uris:
                return _WAITING
        return None


_WAITING = object()


def _feed_post(item: Mapping[str, Any]) -> _FeedPost | None:
    raw_post = item.get("post", item)
    if not isinstance(raw_post, dict):
        return None
    uri = raw_post.get("uri")
    cid = raw_post.get("cid")
    author = raw_post.get("author")
    record = raw_post.get("record")
    did = author.get("did") if isinstance(author, dict) else None
    if not all(isinstance(value, str) for value in (uri, cid, did)):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("$type", "app.bsky.feed.post") != "app.bsky.feed.post":
        return None
    text = record.get("text")
    created_at = record.get("createdAt")
    indexed_at = raw_post.get("indexedAt")
    if not isinstance(indexed_at, str):
        indexed_at = created_at
    if (
        not isinstance(text, str)
        or not isinstance(created_at, str)
        or not isinstance(indexed_at, str)
    ):
        return None
    reply = record.get("reply")
    if not isinstance(reply, dict):
        return _FeedPost(
            uri=uri,
            cid=cid,
            author_did=did,
            text=text,
            created_at=created_at,
            indexed_at=indexed_at,
            parent_uri=None,
            parent_cid=None,
            root_uri=uri,
            root_cid=cid,
            raw=dict(raw_post),
        )
    parent = reply.get("parent")
    root = reply.get("root")
    if not isinstance(parent, dict) or not isinstance(root, dict):
        return None
    parent_uri = parent.get("uri")
    parent_cid = parent.get("cid")
    root_uri = root.get("uri")
    root_cid = root.get("cid")
    if not all(isinstance(value, str) for value in (parent_uri, parent_cid, root_uri, root_cid)):
        return None
    return _FeedPost(
        uri=uri,
        cid=cid,
        author_did=did,
        text=text,
        created_at=created_at,
        indexed_at=indexed_at,
        parent_uri=parent_uri,
        parent_cid=parent_cid,
        root_uri=root_uri,
        root_cid=root_cid,
        raw=dict(raw_post),
    )


def _ancestor_uri(ancestor: Any) -> str | None:
    if isinstance(ancestor, PostView):
        return ancestor.uri
    if isinstance(ancestor, Mapping):
        uri = ancestor.get("uri")
        if isinstance(uri, str):
            return uri
        post = ancestor.get("post")
        if isinstance(post, Mapping) and isinstance(post.get("uri"), str):
            return str(post["uri"])
    return None


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid Bluesky timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Bluesky timestamp has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)
