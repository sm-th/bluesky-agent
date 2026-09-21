from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bluesky_agent.config import Config, ConfigError
from bluesky_agent.intent import IntentRouter, ResearchMode
from bluesky_agent.models import Turn


class FakeHTTPResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


def make_config(root: Path, **overrides: object) -> Config:
    values: dict[str, object] = {
        "operator_handle": "operator.example",
        "operator_did": "did:plc:operator",
        "agent_handle": "agent.example",
        "agent_app_password": "app-password",
        "bluesky_service": "https://bsky.example",
        "bluesky_public_api": "https://public.example",
        "wiki_repo_url": "https://github.com/example/wiki.git",
        "wiki_site_url": "https://wiki.example",
        "state_dir": root / "state",
        "poll_interval": 1,
        "omp_model": "research-model",
        "omp_timeout": 30,
        "wiki_ready_timeout": 5,
        "wiki_ready_interval": 0.1,
        "chromium_bin": "chromium",
        "git_user_name": "Research Agent",
        "git_user_email": "agent@example.com",
        "github_token": "github-token",
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


def turn(text: str, *, reply: bool = False) -> Turn:
    return Turn(
        uri="at://did:plc:operator/app.bsky.feed.post/turn",
        cid="cid-turn",
        rkey="turn",
        text=text,
        created_at="2026-09-21T00:00:00Z",
        parent_uri="at://did:plc:agent/app.bsky.feed.post/parent" if reply else None,
        root_uri="at://did:plc:operator/app.bsky.feed.post/root",
        parent_turn_uri="at://did:plc:operator/app.bsky.feed.post/parent" if reply else None,
        raw={},
    )


def completion(
    choice: str = "QUESTION_ANSWER",
    *,
    confidence: float = 0.91,
    probabilities: dict[str, float] | None = None,
    model: str = "typesafe/jev",
) -> dict[str, object]:
    answer = {
        "choice": choice,
        "probabilities": probabilities
        or {"SOURCE_BRIEF": 0.04, "QUESTION_ANSWER": 0.91, "NOTE_EXPLORE": 0.05},
        "confidence": confidence,
    }
    return {
        "model": model,
        "choices": [{"message": {"content": json.dumps(answer)}}],
    }


class StructuralRoutingTests(unittest.TestCase):
    def test_clear_rules_cover_each_mode_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = IntentRouter.from_environment(
                make_config(Path(directory)),
                {"OPENAI_BASE_URL": "https://manifest.example/v1", "OPENAI_API_KEY": "secret"},
            )
            cases = [
                ("https://example.com/paper", ResearchMode.SOURCE_BRIEF),
                ("https://example.com/paper?id=1", ResearchMode.SOURCE_BRIEF),
                ("Summarize this paper: https://example.com/paper", ResearchMode.SOURCE_BRIEF),
                ("Why does this result matter? https://example.com/paper", ResearchMode.QUESTION_ANSWER),
                ("How should leases be renewed?", ResearchMode.QUESTION_ANSWER),
            ]
            with patch("bluesky_agent.intent.urllib.request.urlopen") as urlopen:
                for text, expected in cases:
                    with self.subTest(text=text):
                        decision = router.route(turn(text))
                        self.assertEqual(decision.mode, expected)
                        self.assertEqual(decision.source, "structural")
                urlopen.assert_not_called()

            fallback = IntentRouter.from_environment(make_config(Path(directory)), {}).route(
                turn("A connection between verification and observability.")
            )
            self.assertEqual(fallback.mode, ResearchMode.NOTE_EXPLORE)
            self.assertEqual(fallback.fallback_reason, "credentials_absent")

    def test_unqualified_reply_inherits_parent_mode_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = IntentRouter.from_environment(
                make_config(Path(directory)),
                {"OPENAI_BASE_URL": "https://manifest.example/v1", "OPENAI_API_KEY": "secret"},
            )
            with patch("bluesky_agent.intent.urllib.request.urlopen") as urlopen:
                decision = router.route(
                    turn("This also connects to the deployment story.", reply=True),
                    parent_mode=ResearchMode.QUESTION_ANSWER,
                )
            self.assertEqual(decision.mode, ResearchMode.QUESTION_ANSWER)
            self.assertEqual(decision.source, "inherited")
            urlopen.assert_not_called()


class ManifestRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = make_config(Path(self.directory.name), intent_model="auto")
        self.environment = {
            "OPENAI_BASE_URL": "https://manifest.example/v1",
            "OPENAI_API_KEY": "never-print-this",
        }
        self.router = IntentRouter.from_environment(self.config, self.environment)

    def test_valid_high_confidence_decision_records_distribution_and_model(self) -> None:
        calls: list[object] = []

        def fake_urlopen(request: object, *, timeout: float) -> FakeHTTPResponse:
            calls.append((request, timeout))
            return FakeHTTPResponse(completion())

        with patch("bluesky_agent.intent.urllib.request.urlopen", fake_urlopen):
            decision = self.router.route(turn("Compare consistency models across these systems."))

        self.assertEqual(decision.mode, ResearchMode.QUESTION_ANSWER)
        self.assertEqual(decision.confidence, 0.91)
        self.assertEqual(decision.probabilities[ResearchMode.NOTE_EXPLORE], 0.05)
        self.assertEqual(decision.source, "manifest")
        self.assertEqual(decision.model, "typesafe/jev")
        self.assertIsNone(decision.fallback_reason)
        self.assertEqual(len(calls), 1)

        request, timeout = calls[0]  # type: ignore[misc]
        self.assertEqual(timeout, 10.0)
        self.assertEqual(request.full_url, "https://manifest.example/v1/chat/completions")  # type: ignore[attr-defined]
        payload = json.loads(request.data)  # type: ignore[attr-defined]
        user_content = json.loads(payload["messages"][1]["content"])
        self.assertEqual(
            set(user_content),
            {"text", "has_url", "explicit_question", "source_directive", "is_reply"},
        )
        self.assertEqual(user_content["text"], "Compare consistency models across these systems.")
        self.assertNotIn("never-print-this", repr(self.router))
        self.assertNotIn("never-print-this", repr(decision))

    def test_low_confidence_falls_back_without_retry(self) -> None:
        with patch(
            "bluesky_agent.intent.urllib.request.urlopen",
            return_value=FakeHTTPResponse(completion(confidence=0.69)),
        ) as urlopen:
            decision = self.router.route(turn("A note about distributed ownership."))
        self.assertEqual(decision.mode, ResearchMode.NOTE_EXPLORE)
        self.assertEqual(decision.fallback_reason, "low_confidence")
        self.assertEqual(urlopen.call_count, 1)

    def test_malformed_diagnostic_and_transport_failures_fall_back_once(self) -> None:
        responses: list[object] = [
            {"model": "auto", "choices": [{"message": {"content": "not json"}}]},
            {"error": {"code": "M101", "message": "No provider connected"}},
            completion(model="openai/gpt-5"),
            completion(
                choice="NOTE_EXPLORE",
                probabilities={"SOURCE_BRIEF": 0.05, "QUESTION_ANSWER": 0.90, "NOTE_EXPLORE": 0.05},
            ),
            TimeoutError("gateway timed out"),
        ]
        expected = [
            "malformed_response",
            "gateway_diagnostic",
            "unexpected_model",
            "malformed_response",
            "gateway_failure",
        ]
        for result, reason in zip(responses, expected, strict=True):
            with self.subTest(reason=reason):
                effect = result if isinstance(result, BaseException) else None
                returned = None if effect is not None else FakeHTTPResponse(result)
                with patch(
                    "bluesky_agent.intent.urllib.request.urlopen",
                    return_value=returned,
                    side_effect=effect,
                ) as urlopen:
                    decision = self.router.route(turn("Explore a possible systems connection."))
                self.assertEqual(decision.mode, ResearchMode.NOTE_EXPLORE)
                self.assertEqual(decision.fallback_reason, reason)
                self.assertEqual(urlopen.call_count, 1)


class IntentConfigTests(unittest.TestCase):
    def test_toml_and_environment_names_parse_and_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.toml"
            path.write_text(
                "\n".join(
                    [
                        'operator_handle = "operator.example"',
                        'operator_did = "did:plc:operator"',
                        'agent_handle = "agent.example"',
                        'wiki_repo_url = "https://github.com/example/wiki.git"',
                        'wiki_site_url = "https://wiki.example"',
                        'intent_model = "typesafe/jev"',
                        "intent_timeout = 4.5",
                        "intent_confidence_threshold = 0.75",
                    ]
                ),
                encoding="utf-8",
            )
            config = Config.load(
                path,
                {
                    "BLUESKY_AGENT_APP_PASSWORD": "app-password",
                    "BLUESKY_AGENT_GITHUB_TOKEN": "github-token",
                    "BLUESKY_AGENT_INTENT_TIMEOUT": "2.25",
                },
            )
            self.assertEqual(config.intent_model, "typesafe/jev")
            self.assertEqual(config.intent_timeout, 2.25)
            self.assertEqual(config.intent_confidence_threshold, 0.75)

    def test_confidence_threshold_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "at most one"):
                make_config(Path(directory), intent_confidence_threshold=1.01)


if __name__ == "__main__":
    unittest.main()
