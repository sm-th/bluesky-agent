from __future__ import annotations

import json
import struct
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from bluesky_agent.atproto import AtprotoClient, HTTPResponse, RecordRef
from bluesky_agent.config import Config
from bluesky_agent.models import Publication, Turn
from bluesky_agent.runner import Runner
from bluesky_agent.state import Phase, StateStore


OPERATOR_DID = "did:plc:operator"
AGENT_DID = "did:plc:agent"
ROOT_URI = f"at://{OPERATOR_DID}/app.bsky.feed.post/root"


def make_config(root: Path) -> Config:
    return Config(
        operator_handle="operator.example",
        operator_did=OPERATOR_DID,
        agent_handle="agent.example",
        agent_app_password="app-password",
        bluesky_service="https://bsky.example",
        bluesky_public_api="https://public.example",
        wiki_repo_url="https://github.com/example/wiki.git",
        wiki_site_url="https://wiki.example",
        state_dir=root / "state",
        poll_interval=1,
        omp_model="test-model",
        omp_timeout=30,
        wiki_ready_timeout=5,
        wiki_ready_interval=0.1,
        chromium_bin="chromium",
        git_user_name="Research Agent",
        git_user_email="agent@example.com",
        github_token="github-token",
    )


def feed_post(
    rkey: str,
    created_at: str,
    *,
    indexed_at: str | None = None,
    parent_uri: str | None = None,
    root_uri: str | None = None,
    author_did: str = OPERATOR_DID,
) -> dict[str, Any]:
    uri = f"at://{author_did}/app.bsky.feed.post/{rkey}"
    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": f"Research {rkey}",
        "createdAt": created_at,
    }
    if parent_uri is not None:
        record["reply"] = {
            "parent": {"uri": parent_uri, "cid": f"cid-parent-{rkey}"},
            "root": {"uri": root_uri, "cid": "cid-root"},
        }
    return {
        "post": {
            "uri": uri,
            "cid": f"cid-{rkey}",
            "author": {"did": author_did},
            "record": record,
            **({"indexedAt": indexed_at} if indexed_at is not None else {}),
        }
    }


class FakeClient:
    def __init__(self) -> None:
        self.feed: list[dict[str, Any]] = []
        self.ancestry: dict[str, list[dict[str, str]]] = {}
        self.likes: list[str] = []
        self.replies: list[str] = []

    def author_feed(self, actor: str, *, since: str) -> list[dict[str, Any]]:
        return list(self.feed)

    def thread_ancestry(self, uri: str) -> list[dict[str, str]]:
        return self.ancestry.get(uri, [])

    def ensure_like(self, subject: RecordRef) -> RecordRef:
        self.likes.append(subject.uri)
        return RecordRef(self.agent_reply_uri(subject.uri, "like"), "like-cid")

    def ensure_image_reply(self, *, parent: RecordRef, **kwargs: Any) -> RecordRef:
        self.replies.append(parent.uri)
        return RecordRef(self.agent_reply_uri(parent.uri), f"reply-cid-{parent.uri.rsplit('/', 1)[-1]}")

    @staticmethod
    def agent_reply_uri(turn_uri: str, kind: str = "reply") -> str:
        return f"at://{AGENT_DID}/app.bsky.feed.{kind}/{turn_uri.rsplit('/', 1)[-1]}"


class FakePipeline:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, Path | None]] = []
        self.image = root / "page.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 1200, 1600))

    def run(self, turn: Turn, parent_session: Path | None) -> Publication:
        self.calls.append((turn.uri, parent_session))
        session = self.root / f"{turn.rkey}.session.json"
        session.write_text("{}", encoding="utf-8")
        return Publication(
            brief=f"Answer for {turn.rkey}",
            page_url=f"https://wiki.example/research/{turn.rkey}/",
            image_path=self.image,
            alt_text=f"Expanded answer for {turn.rkey}",
            session_file=session,
        )


class RunnerEligibilityTests(unittest.TestCase):
    def test_activation_cutoff_uses_publication_time_not_record_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = StateStore(root / "state.sqlite3")
            state.activate("2026-09-21T12:00:00Z")
            client = FakeClient()
            client.feed = [
                feed_post(
                    "old",
                    "2026-09-22T12:00:00Z",
                    indexed_at="2026-09-21T11:59:59Z",
                ),
                feed_post(
                    "equal",
                    "2026-09-22T12:00:00Z",
                    indexed_at="2026-09-21T12:00:00Z",
                ),
                feed_post(
                    "new",
                    "2026-09-20T12:00:00Z",
                    indexed_at="2026-09-21T12:00:01Z",
                ),
            ]
            runner = Runner(
                make_config(root),
                state=state,
                client=client,
                pipeline=FakePipeline(root),
            )

            self.assertEqual(runner.poll(), 1)
            self.assertIsNone(state.get(f"at://{OPERATOR_DID}/app.bsky.feed.post/old"))
            self.assertIsNone(state.get(f"at://{OPERATOR_DID}/app.bsky.feed.post/equal"))
            self.assertIsNotNone(state.get(f"at://{OPERATOR_DID}/app.bsky.feed.post/new"))

    def test_only_known_roots_are_eligible_and_sibling_sessions_stay_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = StateStore(root / "state.sqlite3")
            state.activate("2026-09-21T12:00:00Z")
            client = FakeClient()
            pipeline = FakePipeline(root)
            runner = Runner(make_config(root), state=state, client=client, pipeline=pipeline)

            root_item = feed_post("root", "2026-09-21T12:00:01Z")
            unknown = feed_post(
                "outside",
                "2026-09-21T12:00:02Z",
                parent_uri="at://did:plc:other/app.bsky.feed.post/parent",
                root_uri="at://did:plc:other/app.bsky.feed.post/root",
            )
            client.feed = [unknown, root_item]
            first = runner.run_once()
            self.assertEqual(first.settled, 1)
            self.assertIsNone(state.get(f"at://{OPERATOR_DID}/app.bsky.feed.post/outside"))

            root_reply = client.agent_reply_uri(ROOT_URI)
            child_a = feed_post(
                "child-a",
                "2026-09-21T12:00:03Z",
                parent_uri=root_reply,
                root_uri=ROOT_URI,
            )
            child_b = feed_post(
                "child-b",
                "2026-09-21T12:00:04Z",
                parent_uri=root_reply,
                root_uri=ROOT_URI,
            )
            client.feed = [root_item, child_b, unknown, child_a]
            second = runner.run_once()
            self.assertEqual(second.settled, 2)

            child_a_uri = child_a["post"]["uri"]
            child_b_uri = child_b["post"]["uri"]
            grandchild = feed_post(
                "grandchild-a",
                "2026-09-21T12:00:05Z",
                parent_uri=client.agent_reply_uri(child_a_uri),
                root_uri=ROOT_URI,
            )
            client.feed = [root_item, child_a, child_b, grandchild]
            third = runner.run_once()
            self.assertEqual(third.settled, 1)

            root_session = root / "root.session.json"
            child_a_session = root / "child-a.session.json"
            self.assertEqual(
                pipeline.calls,
                [
                    (ROOT_URI, None),
                    (child_a_uri, root_session),
                    (child_b_uri, root_session),
                    (grandchild["post"]["uri"], child_a_session),
                ],
            )


class StateResumeTests(unittest.TestCase):
    def test_researched_phase_resumes_at_reply_without_reliking_or_researching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = StateStore(root / "state.sqlite3")
            state.activate("2026-09-21T12:00:00Z")
            turn = Turn(
                uri=ROOT_URI,
                cid="cid-root",
                rkey="root",
                text="Research root",
                created_at="2026-09-21T12:00:01Z",
                parent_uri=None,
                root_uri=ROOT_URI,
                parent_turn_uri=None,
                raw={},
            )
            self.assertTrue(state.accept(turn))
            claimed = state.claim_next(lease_seconds=1, now=0, token="crashed-worker")
            self.assertIsNotNone(claimed)
            state.mark_working(ROOT_URI, "crashed-worker")
            image = root / "page.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 1200, 1600))
            session = root / "root.session.json"
            state.record_publication(
                ROOT_URI,
                "crashed-worker",
                Publication("Brief", "https://wiki.example/root/", image, "Alt text", session),
            )

            class PipelineMustNotRun:
                def run(self, turn: Turn, parent_session: Path | None) -> Publication:
                    raise AssertionError("research was already durable")

            client = FakeClient()
            runner = Runner(
                make_config(root),
                state=state,
                client=client,
                pipeline=PipelineMustNotRun(),
            )
            report = runner.run_once()

            self.assertEqual(report.failures, ())
            self.assertEqual(client.likes, [])
            self.assertEqual(client.replies, [ROOT_URI])
            self.assertEqual(state.get(ROOT_URI).phase, Phase.SETTLED)  # type: ignore[union-attr]


class MemoryTransport:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.create_calls = 0
        self.upload_calls = 0

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HTTPResponse:
        parsed = urllib.parse.urlparse(url)
        nsid = parsed.path.rsplit("/", 1)[-1]
        if nsid == "com.atproto.server.createSession":
            return self.response(
                {
                    "did": AGENT_DID,
                    "handle": "agent.example",
                    "accessJwt": "access",
                    "refreshJwt": "refresh",
                }
            )
        if nsid == "com.atproto.repo.getRecord":
            query = urllib.parse.parse_qs(parsed.query)
            key = (query["collection"][0], query["rkey"][0])
            record = self.records.get(key)
            if record is None:
                return self.response({"error": "RecordNotFound", "message": "missing"}, status=400)
            return self.response(record)
        if nsid == "com.atproto.repo.uploadBlob":
            self.upload_calls += 1
            return self.response(
                {"blob": {"$type": "blob", "ref": {"$link": "bafy-image"}, "mimeType": "image/png", "size": len(body or b"")}}
            )
        if nsid == "com.atproto.repo.createRecord":
            assert body is not None
            payload = json.loads(body)
            key = (payload["collection"], payload["rkey"])
            uri = f"at://{AGENT_DID}/{payload['collection']}/{payload['rkey']}"
            self.records[key] = {"uri": uri, "cid": f"cid-{payload['rkey']}", "value": payload["record"]}
            self.create_calls += 1
            raise TimeoutError("response lost after record creation")
        raise AssertionError(f"unexpected endpoint {nsid}")

    @staticmethod
    def response(payload: dict[str, Any], *, status: int = 200) -> HTTPResponse:
        return HTTPResponse(status, json.dumps(payload).encode("utf-8"), {})


class AtprotoIdempotencyTests(unittest.TestCase):
    def test_lost_create_responses_reconcile_without_duplicate_records_or_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = MemoryTransport()
            client = AtprotoClient(
                service="https://bsky.example",
                public_api="https://public.example",
                identifier="agent.example",
                app_password="secret",
                transport=transport,
            )
            subject = RecordRef(ROOT_URI, "cid-root")

            first_like = client.ensure_like(subject)
            second_like = client.ensure_like(subject)
            self.assertEqual(first_like, second_like)
            self.assertEqual(transport.create_calls, 1)

            image = Path(directory) / "page.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 1200, 1600))
            arguments = {
                "rkey_seed": ROOT_URI,
                "parent": subject,
                "root": subject,
                "brief": "A concise answer.",
                "page_url": "https://wiki.example/root/",
                "image_path": image,
                "alt_text": "Expanded answer",
            }
            first_reply = client.ensure_image_reply(**arguments)
            second_reply = client.ensure_image_reply(**arguments)

            self.assertEqual(first_reply, second_reply)
            self.assertEqual(transport.create_calls, 2)
            self.assertEqual(transport.upload_calls, 1)
            self.assertEqual(len(transport.records), 2)


if __name__ == "__main__":
    unittest.main()
