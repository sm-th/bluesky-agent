"""Shared domain records for the Bluesky research pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Turn:
    """One eligible operator-authored post in a research reply tree."""

    uri: str
    cid: str
    rkey: str
    text: str
    created_at: str
    parent_uri: str | None
    root_uri: str
    parent_turn_uri: str | None
    raw: dict


@dataclass(frozen=True)
class Publication:
    """Artifacts required to publish the agent's reply for one turn."""

    brief: str
    page_url: str
    image_path: Path
    alt_text: str
    session_file: Path
