"""End-to-end research, wiki publication, and screenshot orchestration."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Protocol
from .atproto import MAX_GRAPHEMES as MAX_BLUESKY_GRAPHEMES
from .atproto import grapheme_count

from .intent import IntentRouter, ResearchMode, RoutingDecision
from .models import Publication, Turn
from .omp import OMPRunner
from .prompts import RESEARCH_PAGE_MAX_WORDS, build_research_prompt
from .wiki import WikiRepository


_SOURCE_LINK_RE = re.compile(r"\[[^\]\n]+\]\(\.\./sources/([A-Za-z0-9][A-Za-z0-9._~-]*)/\)")
MAX_RESEARCH_PAGE_BYTES = 60_000
_REQUIRED_FRONTMATTER = {"title", "rkey", "date", "brief", "turn_url", "mode"}
_WORD_RE = re.compile(r"\b[^\W_]+(?:[’'-][^\W_]+)*\b", re.UNICODE)
_NON_ENGLISH_SCRIPTS = (
    "ARABIC",
    "ARMENIAN",
    "BENGALI",
    "BOPOMOFO",
    "CJK",
    "CYRILLIC",
    "DEVANAGARI",
    "ETHIOPIC",
    "GEORGIAN",
    "GREEK",
    "GUJARATI",
    "GURMUKHI",
    "HANGUL",
    "HEBREW",
    "HIRAGANA",
    "KANNADA",
    "KATAKANA",
    "KHMER",
    "LAO",
    "MALAYALAM",
    "MYANMAR",
    "ORIYA",
    "SINHALA",
    "TAMIL",
    "TELUGU",
    "THAI",
    "TIBETAN",
)


class ResearchError(RuntimeError):
    """A Research Turn failed validation before Bluesky publication."""


class Screenshotter(Protocol):
    def capture(self, page_url: str, rkey: str) -> Path: ...


class ResearchPipeline:
    """Publish exactly one validated Research Page for one Research Turn."""

    def __init__(self, config: object, screenshot: Screenshotter) -> None:
        self.config = config
        self.screenshot = screenshot
        state_dir = Path(getattr(config, "state_dir")).expanduser().resolve()
        self.recovery_dir = state_dir / "research-runs"
        self.omp = OMPRunner(
            state_dir / "omp-sessions",
            str(getattr(config, "omp_model")),
            float(getattr(config, "omp_timeout")),
        )
        self.wiki = WikiRepository(
            state_dir / "wiki",
            str(getattr(config, "wiki_repo_url")),
            str(getattr(config, "wiki_site_url")),
            str(getattr(config, "git_user_name")),
            str(getattr(config, "git_user_email")),
            str(getattr(config, "github_token")),
        )
        self.router = IntentRouter.from_environment(config)

    def run(self, turn: Turn, parent_session: Path | None) -> Publication:
        """Research, deploy, verify, and render a single accepted turn."""

        self.wiki.sync_main()
        page_url = self.wiki.page_url(turn.rkey)
        page_path = self.wiki.path / "wiki" / "research" / f"{turn.rkey}.md"
        receipt_path = self.recovery_dir / f"{turn.rkey}.json"
        if page_path.is_symlink():
            raise ResearchError(f"Research Page path is a symlink for turn {turn.rkey}")
        if page_path.exists():
            return self._recover_publication(turn, page_path, page_url)

        parent_mode = self._parent_mode(turn)
        routing = self.router.route(turn, parent_mode)
        receipt_path.unlink(missing_ok=True)
        original_head = self.wiki.head()
        try:
            self.wiki.archive_turn(turn)
            original_raw = self.wiki.raw_snapshot()
            prompt = build_research_prompt(
                turn,
                routing.mode,
                operator_handle=str(getattr(self.config, "operator_handle")),
            )
            omp_result = self.omp.run(prompt, self.wiki.path, parent_session)
            brief = parse_brief_reply(omp_result.assistant_text)
            validate_bluesky_reply(brief, page_url)
            self._write_recovery(turn, omp_result.session_file, brief, routing)

            page_path = self.wiki.validate_agent_changes(
                original_head=original_head,
                original_raw=original_raw,
                rkey=turn.rkey,
            )
            validate_changed_wiki_artifacts(self.wiki)
            validate_research_page(
                page_path,
                turn,
                brief,
                operator_handle=str(getattr(self.config, "operator_handle")),
                mode=routing.mode,
            )
        except BaseException:
            self.wiki.discard_changes(original_head)
            receipt_path.unlink(missing_ok=True)
            raise

        try:
            self.wiki.commit_and_push(turn.rkey)
        except BaseException:
            self.wiki.discard_changes(original_head)
            receipt_path.unlink(missing_ok=True)
            raise
        return self._capture_publication(
            turn,
            page_url,
            brief,
            omp_result.session_file,
            routing.mode,
        )

    def _recover_publication(self, turn: Turn, page_path: Path, page_url: str) -> Publication:
        receipt_path = self.recovery_dir / f"{turn.rkey}.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResearchError("Published Research Page has no valid local recovery receipt") from exc
        if (
            not isinstance(receipt, dict)
            or receipt.get("turn_uri") != turn.uri
            or receipt.get("turn_cid") != turn.cid
            or not isinstance(receipt.get("brief"), str)
            or not isinstance(receipt.get("session_file"), str)
            or not isinstance(receipt.get("mode"), str)
        ):
            raise ResearchError("Recovery receipt does not match the accepted Research Turn")
        session_source = Path(receipt["session_file"]).expanduser()
        if session_source.is_symlink():
            raise ResearchError("Recovery receipt names a symlinked Research Session")
        try:
            session_file = session_source.resolve(strict=True)
            session_file.relative_to(self.omp.session_dir)
        except (OSError, ValueError) as exc:
            raise ResearchError("Recovery receipt names an invalid Research Session") from exc
        if not session_file.is_file() or session_file.suffix != ".jsonl":
            raise ResearchError("Recovery receipt names an invalid Research Session")
        brief = receipt["brief"]
        try:
            mode = ResearchMode(receipt["mode"])
        except ValueError as exc:
            raise ResearchError("Recovery receipt contains an invalid research mode") from exc
        validate_bluesky_reply(brief, page_url)
        validate_research_page(
            page_path,
            turn,
            brief,
            operator_handle=str(getattr(self.config, "operator_handle")),
            mode=mode,
        )
        return self._capture_publication(turn, page_url, brief, session_file, mode)

    def _write_recovery(
        self,
        turn: Turn,
        session_file: Path,
        brief: str,
        routing: RoutingDecision,
    ) -> None:
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        target = self.recovery_dir / f"{turn.rkey}.json"
        payload = json.dumps(
            {
                "turn_uri": turn.uri,
                "turn_cid": turn.cid,
                "session_file": str(session_file),
                "brief": brief,
                "mode": routing.mode.value,
                "routing": {
                    "mode": routing.mode.value,
                    "source": routing.source,
                    "model": routing.model,
                    "fallback_reason": routing.fallback_reason,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ResearchError("A recovery receipt already exists for an unpublished turn") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def _parent_mode(self, turn: Turn) -> ResearchMode | None:
        if turn.parent_turn_uri is None:
            return None
        parent_rkey = turn.parent_turn_uri.rsplit("/", 1)[-1]
        parent_path = self.wiki.path / "wiki" / "research" / f"{parent_rkey}.md"
        if parent_path.is_symlink() or not parent_path.is_file():
            raise ResearchError("Settled parent Research Page is missing")
        try:
            text = parent_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ResearchError("Settled parent Research Page is unreadable") from exc
        frontmatter, _ = _split_frontmatter(text)
        try:
            return ResearchMode(frontmatter.get("mode", ""))
        except ValueError as exc:
            raise ResearchError("Settled parent Research Page has an invalid mode") from exc


    def _capture_publication(
        self,
        turn: Turn,
        page_url: str,
        brief: str,
        session_file: Path,
        mode: ResearchMode,
    ) -> Publication:
        self.wiki.wait_for_page(
            page_url,
            turn.rkey,
            mode,
            float(getattr(self.config, "wiki_ready_timeout")),
            float(getattr(self.config, "wiki_ready_interval")),
        )
        image_candidate = Path(self.screenshot.capture(page_url, turn.rkey)).expanduser()
        if image_candidate.is_symlink():
            raise ResearchError("PageScreenshot returned a symlink instead of an image")
        image_path = image_candidate.resolve(strict=True)
        if not image_path.is_file():
            raise ResearchError("PageScreenshot did not return a regular image file")

        label = {
            ResearchMode.SOURCE_BRIEF: "source brief",
            ResearchMode.QUESTION_ANSWER: "research answer",
            ResearchMode.NOTE_EXPLORE: "research exploration",
        }[mode]
        alt_text = f"Screenshot of the expanded {label}. {brief}"
        ensure_english_only(alt_text, "Page Screenshot alt text")
        return Publication(
            brief=brief,
            page_url=page_url,
            image_path=image_path,
            alt_text=alt_text,
            session_file=session_file,
        )


def parse_brief_reply(assistant_text: str) -> str:
    """Accept only the terminal one-line response promised by the prompt."""

    lines = assistant_text.strip().splitlines()
    if len(lines) != 1 or not lines[0].startswith("BRIEF_REPLY: "):
        raise ResearchError("OMP response must contain only one BRIEF_REPLY line")
    brief = lines[0].removeprefix("BRIEF_REPLY: ").strip()
    if not brief:
        raise ResearchError("BRIEF_REPLY is empty")
    if "http://" in brief.lower() or "https://" in brief.lower():
        raise ResearchError("BRIEF_REPLY must not contain a URL")
    ensure_english_only(brief, "Brief Reply")
    _validate_direct_brief(brief)
    return brief


def validate_bluesky_reply(brief: str, page_url: str) -> None:
    """Validate the exact two-line text that the publisher will send."""

    if "\n" in brief or "\r" in brief:
        raise ResearchError("Brief Reply must be one line")
    count = grapheme_len(f"{brief}\n{page_url}")
    if count > MAX_BLUESKY_GRAPHEMES:
        raise ResearchError(f"Brief Reply and page URL use {count} graphemes; limit is {MAX_BLUESKY_GRAPHEMES}")


def validate_research_page(
    path: Path,
    turn: Turn,
    brief: str,
    *,
    operator_handle: str,
    mode: ResearchMode,
) -> None:
    """Validate the compact public page before it can be committed."""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ResearchError(f"Could not read Research Page: {path}") from exc
    if len(payload) > MAX_RESEARCH_PAGE_BYTES:
        raise ResearchError("Research Page is too large for the screenshot contract")
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ResearchError("Research Page is not valid UTF-8") from exc
    ensure_english_only(text, "Research Page")

    frontmatter, body = _split_frontmatter(text)
    unexpected = frontmatter.keys() - _REQUIRED_FRONTMATTER
    missing = _REQUIRED_FRONTMATTER - frontmatter.keys()
    if unexpected:
        raise ResearchError(
            f"Research Page frontmatter is not allowed: {', '.join(sorted(unexpected))}"
        )
    if missing:
        raise ResearchError(f"Research Page frontmatter is missing: {', '.join(sorted(missing))}")
    if frontmatter["rkey"] != turn.rkey:
        raise ResearchError("Research Page rkey does not match the Research Turn")
    if frontmatter["date"] != turn.created_at:
        raise ResearchError("Research Page date does not match the Research Turn")
    if frontmatter["brief"] != brief:
        raise ResearchError("Research Page brief does not match BRIEF_REPLY")
    expected_turn_url = bluesky_turn_url(operator_handle, turn.rkey)
    if frontmatter["turn_url"] != expected_turn_url:
        raise ResearchError("Research Page turn_url is not the canonical Bluesky URL")
    if frontmatter["mode"] != mode.value:
        raise ResearchError("Research Page mode does not match the routed research mode")
    if not frontmatter["title"].strip():
        raise ResearchError("Research Page title is empty")
    required_heading = {
        ResearchMode.SOURCE_BRIEF: "Source brief",
        ResearchMode.QUESTION_ANSWER: "Answer",
        ResearchMode.NOTE_EXPLORE: "Exploration",
    }[mode]
    if re.search(rf"^## {re.escape(required_heading)}\s*$", body, re.MULTILINE) is None:
        raise ResearchError(f"Research Page is missing its {required_heading} section")
    if len(_WORD_RE.findall(body)) > RESEARCH_PAGE_MAX_WORDS:
        raise ResearchError(f"Research Page exceeds {RESEARCH_PAGE_MAX_WORDS} words")
    source_slugs = _SOURCE_LINK_RE.findall(body)
    if not source_slugs:
        raise ResearchError("Research Page contains no claim-level link to a published source note")
    for slug in source_slugs:
        source_path = path.parent.parent / "sources" / f"{slug}.md"
        if source_path.is_symlink() or not source_path.is_file():
            raise ResearchError(f"Research Page cites a missing source note: {slug}")
        source_text = _read_english_markdown(source_path, "Cited source note")
        if "raw/linked/" not in source_text or not re.search(r"https?://", source_text):
            raise ResearchError(f"Cited source note lacks original URL or preserved evidence: {slug}")



def validate_changed_wiki_artifacts(repository: WikiRepository) -> None:
    """Require every generated or updated published artifact to remain English Markdown."""

    for _, relative in repository.changed_paths():
        if not relative.parts or relative.parts[0] != "wiki":
            continue
        path = repository.path.joinpath(*relative.parts)
        if path.suffix != ".md" or path.is_symlink() or not path.is_file():
            raise ResearchError(f"Unexpected published wiki artifact: {relative}")
        _read_english_markdown(path, f"Wiki artifact {relative}")


def _read_english_markdown(path: Path, artifact: str) -> str:
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8", "strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise ResearchError(f"{artifact} is not readable UTF-8 Markdown") from exc
    ensure_english_only(text, artifact)
    if any(token in text for token in ("{{", "{%", "{#")):
        raise ResearchError(f"{artifact} contains executable template syntax")
    if re.search(r"</?[A-Za-z][^>\n]*>|<!--|<!DOCTYPE", text, re.IGNORECASE):
        raise ResearchError(f"{artifact} contains raw HTML")
    if re.search(r"&(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);", text):
        raise ResearchError(f"{artifact} contains an HTML entity")
    return text


def bluesky_turn_url(operator_handle: str, rkey: str) -> str:
    handle = operator_handle.strip().lstrip("@")
    if not handle or "/" in handle or "?" in handle or "#" in handle:
        raise ResearchError("Configured Operator handle is invalid")
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def ensure_english_only(text: str, artifact: str) -> None:
    """Fail closed on scripts that cannot be English publication prose."""

    for character in text:
        if not character.isalpha():
            continue
        name = unicodedata.name(character, "")
        if any(script in name for script in _NON_ENGLISH_SCRIPTS):
            raise ResearchError(f"{artifact} contains non-English script text")


def grapheme_len(text: str) -> int:
    """Count exactly the graphemes used by the Bluesky publisher."""

    return grapheme_count(text)


def _validate_direct_brief(brief: str) -> None:
    lowered = brief.casefold()
    process_patterns = (
        r"^(?:here(?:'s| is)|done\b|research complete\b|i\b|we\b)",
        r"\b(?:i|we)\s+(?:researched|investigated|looked|found|analyzed|updated|created|wrote|added)\b",
        r"\b(?:this page|the page|my research|the research process)\b",
    )
    if any(re.search(pattern, lowered) for pattern in process_patterns):
        raise ResearchError("Brief Reply describes the process instead of directly answering")


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ResearchError("Research Page must begin with YAML frontmatter")
    boundary = text.find("\n---\n", 4)
    if boundary < 0:
        raise ResearchError("Research Page frontmatter is not closed")
    raw_frontmatter = text[4:boundary]
    frontmatter: dict[str, str] = {}
    for line in raw_frontmatter.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):\s*(.*)", line)
        if match is None:
            raise ResearchError(f"Unsupported Research Page frontmatter line: {line!r}")
        key, raw_value = match.groups()
        if key in frontmatter:
            raise ResearchError(f"Duplicate Research Page frontmatter key: {key}")
        frontmatter[key] = _yaml_scalar(raw_value)
    return frontmatter, text[boundary + 5 :]


def _yaml_scalar(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ResearchError("Invalid quoted Research Page frontmatter value") from exc
        if not isinstance(parsed, str):
            raise ResearchError("Research Page frontmatter values must be strings")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ResearchError("Invalid quoted Research Page frontmatter value")
        return value[1:-1].replace("''", "'")
    return value
