# Next Session: Natural Language Notebook Builder

## The Problem

Creating a NotebookLM notebook from Zotero sources currently requires knowing exact Zotero collection keys, item keys, and API paths. The user wants to say things like:

> "Create a new NotebookLM collection with sources from the EM Book Research group library / _2026_jan / o'neill folder"

And have the system figure out:
1. Which Zotero collection that maps to (fuzzy name matching)
2. Which items have PDFs in that collection
3. Create a NotebookLM notebook with the right title
4. Upload all the PDFs
5. Report back what was created

## Investigation Questions

### 1. Architecture: Agent vs Service vs Both?

**Option A: Claude Agent SDK standalone agent**
- A Python script using the Claude Agent SDK (`claude-agent-sdk`)
- The agent has tools: `search_zotero_collections`, `list_items`, `create_notebook`, `upload_sources`
- User talks to it conversationally: "create a notebook from the O'Neill papers in January"
- Agent reasons about what to do, calls tools, reports results
- Runs locally or as a CLI tool

**Option B: Extend the existing notebooklm-api with an LLM-powered endpoint**
- Add `POST /api/orchestrate` that accepts natural language
- Internally calls Claude API to parse intent, resolve Zotero paths, orchestrate the pipeline
- Returns structured result
- Advantage: everything in one deployable service

**Option C: Both**
- The API has the orchestration endpoint (Option B)
- A separate lightweight CLI agent (Option A) that can either call the API or work directly

**Investigate**: What does the Claude Agent SDK actually provide? Is it just tool-use with a loop, or does it have richer orchestration? Read https://github.com/anthropic/agent-sdk or whatever the current package is. Compare effort of SDK agent vs a simple tool-use loop with the Anthropic Python SDK.

### 2. Zotero Hierarchy Resolution

The Zotero API has nested collections. A command like "EM Book Research / _2026_jan / o'neill" means:
- Find collection named something like "EM Book Research" (top-level or nested)
- Within that, find "_2026_jan"
- Within that, find "o'neill"

Current `zotero_service.py` only lists top-level collections. Need:
- Recursive collection traversal
- Fuzzy name matching (user says "o'neill", collection might be "O'Neill" or "ONeill_2024")
- Breadcrumb path display: "Found: EM Book Research > _2026_jan > O'Neill (7 items, 5 PDFs)"

**Investigate**: Does the Zotero API support searching collections by name? Or do we need to fetch the full tree and search locally? Check `GET /groups/{id}/collections?q=...` or similar.

### 3. Batch Upload Intelligence

When uploading 10+ PDFs to NotebookLM:
- What's the rate limit? Does notebooklm-py handle it?
- Should we upload in parallel or sequential?
- How do we handle partial failures (3 of 10 succeed)?
- NotebookLM has a source limit per notebook — what is it? (currently 50?)

**Investigate**: Test uploading 5-10 sources in a row via the API. Measure timing, check for errors.

### 4. Naming Conventions

When auto-creating notebooks, what title? Options:
- Mirror the Zotero path: "O'Neill - Jan 2026"
- Let the user specify
- Agent asks if no title given

### 5. What the Agent Needs Access To

Tools the agent would need:
1. **search_zotero** — fuzzy search collections by path/name
2. **list_collection_items** — show what's in a collection (with PDF status)
3. **create_notebook** — create a NotebookLM notebook
4. **upload_sources** — upload PDFs from Zotero to notebook
5. **check_notebook_status** — verify sources are processed
6. **list_notebooks** — see existing notebooks (avoid duplicates)

All of these already exist in the notebooklm-api — the agent just needs to call them.

## Proposed Approach (To Validate)

1. **Add fuzzy Zotero collection search** to notebooklm-api (`GET /api/zotero/search?path=...`)
2. **Add an orchestration endpoint** (`POST /api/build-notebook`) that accepts natural language
3. Internally use Claude to parse the intent, resolve the Zotero path, and execute the pipeline
4. **Optionally** build a CLI agent using Anthropic SDK's tool-use that wraps the API

This keeps the smarts in the API (deployable, testable) while allowing a conversational CLI on top.

## Files to Read Before Starting

- `~/projects/notebooklm-api/src/services/zotero_service.py` — current Zotero client
- `~/projects/notebooklm-api/src/services/source_service.py` — upload pipeline
- `~/projects/notebooklm-api/src/routes/sources.py` — existing from-zotero endpoint
- Zotero API docs: https://www.zotero.org/support/dev/web_api/v3/basics
- Claude Agent SDK docs (or just Anthropic SDK tool-use): https://docs.anthropic.com/en/docs/agents-tools

## Success Criteria

The user can say:
```
"Make a notebook from the Castoriadis papers in _2026_feb"
```

And get back:
```
Created notebook "Castoriadis - Feb 2026" with 4 sources:
  - Castoriadis_Democracy_1990.pdf ✓
  - Castoriadis_Technique_1984.pdf ✓
  - Castoriadis_Rationality_1997.pdf ✓
  - Castoriadis_Imaginary_Institution.pdf ✓
All sources processed and ready for queries.
Notebook ID: abc123...
```
