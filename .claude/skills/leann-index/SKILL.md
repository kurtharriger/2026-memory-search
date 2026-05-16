---
name: leann-index
description: >
  Maintain and update the personal conversation search index for this project.
  Use this skill whenever the user asks about rebuilding the index, adding new
  ChatGPT or Claude exports, searching conversation history, troubleshooting
  the leann-server MCP, or understanding how the semantic search is set up.
  Also use it when the user mentions downloading a new export, re-indexing,
  or wonders why search results are stale.
---

# LEANN Conversation Index

## What's here

A semantic search index over personal AI conversation history, queryable via
the `leann-server` MCP tool already wired into Claude Code for this project.

| Component | Location |
|---|---|
| Index data | `~/.leann/indexes/conversations.leann` (outside repo) |
| Build script | `build_index.py` |
| Python env | `.venv/` (Python 3.13, managed by `uv`) |
| MCP binary | `.venv/bin/leann_mcp` |
| Downloads | `downloads/chatgpt/`, `downloads/claude/` |

**Indexed so far:** 2,120 ChatGPT conversations + 23 Claude conversations = 2,143 total.

---

## How to search

The `leann_search` MCP tool is available in this session. Use it directly:

> Search my conversation history for anything about Kubernetes networking

Or call it explicitly with `leann_search` (index name: `conversations`). The tool
also exposes `leann_list` — but note it only lists indexes in the current directory,
so it won't show the `~/.leann/indexes` index. Use `leann_search` directly instead.

---

## Updating the index with new exports

### 1. Download fresh exports

**ChatGPT:** Settings → Data Controls → Export → download ZIP → extract into
`downloads/chatgpt/` so the `conversations/` subfolder is at
`downloads/chatgpt/conversations/conversations-000.json` etc.

**Claude.ai:** Settings → Privacy → Export Data → download ZIP → extract into
`downloads/claude/` (the script finds `conversations.json` recursively, so the
nested batch folder is fine as-is).

### 2. Rebuild the index

```bash
cd /Users/kurtharriger/dev/chatgptmemory
uv run python build_index.py --force-rebuild
```

Takes ~20–30 seconds for ~2,000 conversations. Writes to `~/.leann/indexes/`
and registers it automatically — no extra steps needed.

### 3. Verify

```bash
uv run leann search conversations "test query" --top-k 2 --non-interactive
```

---

## Adding a new data source (Obsidian, career docs, etc.)

Use LEANN's built-in `document_rag` for markdown/PDF directories:

```bash
# From the LEANN repo (or installed apps):
uv run python -m apps.document_rag \
  --data-dir ~/path/to/obsidian-vault \
  --file-types .md \
  --index-dir ~/.leann/indexes/obsidian
```

Or add a new `load_*_docs()` function in `build_index.py` and call it from
`build()` — the pattern is already there for ChatGPT and Claude.

---

## If something breaks

See `references/lessons-learned.md` for:
- Why a custom `build_index.py` was needed instead of LEANN's built-in readers
- Python version constraints and dependency notes
- How ChatGPT's JSON mapping tree works
- How LEANN discovers and names indexes
- How the MCP server works under the hood
