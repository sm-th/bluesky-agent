"""Durable activation, lifecycle, and branch state backed by SQLite."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import Publication, Turn


class StateError(RuntimeError):
    """State is missing, inconsistent, or was concurrently changed."""


class Phase(str, Enum):
    """Exact durable lifecycle phases for a Research Turn."""

    ACCEPTED = "accepted"
    MARKED = "marked"
    RESEARCHED = "researched"
    SETTLED = "settled"


_PHASE_ORDER = {
    Phase.ACCEPTED: 0,
    Phase.MARKED: 1,
    Phase.RESEARCHED: 2,
    Phase.SETTLED: 3,
}


@dataclass(frozen=True)
class TurnState:
    turn: Turn
    phase: Phase
    attempts: int
    claim_token: str | None
    session_file: Path | None
    brief: str | None
    page_url: str | None
    image_path: Path | None
    alt_text: str | None
    reply_uri: str | None
    reply_cid: str | None
    last_error: str | None

    def publication(self) -> Publication:
        if not all(
            value is not None
            for value in (
                self.brief,
                self.page_url,
                self.image_path,
                self.alt_text,
                self.session_file,
            )
        ):
            raise StateError(f"turn {self.turn.uri} has incomplete publication state")
        return Publication(
            brief=self.brief,  # type: ignore[arg-type]
            page_url=self.page_url,  # type: ignore[arg-type]
            image_path=self.image_path,  # type: ignore[arg-type]
            alt_text=self.alt_text,  # type: ignore[arg-type]
            session_file=self.session_file,  # type: ignore[arg-type]
        )


class StateStore:
    """Transaction-safe storage for the single sequential worker.

    Claims are leased so a crashed process cannot permanently strand a turn.
    Every phase transition checks both the expected phase and claim token.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS activation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    activated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turns (
                    uri TEXT PRIMARY KEY,
                    cid TEXT NOT NULL,
                    rkey TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    parent_uri TEXT,
                    root_uri TEXT NOT NULL,
                    parent_turn_uri TEXT REFERENCES turns(uri),
                    raw_json TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (
                        phase IN ('accepted', 'marked', 'researched', 'settled')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    claim_until REAL,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    session_file TEXT,
                    brief TEXT,
                    page_url TEXT,
                    image_path TEXT,
                    alt_text TEXT,
                    reply_uri TEXT UNIQUE,
                    reply_cid TEXT,
                    inserted_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS turns_work_queue
                    ON turns(phase, next_attempt_at, created_at, uri);
                CREATE INDEX IF NOT EXISTS turns_root
                    ON turns(root_uri, created_at, uri);
                CREATE INDEX IF NOT EXISTS turns_parent
                    ON turns(parent_turn_uri);
                """
            )

    def activate(self, when: str | None = None) -> str:
        """Set Activation once; later calls are intentionally idempotent."""

        activated_at = when or _utc_now()
        _parse_timestamp(activated_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT activated_at FROM activation WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO activation(singleton, activated_at) VALUES (1, ?)",
                    (activated_at,),
                )
                result = activated_at
            else:
                result = str(row["activated_at"])
            connection.execute("COMMIT")
        return result

    def activation(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT activated_at FROM activation WHERE singleton = 1"
            ).fetchone()
        return str(row["activated_at"]) if row is not None else None

    def require_activation(self) -> str:
        activated_at = self.activation()
        if activated_at is None:
            raise StateError("agent is not activated; run the activate command first")
        return activated_at

    def accept(self, turn: Turn) -> bool:
        """Persist an eligible turn without changing an already accepted record."""

        now = _utc_now()
        raw_json = json.dumps(turn.raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO turns(
                    uri, cid, rkey, text, created_at, parent_uri, root_uri,
                    parent_turn_uri, raw_json, phase, inserted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.uri,
                    turn.cid,
                    turn.rkey,
                    turn.text,
                    turn.created_at,
                    turn.parent_uri,
                    turn.root_uri,
                    turn.parent_turn_uri,
                    raw_json,
                    Phase.ACCEPTED.value,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def get(self, uri: str) -> TurnState | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM turns WHERE uri = ?", (uri,)).fetchone()
        return _row_to_state(row) if row is not None else None

    def root_known(self, uri: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM turns WHERE uri = ? AND root_uri = uri AND parent_uri IS NULL",
                (uri,),
            ).fetchone()
        return row is not None

    def parent_turn_for_post(self, uri: str) -> str | None:
        """Map an Operator turn URI or an Agent reply URI to its turn."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT uri FROM turns
                WHERE uri = ? OR reply_uri = ?
                ORDER BY CASE WHEN uri = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (uri, uri, uri),
            ).fetchone()
        return str(row["uri"]) if row is not None else None

    def session_for_turn(self, uri: str) -> Path | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT phase, session_file FROM turns WHERE uri = ?", (uri,)
            ).fetchone()
        if row is None or row["phase"] != Phase.SETTLED.value or not row["session_file"]:
            return None
        return Path(str(row["session_file"]))

    def claim_next(
        self,
        *,
        lease_seconds: float,
        now: float | None = None,
        token: str | None = None,
    ) -> TurnState | None:
        """Atomically lease the oldest ready turn.

        A child is ready only after its exact parent turn has settled. Siblings
        therefore inherit the same settled parent independently.
        """

        timestamp = time.time() if now is None else now
        claim_token = token or secrets.token_urlsafe(24)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT child.uri
                FROM turns AS child
                LEFT JOIN turns AS parent ON parent.uri = child.parent_turn_uri
                WHERE child.phase != ?
                  AND child.next_attempt_at <= ?
                  AND (child.claim_token IS NULL OR child.claim_until <= ?)
                  AND (child.parent_turn_uri IS NULL OR parent.phase = ?)
                ORDER BY child.created_at ASC, child.uri ASC
                LIMIT 1
                """,
                (Phase.SETTLED.value, timestamp, timestamp, Phase.SETTLED.value),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            uri = str(row["uri"])
            cursor = connection.execute(
                """
                UPDATE turns
                SET claim_token = ?, claim_until = ?, attempts = attempts + 1,
                    updated_at = ?
                WHERE uri = ?
                  AND (claim_token IS NULL OR claim_until <= ?)
                """,
                (
                    claim_token,
                    timestamp + lease_seconds,
                    _utc_now(),
                    uri,
                    timestamp,
                ),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise StateError("turn claim lost a concurrent race")
            claimed = connection.execute("SELECT * FROM turns WHERE uri = ?", (uri,)).fetchone()
            connection.execute("COMMIT")
        if claimed is None:
            raise StateError("claimed turn disappeared")
        return _row_to_state(claimed)

    def mark_working(self, uri: str, claim_token: str) -> TurnState:
        return self._advance(uri, claim_token, Phase.ACCEPTED, Phase.MARKED, {})

    def record_publication(
        self,
        uri: str,
        claim_token: str,
        publication: Publication,
    ) -> TurnState:
        return self._advance(
            uri,
            claim_token,
            Phase.MARKED,
            Phase.RESEARCHED,
            {
                "brief": publication.brief,
                "page_url": publication.page_url,
                "image_path": str(publication.image_path),
                "alt_text": publication.alt_text,
                "session_file": str(publication.session_file),
            },
        )

    def settle(
        self,
        uri: str,
        claim_token: str,
        *,
        reply_uri: str,
        reply_cid: str,
    ) -> TurnState:
        return self._advance(
            uri,
            claim_token,
            Phase.RESEARCHED,
            Phase.SETTLED,
            {"reply_uri": reply_uri, "reply_cid": reply_cid},
            release=True,
        )

    def fail_claim(
        self,
        uri: str,
        claim_token: str,
        error: str,
        *,
        now: float | None = None,
    ) -> float:
        """Release a failed claim and schedule bounded exponential retry."""

        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts FROM turns WHERE uri = ? AND claim_token = ?",
                (uri, claim_token),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise StateError("cannot fail a claim that is not owned")
            delay = min(300.0, float(2 ** min(max(int(row["attempts"]) - 1, 0), 8)))
            connection.execute(
                """
                UPDATE turns
                SET claim_token = NULL, claim_until = NULL, next_attempt_at = ?,
                    last_error = ?, updated_at = ?
                WHERE uri = ? AND claim_token = ?
                """,
                (timestamp + delay, error[-4000:], _utc_now(), uri, claim_token),
            )
            connection.execute("COMMIT")
        return delay

    def list_turns(self, *, limit: int = 100) -> list[TurnState]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM turns ORDER BY created_at DESC, uri DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_state(row) for row in rows]

    def counts(self) -> dict[str, int]:
        result = {phase.value: 0 for phase in Phase}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT phase, COUNT(*) AS count FROM turns GROUP BY phase"
            ).fetchall()
        for row in rows:
            result[str(row["phase"])] = int(row["count"])
        return result

    def check(self) -> None:
        with self._connect() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise StateError(f"SQLite integrity check failed: {result[0] if result else 'no result'}")

    def _advance(
        self,
        uri: str,
        claim_token: str,
        expected: Phase,
        target: Phase,
        values: Mapping[str, Any],
        *,
        release: bool = False,
    ) -> TurnState:
        if _PHASE_ORDER[target] != _PHASE_ORDER[expected] + 1:
            raise StateError(f"invalid lifecycle transition: {expected.value} -> {target.value}")
        allowed = {
            "brief",
            "page_url",
            "image_path",
            "alt_text",
            "session_file",
            "reply_uri",
            "reply_cid",
        }
        unknown = set(values) - allowed
        if unknown:
            raise StateError(f"invalid state columns: {', '.join(sorted(unknown))}")
        assignments = ["phase = ?", "last_error = NULL", "next_attempt_at = 0", "updated_at = ?"]
        parameters: list[Any] = [target.value, _utc_now()]
        for name, value in values.items():
            assignments.append(f"{name} = ?")
            parameters.append(value)
        if release:
            assignments.extend(["claim_token = NULL", "claim_until = NULL"])
        parameters.extend([uri, claim_token, expected.value])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE turns SET {', '.join(assignments)}
                WHERE uri = ? AND claim_token = ? AND phase = ?
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise StateError(
                    f"turn {uri} is not owned in expected phase {expected.value}"
                )
            row = connection.execute("SELECT * FROM turns WHERE uri = ?", (uri,)).fetchone()
            connection.execute("COMMIT")
        if row is None:
            raise StateError("advanced turn disappeared")
        return _row_to_state(row)


def _row_to_state(row: sqlite3.Row) -> TurnState:
    raw = json.loads(str(row["raw_json"]))
    if not isinstance(raw, dict):
        raise StateError(f"turn {row['uri']} has invalid raw JSON")
    turn = Turn(
        uri=str(row["uri"]),
        cid=str(row["cid"]),
        rkey=str(row["rkey"]),
        text=str(row["text"]),
        created_at=str(row["created_at"]),
        parent_uri=str(row["parent_uri"]) if row["parent_uri"] is not None else None,
        root_uri=str(row["root_uri"]),
        parent_turn_uri=(
            str(row["parent_turn_uri"]) if row["parent_turn_uri"] is not None else None
        ),
        raw=raw,
    )
    return TurnState(
        turn=turn,
        phase=Phase(str(row["phase"])),
        attempts=int(row["attempts"]),
        claim_token=str(row["claim_token"]) if row["claim_token"] else None,
        session_file=Path(str(row["session_file"])) if row["session_file"] else None,
        brief=str(row["brief"]) if row["brief"] is not None else None,
        page_url=str(row["page_url"]) if row["page_url"] is not None else None,
        image_path=Path(str(row["image_path"])) if row["image_path"] else None,
        alt_text=str(row["alt_text"]) if row["alt_text"] is not None else None,
        reply_uri=str(row["reply_uri"]) if row["reply_uri"] else None,
        reply_cid=str(row["reply_cid"]) if row["reply_cid"] else None,
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError(f"invalid activation timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise StateError("activation timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)
