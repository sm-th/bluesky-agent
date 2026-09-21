from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from bluesky_agent.models import Turn
from bluesky_agent.research import (
    ResearchError,
    grapheme_len,
    parse_brief_reply,
    validate_bluesky_reply,
    validate_research_page,
    validate_changed_wiki_artifacts,
)


def _turn() -> Turn:
    return Turn(
        uri="at://did:plc:operator/app.bsky.feed.post/3abc",
        cid="bafy-cid",
        rkey="3abc",
        text="What does the evidence show?",
        created_at="2026-09-21T00:00:00Z",
        parent_uri=None,
        root_uri="at://did:plc:operator/app.bsky.feed.post/3abc",
        parent_turn_uri=None,
        raw={"$type": "app.bsky.feed.post", "text": "What does the evidence show?"},
    )


def _page(brief: str = "The evidence supports the narrower claim.") -> str:
    return f'''---
title: A narrow answer
rkey: 3abc
date: 2026-09-21T00:00:00Z
brief: {brief}
turn_url: https://bsky.app/profile/operator.example/post/3abc
---

## Answer

The evidence supports the narrower claim ([S1](../sources/source/)).
'''



def test_brief_output_must_be_one_direct_english_line() -> None:
    assert parse_brief_reply("BRIEF_REPLY: The central claim is unsupported.") == "The central claim is unsupported."

    for malformed in (
        "Here is the result\nBRIEF_REPLY: Direct answer.",
        "BRIEF_REPLY: I researched the subject.",
        "BRIEF_REPLY: 詳細な答えです。",
        "BRIEF_REPLY: See https://example.com",
    ):
        with pytest.raises(ResearchError):
            parse_brief_reply(malformed)


def test_reply_limit_counts_combining_and_joined_emoji_as_graphemes() -> None:
    assert grapheme_len("e\u0301") == 1
    assert grapheme_len("👩\u200d🔬") == 1
    validate_bluesky_reply("a" * 260, "https://w.example/x/")
    with pytest.raises(ResearchError, match="300"):
        validate_bluesky_reply("a" * 290, "https://w.example/long/")


def test_research_page_validates_schema_brief_language_and_size(tmp_path: Path) -> None:
    page = tmp_path / "wiki" / "research" / "3abc.md"
    page.parent.mkdir(parents=True)
    source = tmp_path / "wiki" / "sources" / "source.md"
    source.parent.mkdir()
    source.write_text("Original URL: https://example.com\\nPreserved evidence: raw/linked/source.md\\n", encoding="utf-8")
    brief = "The evidence supports the narrower claim."
    page.write_text(_page(brief), encoding="utf-8")

    validate_research_page(page, _turn(), brief, operator_handle="operator.example")

    page.write_text(_page(brief).replace("The evidence supports", "証拠は支持する"), encoding="utf-8")
    with pytest.raises(ResearchError, match="non-English"):
        validate_research_page(page, _turn(), brief, operator_handle="operator.example")


def test_research_page_rejects_missing_page_contract(tmp_path: Path) -> None:
    page = tmp_path / "wiki" / "research" / "3abc.md"
    page.parent.mkdir(parents=True)
    page.write_text(_page().replace("## Answer", "## Notes"), encoding="utf-8")

    with pytest.raises(ResearchError, match="Answer section"):
        validate_research_page(
            page,
            _turn(),
            "The evidence supports the narrower claim.",
            operator_handle="operator.example",
        )


def test_research_page_rejects_frontmatter_engine_override(tmp_path: Path) -> None:
    page = tmp_path / "wiki" / "research" / "3abc.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        _page().replace("title:", "templateEngineOverride: njk,md\ntitle:"),
        encoding="utf-8",
    )

    with pytest.raises(ResearchError, match="frontmatter is not allowed"):
        validate_research_page(
            page,
            _turn(),
            "The evidence supports the narrower claim.",
            operator_handle="operator.example",
        )


@pytest.mark.parametrize(
    "payload",
    (
        "<script>document.body.textContent = 'replaced'</script>",
        "Encoded script text: &#x0627;",
    ),
)
def test_published_markdown_rejects_rendered_content_bypasses(
    tmp_path: Path, payload: str
) -> None:
    relative = PurePosixPath("wiki", "sources", "hostile.md")
    target = tmp_path.joinpath(*relative.parts)
    target.parent.mkdir(parents=True)
    target.write_text(payload, encoding="utf-8")

    class Repository:
        path = tmp_path

        @staticmethod
        def changed_paths() -> list[tuple[str, PurePosixPath]]:
            return [("??", relative)]

    with pytest.raises(ResearchError):
        validate_changed_wiki_artifacts(Repository())
