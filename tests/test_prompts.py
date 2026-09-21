from __future__ import annotations

import pytest

from bluesky_agent.intent import ResearchMode
from bluesky_agent.models import Turn
from bluesky_agent.prompts import RESEARCH_PAGE_MAX_WORDS, build_research_prompt


@pytest.fixture
def turn() -> Turn:
    return Turn(
        uri="at://did:plc:operator/app.bsky.feed.post/turn-1",
        cid="bafy-turn-1",
        rkey="turn-1",
        text="Research this.",
        created_at="2026-09-21T12:00:00Z",
        parent_uri=None,
        root_uri="at://did:plc:operator/app.bsky.feed.post/turn-1",
        parent_turn_uri=None,
        raw={},
    )


@pytest.mark.parametrize(
    ("mode", "heading", "mode_instruction"),
    (
        (
            ResearchMode.SOURCE_BRIEF,
            "## Source brief",
            "keep evidence separate from caveats",
        ),
        (
            ResearchMode.QUESTION_ANSWER,
            "## Answer",
            "Directly resolve the Research Turn's question",
        ),
        (
            ResearchMode.NOTE_EXPLORE,
            "## Exploration",
            "test it against preserved evidence",
        ),
    ),
)
def test_research_prompt_applies_mode_schema_and_writing_contract(
    turn: Turn,
    mode: ResearchMode,
    heading: str,
    mode_instruction: str,
) -> None:
    prompt = build_research_prompt(turn, mode, operator_handle="operator.test")

    assert "front matter must contain exactly `title`, `rkey`, `date`, `brief`, `turn_url`, and `mode`" in prompt
    assert f"`mode: {mode.value}`" in prompt
    assert f"begin with exactly `{heading}`" in prompt
    assert mode_instruction in prompt
    assert "Target 140-180 words" in prompt
    assert f"at most {RESEARCH_PAGE_MAX_WORDS} words" in prompt
    assert "at most two compact sections" in prompt
    assert "Preserve every selected linked source" in prompt
    assert "Attach claim-level citations" in prompt
    assert "All prose you generate or update must be English" in prompt
    assert "BRIEF_REPLY: <a direct English answer to the turn's substance>" in prompt
