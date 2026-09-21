# Karpathy's LLM Wiki: Research and Project Guidance

## Scope and source authority

This note uses only:

- Andrej Karpathy's original **“LLM Knowledge Bases”** post and his replies about how he uses the workflow ([X, April 2, 2026](https://x.com/karpathy/status/2039805659525644595)).
- Karpathy's follow-up explaining that the linked gist is an intentionally abstract “idea file” for an agent to customize ([X, April 4, 2026](https://x.com/karpathy/status/2040470801506541998)).
- Karpathy's canonical **“LLM Wiki”** idea file ([GitHub Gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f); [raw text](https://gist.githubusercontent.com/karpathy/442a6bf555914893e9891c11519de94f/raw)).
- [`tobi/qmd`](https://github.com/tobi/qmd), the optional search implementation linked directly by the gist. This is canonical material for that tool, but it is not authored by Karpathy and is not required by the LLM Wiki pattern.

Karpathy explicitly presents the gist as an abstract pattern rather than a finished application or fixed specification. Directory names, page templates, metadata, citation syntax, and rebuild semantics below are therefore project decisions unless identified as sourced facts.

## Sourced facts from Karpathy

### 1. Core model and information architecture

The central contrast is with query-time RAG. RAG repeatedly retrieves raw fragments and reconstructs an answer; an LLM Wiki instead **incrementally compiles a persistent, structured, interlinked Markdown artifact** and keeps that synthesis current as sources and questions accumulate ([canonical gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)).

Karpathy identifies three layers:

1. **Raw sources**: a curated, immutable collection of articles, papers, images, data, repositories, and similar material. The agent may read but must not modify this layer; it is the source of truth.
2. **Wiki**: LLM-generated Markdown such as source summaries, entity pages, concept pages, comparisons, an overview, and synthesis pages. The LLM owns creation and maintenance; the human primarily reads it.
3. **Schema/instructions**: a file such as `AGENTS.md` or `CLAUDE.md` defining structure, conventions, and ingest/query/maintenance workflows. Karpathy calls this the key configuration and says the human and agent co-evolve it.

The original post describes essentially the same pipeline as source documents in `raw/` followed by an incrementally compiled directory of `.md` files containing summaries, backlinks, concept categorization, and interlinked articles ([original post](https://x.com/karpathy/status/2039805659525644595)).

Two navigation files have separate purposes in the gist:

- `index.md` is a content catalog. It lists each wiki page with a link and one-line summary, optionally adding metadata such as date or source count, and groups pages by category. The agent updates it on every ingest and reads it first when answering a query.
- `log.md` is an append-only chronological record of ingests, queries, and lint passes. Consistent headings make it machine-readable with simple tools.

Karpathy does **not** prescribe a canonical directory tree, taxonomy, filename scheme, page template, frontmatter schema, or link syntax.

### 2. Page lifecycle

The example ingest flow is: read a newly added raw source, discuss its takeaways with the human, create a source summary, update the index, revise relevant entity and concept pages, and append a log entry. One source can update 10–15 pages ([canonical gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)).

This process is incremental rather than append-only at the page level. New evidence may revise topic summaries, strengthen or challenge a synthesis, expose contradictions, or supersede stale claims. Periodic “lint” passes look for contradictions, superseded claims, orphan pages, important concepts without pages, missing cross-references, and gaps that further research could fill ([canonical gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)).

Karpathy's own process was not fully autonomous: he said he manually added sources one at a time and stayed closely involved, especially while the structure was young. Once the pattern stabilized, filing a marginal document became easier ([reply in the original post](https://x.com/karpathy/status/2039805659525644595)).

Useful query results—comparisons, analyses, and newly discovered connections—can be filed back into the wiki so investigation compounds rather than disappearing in chat history ([canonical gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)). The sources do not define formal draft, review, archive, merge, deletion, or supersession states.

### 3. Linking and navigation

Cross-references are part of the compiled knowledge, not something rediscovered for each question. Ingest updates related pages and their links; linting checks for missing cross-references and pages with no inbound links. Obsidian's graph view is suggested as a way to see hubs and orphans ([canonical gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)).

At moderate scale, Karpathy says an index-first workflow can work without embedding infrastructure. The gist gives the rough range as about 100 sources and hundreds of pages; the original post describes one of his wikis as roughly 100 articles and 400,000 words ([gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f); [original post](https://x.com/karpathy/status/2039805659525644595)).

The sources do not select Markdown links versus Obsidian wikilinks, stable IDs versus title-derived slugs, or rename/link-rewrite rules.

### 4. Citations and provenance

The gist says query answers should be synthesized **with citations**, and it defines immutable raw inputs as the source of truth ([canonical gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)). It does not specify:

- citation syntax;
- whether every factual claim on every page needs a citation;
- source IDs, locators, quotation requirements, or bibliographies;
- how citations survive page rewrites; or
- how to distinguish source-backed statements from agent inference.

Consequently, claim-level provenance is consistent with Karpathy's architecture but is an implementation choice, not a rule stated by him.

### 5. Source preservation

The raw layer is explicitly immutable and authoritative. For web material, Karpathy recommends converting articles to Markdown and downloading related images locally so an agent can inspect them and the knowledge base does not depend on image URLs remaining available ([gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f); [original post](https://x.com/karpathy/status/2039805659525644595)). He also notes that an agent may need to read Markdown text and inspect referenced images separately.

The gist describes the wiki as a Git repository of Markdown, gaining version history, branching, and collaboration. It does not prescribe acquisition timestamps, content hashes, deduplication, MIME preservation, licensing metadata, or handling for upstream edits/deletions.

### 6. Agent instructions and division of responsibility

Karpathy intends the idea file to be pasted into an agent such as Codex, Claude Code, or OpenCode/Pi, after which that agent and the user instantiate a domain-specific version ([follow-up post](https://x.com/karpathy/status/2040470801506541998); [canonical gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)). The durable repository instruction file should teach the agent the wiki's structure, conventions, and operational workflows.

The role split is explicit: the human curates sources, directs analysis, asks questions, and interprets results; the LLM summarizes, cross-references, files, and performs maintenance. Karpathy summarizes this as “Obsidian is the IDE; the LLM is the programmer; the wiki is the codebase” ([canonical gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)). The sources provide no complete production prompt or tool-permission policy.

### 7. Search and rebuild boundaries

Search infrastructure is optional. Karpathy says `index.md` can suffice at small scale and directly links [`qmd`](https://github.com/tobi/qmd) as a later option. Its repository describes local hybrid BM25/vector search with LLM reranking, CLI output for agents, and an MCP server. This is an optional scaling tool, not part of the minimum architecture.

Most importantly for this project, **Karpathy does not define a clean rebuild**. The sources specify no rebuild command, deterministic replay, idempotency guarantee, cache invalidation, atomic replacement, or rule for retaining query-derived pages. A rebuild is conceptually enabled by separating immutable raw inputs and instructions from an LLM-owned generated wiki, but equivalent output is not guaranteed: ingestion can include human guidance, instructions can evolve, query outputs can become pages, and LLM generation is not claimed to be deterministic.

## Implementation recommendations for the Bluesky research agent

Everything in this section is a project recommendation derived from—but not asserted by—the sources above.

### Information architecture

Keep authored inputs and generated knowledge physically distinct:

```text
raw/
  bluesky/       # immutable snapshots of triggering posts/threads
  linked/        # immutable snapshots of external sources
  assets/        # downloaded media referenced by snapshots
  manifest/      # one immutable provenance record per captured source
wiki/            # fully generated; safe to replace
  sources/
  entities/
  concepts/
  syntheses/
  index.md
  log.md
AGENTS.md         # durable structure and workflow contract
```

Use the Bluesky record identifier, canonical URL, capture time, author identity, source type, content hash, and links to locally saved assets in each raw manifest. Use stable source IDs that do not depend on mutable titles. Treat a changed upstream record as a new captured version rather than editing its previous snapshot.

Make page classes purposeful:

- **Source pages** summarize exactly one preserved source and point back to its raw snapshot.
- **Entity pages** consolidate claims about a person, organization, project, product, or dataset.
- **Concept pages** explain themes and relationships across sources.
- **Synthesis pages** answer research questions across multiple sources and label analytical inference explicitly.

### Trigger-to-ingest lifecycle

A Bluesky trigger should enqueue research; it should not permit transient network content to mutate the wiki directly. Process each trigger as a small transaction:

1. Capture the triggering post, relevant thread context, linked materials, metadata, and accessible media into `raw/`.
2. Record failures or unavailable linked content rather than silently omitting it.
3. Read `wiki/index.md`, then inspect only the related pages and preserved sources.
4. Create or update the source page and all affected entity, concept, and synthesis pages.
5. Preserve contradictions. Do not overwrite an older claim merely because a newer source differs; describe the disagreement and cite both.
6. Refresh inbound/outbound links and the index.
7. Append one structured log entry containing the trigger/source IDs and the pages affected.
8. Run focused health checks on the changed neighborhood before publishing it.

Start with one-trigger-at-a-time, human-reviewed ingestion. Increase autonomy only after page shapes and citation behavior are stable, matching Karpathy's reported early-stage practice.

### Linking rules

Use portable relative Markdown links and stable filenames. Keep a page's display title separate from its filename so a title edit does not break links. Each source page should link to the concepts/entities it supports, and each generated knowledge page should link back to its source pages. Lint for broken links, orphans, missing reciprocal context, and concepts repeatedly mentioned without a dedicated page.

Do not create a page merely to eliminate an orphan warning. A page should represent a durable source, entity, concept, or synthesis; otherwise merge its content into the closest durable page.

### Citation contract

Require citations at the claim or paragraph level, not only in a page-level bibliography. A citation should identify:

- the preserved raw source ID/path;
- the original canonical URL or Bluesky record identifier;
- a useful locator such as post ID, section heading, paragraph, timestamp, or quoted span; and
- the captured version when more than one snapshot exists.

Distinguish three kinds of prose:

- **Source-backed fact**: must cite preserved evidence.
- **Cross-source synthesis**: must cite every source necessary to support it.
- **Agent inference or recommendation**: must be visibly labeled and must not masquerade as a sourced claim.

Any web search used to fill a gap must first preserve the selected result in `raw/linked/`; the agent must not cite a search-result snippet or unarchived browsing session. When a query result is filed into the wiki, retain its citations and inference labels.

### Source preservation rules

Make `raw/` append-only to the research agent. Never normalize in place, rewrite captured text, or replace an old snapshot with a fresher fetch. Save the original response or faithful text representation alongside normalized Markdown when practical. Download important media locally and record its relationship to the parent source. Record capture errors, redirects, deletions, and access limitations in provenance rather than inventing missing content.

A source snapshot and its manifest should be sufficient to audit a generated claim without reaching the live network. Git history is useful defense in depth, but it is not a substitute for immutable-source rules and explicit provenance.

### Durable agent prompt/schema

`AGENTS.md` should encode invariants rather than a one-shot prose request. It should tell the agent:

- which directories it may write and which are immutable;
- the page classes and templates;
- the exact ingest, query, lint, and rebuild workflows;
- the citation and inference-labeling contract;
- how to handle contradictions, updates, unavailable content, and duplicate triggers;
- how and when to update `index.md` and `log.md`;
- health checks required before publishing; and
- when human review is mandatory.

Keep domain vocabulary and examples in the schema so later sessions reproduce the established structure. Version instruction changes because a rebuild under a different schema may produce materially different pages.

### Clean replacement and rebuild strategy

Treat `wiki/` as a disposable build artifact. For this project, perform a **clean replacement, not a migration**:

1. Freeze the input set: immutable raw snapshots, manifests, and the chosen version of `AGENTS.md`.
2. Build into a new staging directory from those inputs only. Do not read, copy, translate, or “improve” pages from the old wiki.
3. Process sources in a stable order and record the prompt/schema version and model/tool configuration. This improves repeatability without claiming deterministic LLM output.
4. Regenerate source, entity, concept, synthesis, index, and log pages. If a query-derived insight must survive clean rebuilds, preserve it first as an explicitly labeled input artifact with its question, evidence, and provenance; otherwise accept that it is disposable.
5. Before replacement, verify source coverage, citations, internal links, index coverage, duplicate stable IDs, and contradictions that lack explicit treatment.
6. Replace the published wiki only after the staged build passes those checks; retain Git history for comparison and rollback, not as a migration input.

Make incremental ingest and clean rebuild converge on the same documented page contracts, but do not require byte-identical prose. Define success through observable invariants—source coverage, traceable claims, valid links, complete indexing, and explicit conflicts—rather than exact generated wording.

Add full-text or hybrid search only when index-first navigation measurably stops finding the right pages. If that point arrives, expose search through a narrow agent-facing CLI or MCP interface; [`qmd`](https://github.com/tobi/qmd) is the directly linked reference option.

## Actionable checklist for the Bluesky research agent

- [ ] Archive the triggering Bluesky post, relevant thread context, linked sources, metadata, and media before editing the wiki.
- [ ] Assign stable source IDs and immutable versioned snapshots; never rewrite prior raw captures.
- [ ] Record canonical URL/record ID, author, capture time, source type, content hash, and capture limitations in a manifest.
- [ ] Read `wiki/index.md` first, then inspect the relevant existing pages and preserved sources.
- [ ] Create or update the source summary plus every materially affected entity, concept, and synthesis page.
- [ ] Cite every factual claim to preserved evidence with a useful locator and captured version.
- [ ] Label cross-source synthesis, uncertainty, contradiction, and agent inference explicitly.
- [ ] Preserve conflicting claims with citations rather than silently choosing or overwriting one.
- [ ] Maintain portable links in both directions and check the changed neighborhood for broken links, orphans, and missing concept pages.
- [ ] Update `wiki/index.md` and append one structured `wiki/log.md` entry for each ingest, query filing, lint pass, or rebuild.
- [ ] Preserve any research output that must survive rebuild as an explicit provenance-bearing input; leave all other generated pages disposable.
- [ ] For a rebuild, generate a fresh staged `wiki/` from immutable inputs and the versioned schema only; never migrate content from the old wiki.
- [ ] Validate source coverage, claim citations, stable-ID uniqueness, internal links, index completeness, and contradiction handling before publishing.
- [ ] Keep ingestion human-reviewed while conventions are still changing; expand autonomy only after repeated clean runs.
- [ ] Add external search infrastructure only after index-first retrieval demonstrably fails at the project's actual scale.
