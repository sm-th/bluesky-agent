"""Command-line entrypoint for the Bluesky research agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from .config import Config, ConfigError
from .runner import Runner
from .state import StateError

_PUBLISHING_SECRET_ENV = (
    "BLUESKY_AGENT_APP_PASSWORD",
    "BLUESKY_AGENT_AGENT_APP_PASSWORD",
    "BLUESKY_AGENT_GITHUB_TOKEN",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bluesky-agent")
    parser.add_argument(
        "--config",
        help="TOML configuration path (or set BLUESKY_AGENT_CONFIG)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    activate = commands.add_parser("activate", help="start eligibility from this moment")
    activate.add_argument(
        "--at",
        metavar="RFC3339",
        help="explicit activation timestamp, primarily for controlled deployment",
    )
    commands.add_parser("once", help="poll and process the available queue once")
    commands.add_parser("run", help="poll and process continuously")
    show = commands.add_parser("show", help="show durable lifecycle state")
    show.add_argument("--limit", type=int, default=20, help="maximum recent turns to show")
    commands.add_parser("doctor", help="verify state and Bluesky identity bindings")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = Config.load(arguments.config)
        for name in _PUBLISHING_SECRET_ENV:
            os.environ.pop(name, None)
        runner = Runner(config)
        if arguments.command == "activate":
            activated_at = runner.state.activate(arguments.at)
            print(json.dumps({"activation": activated_at}, sort_keys=True))
            return 0
        if arguments.command == "show":
            if arguments.limit <= 0:
                raise ValueError("--limit must be greater than zero")
            print(json.dumps(runner.status(limit=arguments.limit), indent=2, sort_keys=True))
            return 0
        if arguments.command == "doctor":
            print(json.dumps(runner.doctor(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "once":
            report = runner.run_once()
            print(
                json.dumps(
                    {
                        "discovered": report.discovered,
                        "settled": report.settled,
                        "failures": list(report.failures),
                    },
                    sort_keys=True,
                )
            )
            return 1 if report.failures else 0
        if arguments.command == "run":
            try:
                runner.run_forever()
            except KeyboardInterrupt:
                return 0
            return 0
        raise AssertionError(f"unhandled command: {arguments.command}")
    except (ConfigError, StateError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
