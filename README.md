# Personal Conversation Search

Semantic search over personal AI conversation history using [LEANN](https://github.com/yichuan-w/LEANN) — a graph-based vector index with a 97% storage reduction vs traditional vector databases.

Three separate indexes, one per source:

| Index | Search command | Contents |
|---|---|---|
| `chatgpt` | `leann search chatgpt` | ChatGPT conversation exports |
| `claude` | `leann search claude` | Claude.ai conversation exports |
| `claude-code` | `leann search claude-code` | Claude Code coding sessions |

---

## Searching

### From Claude Code (recommended)

Once the MCP server is registered (see [Setup from scratch](#setup-from-scratch)), ask naturally in any Claude Code session:

> "Search my conversation history for anything about Kubernetes networking"
> "What have I discussed about salary negotiation?"
> "Search my Claude Code sessions for anything about Keycloak"

Claude will call `leann_search` with the appropriate index name automatically.

### From the terminal

```bash
uv run leann search chatgpt "your query" --top-k 5 --non-interactive
uv run leann search claude "your query" --top-k 5 --non-interactive
uv run leann search claude-code "your query" --top-k 5 --non-interactive
```

---

## Updating the index

Re-export your data and rebuild whenever you want fresh results (suggested: monthly).

### 1. Export your data

**ChatGPT:** Settings → Data Controls → Export → download the ZIP → drop it into `downloads/chatgpt/` (no extraction needed):
```
downloads/chatgpt/<any>.zip
```

**Claude.ai:** Settings → Privacy → Export Data → download the ZIP → drop it into `downloads/claude/`. The filename is prefixed with `data-`:
```
downloads/claude/data-<uuid>-<timestamp>-<hash>-batch-NNNN.zip
```

**Claude Code** sessions are indexed automatically from `~/.claude/projects/` — no export needed.

### 2. Rebuild

Rebuild all sources:
```bash
uv run python build_index.py --force-rebuild
```

Rebuild only one source (e.g. after a new ChatGPT export, skip the others):
```bash
uv run python build_index.py --sources chatgpt --force-rebuild
```

After indexing, the script calls `claude -p` to generate topic summaries
(`~/.leann/indexes/<source>/<source>.summary.md` and a combined
`~/.leann/indexes/summary.md`). This requires Claude Code to be installed and
authenticated. To skip:

```bash
uv run python build_index.py --force-rebuild --skip-summary
```

### 3. Verify

```bash
uv run leann search chatgpt "test query" --top-k 2 --non-interactive
uv run leann search claude-code "test query" --top-k 2 --non-interactive
```

---

## Project layout

```
.
├── install.sh              # One-command setup (venv, MCP, user skill, indexes)
├── build_index.py          # Extracts ZIPs, writes per-conversation files, builds indexes
├── downloads/
│   ├── chatgpt/            # Drop ChatGPT export ZIP here (no extraction needed)
│   └── claude/             # Drop Claude.ai export ZIP here
└── .claude/
    └── skills/
        └── leann-index/    # Project skill for AI agents (see below)

~/.leann/indexes/           # All index output lives here (outside the repo)
    summary.md              # Combined topic summary (@-imported by user skill)
    chatgpt/
        chatgpt.leann       # Vector index (leann search chatgpt)
        chatgpt.summary.md
        sources/<id>.md     # One file per conversation
    claude/
        claude.leann        # Vector index (leann search claude)
        claude.summary.md
        sources/<id>.md
    claude-code/
        claude-code.leann   # Vector index (leann search claude-code)
        claude-code.summary.md
        sources/<id>.md     # One file per Claude Code session
~/.claude/skills/
    └── personal-search/    # User-level skill (created by install.sh)
                            # @-imports summary.md — stays current on rebuild
```

---

## Setup from scratch

Run the install script — it handles everything idempotently:

```bash
bash install.sh
```

This installs Homebrew dependencies, creates the Python 3.13 venv, installs
LEANN, registers the MCP server with Claude Code, and writes a user-level skill
(`~/.claude/skills/personal-search/`) so `leann_search` is available in all
Claude Code sessions. If export data is already in `downloads/`, it also builds
the indexes.

If no export data is found in `downloads/`, the index build is skipped
automatically and the script prints next steps. Safe to run before exporting.

### Manual setup (step by step)

If you prefer to do it by hand:

```bash
# 1. Install system dependencies
brew install libomp boost protobuf zeromq pkgconf

# 2. Create the Python 3.13 environment (3.14+ not supported by LEANN)
uv venv --python 3.13
uv pip install leann

# 3. Register the MCP server with Claude Code
# Stored in ~/.claude.json (machine-local, not in the repo).
# Required for leann_search to be available in Claude Code sessions.
claude mcp add leann-server -- "$(pwd)/.venv/bin/leann_mcp"

# 4. Build the indexes (writes to ~/.leann/indexes/ and registers automatically)
uv run python build_index.py
```

---

## For AI agents

This project has a skill at `.claude/skills/leann-index/` that covers the update workflow, data source formats, and how the MCP server works. When helping with this project, consult that skill.

For deeper context on why the custom `build_index.py` exists (LEANN's built-in readers are mismatched against the current ChatGPT and Claude export formats), see `.claude/skills/leann-index/references/lessons-learned.md`.
