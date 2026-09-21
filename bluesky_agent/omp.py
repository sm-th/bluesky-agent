"""Persistent, branch-safe execution of the OMP research session."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


MINIMUM_OMP_VERSION = (18, 2, 4)
_VERSION_RE = re.compile(r"(?:omp[/ ]v?)(\d+)\.(\d+)\.(\d+)", re.IGNORECASE)
_FORK_RE = re.compile(r"(?:^|\s)--fork(?:[=\s]|$)", re.MULTILINE)

_OMP_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "SSL_CERT_FILE",
        "NIX_SSL_CERT_FILE",
        "GIT_SSL_CAINFO",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "XAI_API_KEY",
        "MISTRAL_API_KEY",
        "DEEPSEEK_API_KEY",
        "EXA_API_KEY",
        "BRAVE_API_KEY",
        "TAVILY_API_KEY",
    }
)


class OMPError(RuntimeError):
    """OMP could not produce one complete, durable research turn."""


@dataclass(frozen=True)
class OMPResult:
    """The durable session and terminal assistant response from one run."""

    session_file: Path
    assistant_text: str


class OMPRunner:
    """Run OMP headlessly while owning session discovery and fork lineage."""

    def __init__(
        self,
        session_dir: Path,
        model: str,
        timeout: float,
        *,
        executable: str = "omp",
    ) -> None:
        self.session_dir = Path(session_dir).expanduser().resolve()
        self.model = model.strip()
        self.timeout = float(timeout)
        self.executable = executable
        self._preflight_complete = False
        if not self.model:
            raise ValueError("OMP model must not be empty")
        if self.timeout <= 0:
            raise ValueError("OMP timeout must be positive")

    def preflight(self) -> tuple[int, int, int]:
        """Require the exact OMP release family and startup fork capability."""

        version_result = self._probe("--version")
        match = _VERSION_RE.search(version_result.stdout)
        if match is None:
            raise OMPError(f"Could not parse OMP version: {version_result.stdout.strip()!r}")
        version = tuple(int(part) for part in match.groups())
        if version < MINIMUM_OMP_VERSION:
            minimum = ".".join(str(part) for part in MINIMUM_OMP_VERSION)
            actual = ".".join(str(part) for part in version)
            raise OMPError(f"OMP {minimum} or newer is required for safe session forking; found {actual}")

        help_result = self._probe("--help")
        if _FORK_RE.search(help_result.stdout) is None:
            raise OMPError("Installed OMP does not advertise the required startup --fork capability")

        self._preflight_complete = True
        return version

    def run(self, prompt: str, cwd: Path, parent_session: Path | None = None) -> OMPResult:
        """Create a root session or a full child fork and return its final text."""

        if not self._preflight_complete:
            self.preflight()
        if not prompt.strip():
            raise ValueError("OMP prompt must not be empty")

        workspace = Path(cwd).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise OMPError(f"OMP workspace is not a directory: {workspace}")
        self.session_dir.mkdir(parents=True, exist_ok=True)

        parent: Path | None = None
        parent_digest: str | None = None
        if parent_session is not None:
            parent = self._validated_parent(parent_session)
            parent_digest = _file_digest(parent)

        before = self._session_files()
        argv = self._argv(prompt, workspace, parent)
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            env={key: value for key, value in os.environ.items() if key in _OMP_ENV_ALLOWLIST},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            raise OMPError(f"OMP exceeded its {self.timeout:g}-second timeout") from exc

        if process.returncode != 0:
            detail = stderr.strip() or stdout.strip() or "no diagnostic output"
            raise OMPError(f"OMP exited with status {process.returncode}: {detail}")

        after = self._session_files()
        created = sorted(after - before)
        if len(created) != 1:
            raise OMPError(f"OMP must create exactly one new session file; found {len(created)}")
        session_file = created[0]
        if not session_file.is_file() or session_file.stat().st_size == 0:
            raise OMPError(f"OMP created an empty or invalid session: {session_file}")
        if parent is not None:
            if session_file == parent:
                raise OMPError("OMP child run reused its settled parent session")
            if _file_digest(parent) != parent_digest:
                raise OMPError("OMP mutated the settled parent session while forking")

        assistant_text = terminal_assistant_text(parse_json_events(stdout))
        return OMPResult(session_file=session_file, assistant_text=assistant_text)

    def _probe(self, flag: str) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.executable, flag],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=min(self.timeout, 30),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OMPError(f"Could not execute OMP preflight {flag}: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise OMPError(f"OMP preflight {flag} failed: {detail}")
        return result

    def _argv(self, prompt: str, cwd: Path, parent: Path | None) -> list[str]:
        argv = [
            self.executable,
            "-p",
            "--mode",
            "json",
            "--session-dir",
            str(self.session_dir),
            "--cwd",
            str(cwd),
            "--model",
            self.model,
            "--approval-mode",
            "yolo",
            "--no-title",
        ]
        if parent is not None:
            argv.extend(("--fork", str(parent)))
        argv.extend(("--", prompt))
        return argv

    def _validated_parent(self, path: Path) -> Path:
        source = Path(path).expanduser()
        if source.is_symlink():
            raise OMPError(f"Parent session must not be a symlink: {source}")
        try:
            parent = source.resolve(strict=True)
        except OSError as exc:
            raise OMPError(f"Parent session does not exist: {path}") from exc
        try:
            parent.relative_to(self.session_dir)
        except ValueError as exc:
            raise OMPError("Parent session is outside the controlled session directory") from exc
        if parent.suffix != ".jsonl" or not parent.is_file():
            raise OMPError(f"Parent session is not a regular JSONL session: {parent}")
        return parent

    def _session_files(self) -> set[Path]:
        return {path.resolve() for path in self.session_dir.rglob("*.jsonl") if path.is_file()}


def parse_json_events(output: str) -> list[dict[str, Any]]:
    """Parse OMP's newline-delimited JSON stream without ignoring corruption."""

    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OMPError(f"Malformed OMP JSON event on line {line_number}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise OMPError(f"Invalid OMP JSON event on line {line_number}")
        events.append(event)
    if not events:
        raise OMPError("OMP emitted no JSON events")
    return events


def terminal_assistant_text(events: Iterable[dict[str, Any]]) -> str:
    """Extract assistant text only after a terminal agent_end event."""

    completed_assistant_messages: list[dict[str, Any]] = []
    terminal_event: dict[str, Any] | None = None
    for event in events:
        event_type = event.get("type")
        if event_type == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                completed_assistant_messages.append(message)
        elif event_type == "agent_end" and event.get("isTerminal") is not False:
            terminal_event = event

    if terminal_event is None:
        raise OMPError("OMP stream ended without a terminal agent_end event")

    terminal_messages = terminal_event.get("messages")
    if isinstance(terminal_messages, list):
        for message in reversed(terminal_messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = _message_text(message)
                if text:
                    return text
    for message in reversed(completed_assistant_messages):
        text = _message_text(message)
        if text:
            return text
    raise OMPError("Terminal OMP event contained no completed assistant text")


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts).strip()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
