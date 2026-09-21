from __future__ import annotations

import json
from pathlib import Path

import pytest

from bluesky_agent.omp import OMPError, OMPRunner, parse_json_events, terminal_assistant_text


_FAKE_OMP = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

if sys.argv[1:] == ["--version"]:
    print("omp/18.2.4")
    raise SystemExit(0)
if sys.argv[1:] == ["--help"]:
    print("--fork <session>  Fork a saved session")
    raise SystemExit(0)

args = sys.argv[1:]
with open(pathlib.Path(__file__).with_suffix(".log"), "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
session_dir = pathlib.Path(args[args.index("--session-dir") + 1])
session_dir.mkdir(parents=True, exist_ok=True)
session = session_dir / f"{time.time_ns()}.jsonl"
session.write_text('{"type":"session"}\n', encoding="utf-8")
message = {"role": "assistant", "content": [{"type": "text", "text": "BRIEF_REPLY: Direct answer."}]}
print(json.dumps({"type": "message_end", "message": message}))
print(json.dumps({"type": "agent_end", "messages": [message], "isTerminal": True}))
'''


def _fake_omp(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "omp"
    executable.write_text(_FAKE_OMP, encoding="utf-8")
    executable.chmod(0o755)
    return executable, executable.with_suffix(".log")


def test_root_and_child_create_distinct_durable_sessions_and_fork_exact_parent(
    tmp_path: Path,
) -> None:
    executable, log = _fake_omp(tmp_path)
    workspace = tmp_path / "wiki"
    workspace.mkdir()
    sessions = tmp_path / "sessions"
    runner = OMPRunner(sessions, "openai/test", 10, executable=str(executable))

    root = runner.run("root prompt", workspace)
    parent_before = root.session_file.read_bytes()
    child = runner.run("child prompt", workspace, root.session_file)

    assert root.session_file != child.session_file
    assert root.session_file.exists()
    assert child.session_file.exists()
    assert root.session_file.read_bytes() == parent_before
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert "--fork" not in calls[0]
    fork_index = calls[1].index("--fork")
    assert calls[1][fork_index + 1] == str(root.session_file.resolve())
    assert calls[0][calls[0].index("--session-dir") + 1] == str(sessions.resolve())


def test_preflight_rejects_omp_without_fork_capability(tmp_path: Path) -> None:
    executable = tmp_path / "omp"
    executable.write_text(
        "#!/bin/sh\n[ \"$1\" = --version ] && echo omp/18.2.4 || echo no-fork\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    runner = OMPRunner(tmp_path / "sessions", "model", 10, executable=str(executable))

    with pytest.raises(OMPError, match="--fork capability"):
        runner.preflight()


def test_json_events_require_terminal_completed_assistant_text() -> None:
    events = parse_json_events(
        '\n'.join(
            (
                '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"settled"}]}}',
                '{"type":"agent_end","messages":[],"isTerminal":true}',
            )
        )
    )
    assert terminal_assistant_text(events) == "settled"

    with pytest.raises(OMPError, match="terminal agent_end"):
        terminal_assistant_text([{"type": "agent_end", "messages": [], "isTerminal": False}])


def test_json_parser_rejects_non_json_output() -> None:
    with pytest.raises(OMPError, match="Malformed OMP JSON"):
        parse_json_events("not-json")
