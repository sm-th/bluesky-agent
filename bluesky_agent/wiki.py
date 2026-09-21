"""Fail-closed synchronization and publication of the shared Research Wiki."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from .intent import ResearchMode
from .models import Turn


_RKEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")
_CREDENTIAL_RE = re.compile(
    rb"(?i)(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"sk-[A-Za-z0-9_-]{16,}|(?:api[_-]?key|token|password)\s*[:=]\s*"
    rb"[\"']?[A-Za-z0-9_./+=:-]{12,})"
)
_SECRET_ENV_NAMES = (
    "BLUESKY_AGENT_APP_PASSWORD",
    "BLUESKY_AGENT_GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "EXA_API_KEY",
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
)
_TRUSTED_SCAFFOLD = {
    "AGENTS.md": "ef10ec68c91c8d13e0e2fc82b56e74c126fbd67c050cbd049ca8109d8a360c8a",
    ".github/workflows/deploy.yml": "2f504e31cd99fe39777119770c4c712411c111e6c06643f4eefc2590aae1c8f5",
    "eleventy.config.js": "bd2fa3399524cb23d479b39e0b9d18afbf8010e28b657be4e58d1fb5985a9f37",
    "package.json": "6d8077e71863678e912f66a84802a62c5110cc797cecbd6234b042dbd3510308",
    "package-lock.json": "edb1258d642126915ee0b20701912d7d477fc7b87eb072ff695caf2ef2d9266f",
    "wiki/_includes/layouts/base.njk": "a771261d06bf7383e7a674d528ac817fcad1a075daef87b98f6516d98c445097",
    "wiki/_includes/layouts/page.njk": "fac2ace0d951f9db8bb12713dd81c098b43038acd13de769f5b0e31fb95eec39",
    "wiki/_includes/layouts/research.njk": "76260eb3645f7e0db7f17a3e69fdef781e2cb01fa6213039fe27dcf9270278ea",
    "wiki/research/research.11tydata.js": "d3ffb304d90d49151184405bd190214871c9ae49228bf13ba3575fae7d13c345",
    "wiki/sources/sources.11tydata.json": "6df882b8d6ef60edddca9b36f644e476b6eab498563fba605db029300ea3bd30",
    "wiki/entities/entities.11tydata.json": "a95e52482329d2545e8e56ad5ba9fe04d3516b9be182f8227c61cc573cde45df",
    "wiki/concepts/concepts.11tydata.json": "b3830d858e4a892c012c9826bafdaea5897368a902733bd94c9b27ea3b887530",
    "wiki/assets/site.css": "9d71fb8dbf41f7bfc9c1102c37c0e91ba298b677deda5911199436499f0a5c30",
}



@dataclass(frozen=True)
class RawSnapshot:
    size: int
    digest: str


class WikiError(RuntimeError):
    """The wiki workspace or its deployment violated the publication contract."""


class WikiRepository:
    """Own a single clean clone of the published main branch."""

    def __init__(
        self,
        path: Path,
        repo_url: str,
        site_url: str,
        git_user_name: str,
        git_user_email: str,
        github_token: str,
        *,
        command_timeout: float = 120,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.repo_url = repo_url.strip()
        self.site_url = site_url.rstrip("/")
        self.git_user_name = git_user_name.strip()
        self.git_user_email = git_user_email.strip()
        self.github_token = github_token.strip()
        self.command_timeout = float(command_timeout)
        if not self.repo_url or not self.site_url:
            raise ValueError("Wiki repository and site URLs are required")
        if not self.git_user_name or not self.git_user_email:
            raise ValueError("Git publication identity is required")

    def sync_main(self) -> None:
        """Clone or fast-forward main, rejecting all local ambiguity."""

        if not (self.path / ".git").is_dir():
            if self.path.exists() and any(self.path.iterdir()):
                raise WikiError(f"Wiki path exists but is not a clone: {self.path}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._git_external("clone", "--branch", "main", "--single-branch", "--", self.repo_url, str(self.path))

        if self._git("remote", "get-url", "origin").strip() != self.repo_url:
            raise WikiError("Wiki origin does not match the configured repository")
        if self._git("branch", "--show-current").strip() != "main":
            raise WikiError("Wiki clone is not on main")
        self.discard_uncommitted()
        self._git("fetch", "--prune", "origin", "main")
        counts = self._git("rev-list", "--left-right", "--count", "HEAD...origin/main").split()
        if len(counts) != 2 or not all(part.isdigit() for part in counts):
            raise WikiError("Could not determine wiki branch divergence")
        local_only, remote_only = (int(part) for part in counts)
        if local_only and remote_only:
            raise WikiError("Wiki main diverged from origin/main")
        if local_only:
            self._git("reset", "--hard", "origin/main")
            self._git("clean", "-fd", "--", "raw", "wiki")
        elif remote_only:
            self._git("merge", "--ff-only", "origin/main")
        self._require_trusted_scaffold()
        self.require_clean()

    def _require_trusted_scaffold(self) -> None:
        for relative, expected in _TRUSTED_SCAFFOLD.items():
            path = self.path / relative
            if path.is_symlink() or not path.is_file() or _digest(path) != expected:
                raise WikiError(f"Trusted wiki scaffold changed: {relative}")

    def require_clean(self) -> None:
        if self._git("status", "--porcelain=v1", "--untracked-files=all"):
            raise WikiError("Wiki workspace contains unexpected changes")

    def discard_uncommitted(self) -> None:
        """Remove interrupted, unpublished file edits from the dedicated clone."""

        if self._git("status", "--porcelain=v1", "--untracked-files=all"):
            self._git("reset", "--hard", "HEAD")
            self._git("clean", "-fd", "--", "raw", "wiki")
        self.require_clean()

    def discard_changes(self, original_head: str) -> None:
        """Restore the dedicated clone after a failed unpublished OMP run."""

        self._git("reset", "--hard", original_head)
        self._git("clean", "-fd", "--", "raw", "wiki")
        self.require_clean()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def archive_turn(self, turn: Turn) -> Path:
        """Create the immutable raw Bluesky record exactly once."""

        _validate_rkey(turn.rkey)
        target = self.path / "raw" / "bluesky" / f"{turn.rkey}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(asdict(turn), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as exc:
            raise WikiError(f"Raw Bluesky turn already exists: {target.name}") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    def raw_snapshot(self) -> dict[Path, RawSnapshot]:
        raw = self.path / "raw"
        if not raw.exists():
            return {}
        snapshot: dict[Path, RawSnapshot] = {}
        for path in raw.rglob("*"):
            if path.is_symlink():
                raise WikiError(f"Raw source must not be a symlink: {path.relative_to(self.path)}")
            if path.is_file():
                relative = path.relative_to(self.path)
                snapshot[relative] = RawSnapshot(size=path.stat().st_size, digest=_digest(path))
        return snapshot

    def validate_agent_changes(
        self,
        *,
        original_head: str,
        original_raw: Mapping[Path, RawSnapshot],
        rkey: str,
    ) -> Path:
        """Reject commits, raw mutations, and files outside the wiki contract."""

        _validate_rkey(rkey)
        if self.head() != original_head:
            raise WikiError("OMP unexpectedly changed Git history")

        current_raw = self.raw_snapshot()
        research_log = Path("raw") / "research-log.ndjson"
        for path, snapshot in original_raw.items():
            if path == research_log:
                self._validate_research_log_append(path, snapshot, rkey)
            elif current_raw.get(path) != snapshot:
                raise WikiError(f"OMP changed immutable raw source: {path}")
        new_raw = current_raw.keys() - original_raw.keys()
        for path in new_raw:
            allowed_linked = len(path.parts) >= 3 and path.parts[:2] == ("raw", "linked")
            if not allowed_linked:
                raise WikiError(f"OMP created an unexpected raw artifact: {path}")

        changes = self.changed_paths()
        if not changes:
            raise WikiError("OMP produced no wiki changes")
        changed_paths = {path for _, path in changes}
        archive = PurePosixPath("raw", "bluesky", f"{rkey}.json")
        if archive not in changed_paths:
            raise WikiError(f"Raw Bluesky archive is not publishable: {archive}")
        if PurePosixPath("raw", "research-log.ndjson") not in changed_paths:
            raise WikiError("OMP did not append the required raw research log record")
        unpublished_raw = {
            PurePosixPath(path.as_posix()) for path in new_raw
        } - changed_paths
        if unpublished_raw:
            rendered = ", ".join(str(path) for path in sorted(unpublished_raw))
            raise WikiError(f"New raw evidence is ignored or otherwise unpublishable: {rendered}")
        for status, path in changes:
            if "D" in status:
                raise WikiError(f"OMP deleted a repository file: {path}")
            if not _allowed_changed_path(path, rkey):
                raise WikiError(f"OMP changed a path outside the research contract: {path}")
        self._validate_generated_content(changes)

        expected_page = PurePosixPath("wiki", "research", f"{rkey}.md")
        research_pages = {
            path for _, path in changes if len(path.parts) >= 2 and path.parts[:2] == ("wiki", "research")
        }
        if research_pages != {expected_page}:
            rendered = ", ".join(str(path) for path in sorted(research_pages)) or "none"
            raise WikiError(f"OMP must create only {expected_page} under wiki/research; changed: {rendered}")

        page_path = self.path.joinpath(*expected_page.parts)
        if page_path.is_symlink() or not page_path.is_file():
            raise WikiError(f"Required Research Page is missing: {expected_page}")
        return page_path

    def _validate_generated_content(
        self, changes: list[tuple[str, PurePosixPath]]
    ) -> None:
        secrets = {
            value.encode("utf-8")
            for value in (
                self.github_token,
                *(os.environ.get(name, "") for name in _SECRET_ENV_NAMES),
            )
            if len(value) >= 8
        }
        for _, relative in changes:
            if not (
                len(relative.parts) >= 3
                and relative.parts[:2] == ("raw", "linked")
                or relative.parts
                and relative.parts[0] == "wiki"
            ):
                continue
            path = self.path.joinpath(*relative.parts)
            if path.is_symlink() or not path.is_file():
                continue
            payload = path.read_bytes()
            if any(secret in payload for secret in secrets) or _CREDENTIAL_RE.search(payload):
                raise WikiError(f"Generated artifact contains credential-like content: {relative}")

    def _validate_research_log_append(self, path: Path, snapshot: RawSnapshot, rkey: str) -> None:
        target = self.path / path
        if not target.is_file() or target.is_symlink() or target.stat().st_size <= snapshot.size:
            raise WikiError("OMP did not append to raw/research-log.ndjson")
        with target.open("rb") as handle:
            prefix = handle.read(snapshot.size)
            appended = handle.read()
        if hashlib.sha256(prefix).hexdigest() != snapshot.digest:
            raise WikiError("OMP rewrote existing raw/research-log.ndjson bytes")
        if prefix and not prefix.endswith(b"\n"):
            raise WikiError("Existing raw research log does not end at a record boundary")
        try:
            decoded = appended.decode("utf-8", "strict")
            if not decoded.endswith("\n") or "\r" in decoded or len(decoded.splitlines()) != 1:
                raise WikiError("OMP must append one physical NDJSON line")
            record = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WikiError("OMP appended an invalid raw research log record") from exc
        required = {"rkey", "uri", "cid", "created_at", "parent_uri", "root_uri", "recorded_at"}
        if (
            not isinstance(record, dict)
            or set(record) != required
            or record.get("rkey") != rkey
            or not isinstance(record.get("recorded_at"), str)
            or not record["recorded_at"]
        ):
            raise WikiError("OMP must append exactly one complete research log record for this turn")

    def changed_paths(self) -> list[tuple[str, PurePosixPath]]:
        output = self._git_bytes("status", "--porcelain=v1", "-z", "--untracked-files=all")
        if not output:
            return []
        entries = output.split(b"\0")
        changes: list[tuple[str, PurePosixPath]] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            if len(entry) < 4 or entry[2:3] != b" ":
                raise WikiError("Could not parse wiki status")
            status = entry[:2].decode("ascii", "strict")
            if "R" in status or "C" in status:
                raise WikiError("OMP may not rename or copy tracked repository files")
            raw_path = entry[3:].decode("utf-8", "strict")
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts:
                raise WikiError("Git reported an unsafe changed path")
            changes.append((status, path))
        return changes

    def commit_and_push(self, rkey: str) -> str:
        """Commit the validated turn and publish it to main."""

        _validate_rkey(rkey)
        self._git("add", "--all", "--", "raw", "wiki")
        self._git(
            "-c",
            f"user.name={self.git_user_name}",
            "-c",
            f"user.email={self.git_user_email}",
            "commit",
            "-m",
            f"Research Bluesky turn {rkey}",
        )
        commit = self.head()
        self.require_clean()
        self._git("push", "origin", "HEAD:main")
        return commit

    def page_url(self, rkey: str) -> str:
        _validate_rkey(rkey)
        return f"{self.site_url}/{rkey}/"

    def wait_for_page(
        self,
        page_url: str,
        rkey: str,
        mode: ResearchMode,
        timeout: float,
        interval: float,
    ) -> None:
        """Wait for the deployed renderer's exact per-turn, per-mode article marker."""

        if timeout <= 0 or interval <= 0:
            raise ValueError("Wiki readiness timeout and interval must be positive")
        marker = (
            f'<article class="research-card" data-research-turn="{rkey}" '
            f'data-research-mode="{mode.value}">'
        )
        deadline = time.monotonic() + timeout
        last_problem = "deployment was not checked"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WikiError(f"Research Page did not deploy before timeout: {last_problem}")
            request = urllib.request.Request(
                page_url,
                headers={"Accept": "text/html", "Cache-Control": "no-cache", "User-Agent": "bluesky-research-agent/1"},
            )
            try:
                with urllib.request.urlopen(request, timeout=min(15.0, remaining)) as response:
                    if response.status != 200:
                        last_problem = f"HTTP {response.status}"
                    else:
                        body = response.read(2_000_001)
                        if len(body) > 2_000_000:
                            last_problem = "response exceeded 2 MB"
                        else:
                            html = body.decode("utf-8", "strict")
                            if marker in html:
                                return
                            last_problem = "turn marker is absent"
            except (OSError, UnicodeError, urllib.error.HTTPError, urllib.error.URLError) as exc:
                last_problem = str(exc)
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    def _git(self, *args: str) -> str:
        return self._git_bytes(*args).decode("utf-8", "strict").strip()

    def _git_bytes(self, *args: str) -> bytes:
        authenticated = any(argument in {"fetch", "push"} for argument in args)
        return self._run_git(
            ("git", "-c", "core.hooksPath=/dev/null", "-C", str(self.path), *args),
            authenticated=authenticated,
        )

    def _git_external(self, *args: str) -> str:
        authenticated = bool(args and args[0] == "clone")
        return self._run_git(
            ("git", "-c", "core.hooksPath=/dev/null", *args),
            authenticated=authenticated,
        ).decode("utf-8", "strict").strip()

    def _run_git(self, argv: tuple[str, ...], *, authenticated: bool) -> bytes:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        if (
            authenticated
            and self.github_token
            and self.repo_url.startswith("https://github.com/")
        ):
            credential = base64.b64encode(
                f"x-access-token:{self.github_token}".encode()
            ).decode("ascii")
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credential}"
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=self.command_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WikiError(f"Git command failed to execute: {argv[-1]}: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip() or "no diagnostic output"
            raise WikiError(f"Git command failed ({argv[-1]}): {detail}")
        return result.stdout



def _allowed_changed_path(path: PurePosixPath, rkey: str) -> bool:
    if path == PurePosixPath("raw", "bluesky", f"{rkey}.json"):
        return True
    if path == PurePosixPath("raw", "research-log.ndjson"):
        return True
    if len(path.parts) >= 3 and path.parts[:2] == ("raw", "linked"):
        return True
    if path in {PurePosixPath("wiki", "index.md"), PurePosixPath("wiki", "log.md")}:
        return True
    if path == PurePosixPath("wiki", "research", f"{rkey}.md"):
        return True
    return (
        len(path.parts) == 3
        and path.parts[0] == "wiki"
        and path.parts[1] in {"sources", "entities", "concepts"}
        and path.suffix == ".md"
    )

def _validate_rkey(rkey: str) -> None:
    if not _RKEY_RE.fullmatch(rkey) or rkey in {".", ".."}:
        raise WikiError(f"Unsafe Bluesky record key: {rkey!r}")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
