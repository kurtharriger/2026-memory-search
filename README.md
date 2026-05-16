# Personal Conversation Search

Semantic search over personal AI conversation history using [LEANN](https://github.com/yichuan-w/LEANN) — a graph-based vector index with a 97% storage reduction vs traditional vector databases.

**Index location:** `~/.leann/indexes/conversations.leann`

---

## Searching

### From Claude Code (recommended)

Once the MCP server is registered (see [Setup from scratch](#setup-from-scratch)), ask naturally in any Claude Code session:

> "Search my conversation history for anything about Kubernetes networking"
> "What have I discussed about salary negotiation?"
> "Find conversations where I was debugging React hooks"

Claude will call `leann_search` with `index_name: "conversations"` automatically.

### From the terminal

```bash
uv run leann search conversations "your query here" --top-k 5 --non-interactive
```

---

## Updating the index

Re-export your data and rebuild whenever you want fresh results (suggested: monthly).

### 1. Export your data

**ChatGPT:** Settings → Data Controls → Export → download the ZIP → extract so the layout is:
```
downloads/chatgpt/conversations/conversations-000.json
downloads/chatgpt/conversations/conversations-001.json
...
```

**Claude.ai:** Settings → Privacy → Export Data → download the ZIP → extract into `downloads/claude/` (the nested batch folder is fine as-is).

### 2. Rebuild

```bash
uv run python build_index.py --force-rebuild
```

Takes about 20–30 seconds for ~2,000 conversations. The MCP server picks up the new index immediately — no restart needed.

After indexing, the script calls `claude -p` to generate a topic summary
(`~/.leann/indexes/conversations.summary.md`). This requires Claude Code to be
installed and authenticated. If it isn't, or you want to skip it:

```bash
uv run python build_index.py --force-rebuild --skip-summary
```

### 3. Verify

```bash
uv run leann search conversations "test query" --top-k 2 --non-interactive
```

---

## Project layout

```
.
├── build_index.py          # Indexes ChatGPT + Claude exports into LEANN
├── downloads/
│   ├── chatgpt/            # ChatGPT export (conversations/ subfolder)
│   └── claude/             # Claude.ai export (batch subfolder with conversations.json)
└── .claude/
    └── skills/
        └── leann-index/    # Project skill for AI agents (see below)

~/.leann/indexes/           # Index lives here (outside the repo — no gitignore needed)
```

---

## Setup from scratch

If you need to rebuild the environment on a new machine:

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

# 4. Build the index (writes to ~/.leann/indexes/ and registers it automatically)
uv run python build_index.py
```

---

## For AI agents

This project has a skill at `.claude/skills/leann-index/` that covers the update workflow, data source formats, and how the MCP server works. When helping with this project, consult that skill.

For deeper context on why the custom `build_index.py` exists (LEANN's built-in readers are mismatched against the current ChatGPT and Claude export formats), see `.claude/skills/leann-index/references/lessons-learned.md`.
