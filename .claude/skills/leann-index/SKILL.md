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

Semantic search over personal AI conversation history, split into three named
indexes so each source can be searched or rebuilt independently.

| Index | Search command | Location |
|---|---|---|
| `chatgpt` | `leann search chatgpt` | `~/.leann/indexes/chatgpt/` |
| `claude` | `leann search claude` | `~/.leann/indexes/claude/` |
| `claude-code` | `leann search claude-code` | `~/.leann/indexes/claude-code/` |

Each source directory contains:
- `<source>.leann` — the vector index file
- `<source>.summary.md` — topic summary for that source
- `sources/<id>.md` — one plain-text file per conversation, with YAML frontmatter

The combined topic summary is at `~/.leann/indexes/summary.md`.

| Component | Location |
|---|---|
| Build script | `build_index.py` |
| Python env | `.venv/` (Python 3.13, managed by `uv`) |
| MCP binary | `.venv/bin/leann_mcp` |
| ChatGPT exports | `downloads/chatgpt/*.zip` |
| Claude exports | `downloads/claude/*.zip` |
| Claude Code sessions | `~/.claude/projects/` (read directly, no export needed) |

---

## How to search

The `leann_search` MCP tool is available in this session. Search a specific
source or all three in parallel:

```
leann_search(index_name="chatgpt", query="kubernetes networking")
leann_search(index_name="claude", query="kubernetes networking")
leann_search(index_name="claude-code", query="kubernetes networking")
```

Each result includes a `source` field — the absolute path to the individual
conversation file. Read that file for full context when needed.

---

## Updating the index with new exports

### 1. Download fresh exports

**ChatGPT:** Settings → Data Controls → Export → download the ZIP → drop into
`downloads/chatgpt/` (no extraction needed).

**Claude.ai:** Settings → Privacy → Export Data → download the ZIP → drop into
`downloads/claude/` (no extraction needed). The filename starts with `data-`.

**Claude Code** sessions come from `~/.claude/projects/` automatically — no export needed.

### 2. Rebuild

Rebuild all sources:
```bash
cd /Users/kurtharriger/dev/chatgptmemory
uv run python build_index.py --force-rebuild
```

Rebuild only one source (faster when you only have a new export for one):
```bash
uv run python build_index.py --sources chatgpt --force-rebuild
uv run python build_index.py --sources claude-code --force-rebuild
```

Skip topic summary generation (saves time / avoids `claude` CLI call):
```bash
uv run python build_index.py --force-rebuild --skip-summary
```

### 3. Verify

```bash
uv run leann search chatgpt "test query" --top-k 2 --non-interactive
uv run leann search claude-code "test query" --top-k 2 --non-interactive
```

---

## Adding a new data source (Obsidian, Logseq, etc.)

Add a new source to `build_index.py`:
1. Write a `load_<source>_docs(export_dir, sources_dir)` function following the
   existing pattern — returns `list[tuple[str, dict]]`
2. Add the source name to `VALID_SOURCES` and `SOURCE_LABEL` / `SOURCE_UNIT`
3. Add the load call in `build_source()`

For simple markdown/PDF directories, LEANN's built-in `document_rag` also works:
```bash
uv run python -m apps.document_rag \
  --data-dir ~/path/to/obsidian-vault \
  --file-types .md \
  --index-dir ~/.leann/indexes/obsidian
```

---

## If something breaks

See `references/lessons-learned.md` for:
- Why a custom `build_index.py` was needed instead of LEANN's built-in readers
- Python version constraints and brew dependency notes
- How ChatGPT's JSON mapping tree works
- How LEANN discovers and names indexes (`leann list` vs `leann search`)
- How the MCP server works under the hood
- Smoke-test cleanup (deregistering temp indexes from `~/.leann/projects.json`)
