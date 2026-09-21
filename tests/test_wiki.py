from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from bluesky_agent.intent import ResearchMode
from bluesky_agent.wiki import WikiError, WikiRepository


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _repository(path: Path, remote: Path | None = None) -> WikiRepository:
    return WikiRepository(
        path,
        str(remote or path),
        "https://smith.wiki",
        "Smith Wiki",
        "agent@smith.wiki",
        "",
    )


def _initialize(path: Path) -> None:
    path.mkdir()
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(path)),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")

class _PageResponse:
    status = 200

    def __init__(self, html: str) -> None:
        self.body = html.encode("utf-8")

    def __enter__(self) -> _PageResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_deployment_readiness_requires_matching_turn_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    exact = (
        '<article class="research-card" data-research-turn="turn-1" '
        'data-research-mode="SOURCE_BRIEF">'
    )
    monkeypatch.setattr(
        "bluesky_agent.wiki.urllib.request.urlopen",
        lambda request, timeout: _PageResponse(exact),
    )

    repository.wait_for_page(
        "https://wiki.example/turn-1/",
        "turn-1",
        ResearchMode.SOURCE_BRIEF,
        timeout=1,
        interval=0.1,
    )

    wrong_mode = exact.replace("SOURCE_BRIEF", "NOTE_EXPLORE")
    monkeypatch.setattr(
        "bluesky_agent.wiki.urllib.request.urlopen",
        lambda request, timeout: _PageResponse(wrong_mode),
    )
    moments = iter((0.0, 0.0, 2.0, 2.0))
    monkeypatch.setattr("bluesky_agent.wiki.time.monotonic", lambda: next(moments))
    monkeypatch.setattr("bluesky_agent.wiki.time.sleep", lambda seconds: None)

    with pytest.raises(WikiError, match="did not deploy"):
        repository.wait_for_page(
            "https://wiki.example/turn-1/",
            "turn-1",
            ResearchMode.SOURCE_BRIEF,
            timeout=1,
            interval=0.1,
        )


def test_git_hooks_are_disabled_for_publication_commands(tmp_path: Path) -> None:
    path = tmp_path / "wiki"
    _initialize(path)
    tracked = path / "tracked.txt"
    tracked.write_text("safe\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")

    marker = tmp_path / "hook-ran"
    hook = path / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)

    repository = _repository(path)
    repository._git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Safe commit",
    )

    assert not marker.exists()


def test_sync_discards_unknown_local_only_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ("git", "init", "--bare", "--initial-branch=main", str(remote)),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    seed = tmp_path / "seed"
    _initialize(seed)
    (seed / "wiki").mkdir()
    (seed / "wiki" / "index.md").write_text("safe\n", encoding="utf-8")
    _git(seed, "add", "wiki/index.md")
    _git(seed, "commit", "-m", "Initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "origin", "main")

    clone = tmp_path / "clone"
    repository = _repository(clone, remote)
    monkeypatch.setattr(repository, "_require_trusted_scaffold", lambda: None)
    repository.sync_main()
    trusted_head = repository.head()
    (clone / "wiki" / "index.md").write_text("poisoned\n", encoding="utf-8")
    repository._git("add", "wiki/index.md")
    repository._git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Unvalidated",
    )
    assert repository.head() != trusted_head

    repository.sync_main()

    assert repository.head() == trusted_head
    assert (clone / "wiki" / "index.md").read_text(encoding="utf-8") == "safe\n"


def test_generated_artifacts_cannot_publish_runtime_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "wiki-clone"
    target = path / "raw" / "linked" / "leak.txt"
    target.parent.mkdir(parents=True)
    secret = "sk-test-runtime-secret-1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    target.write_text(f"Leaked key: {secret}\n", encoding="utf-8")
    repository = _repository(path)

    with pytest.raises(WikiError, match="credential-like"):
        repository._validate_generated_content(
            [("??", PurePosixPath("raw", "linked", "leak.txt"))]
        )
