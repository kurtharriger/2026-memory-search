# LEANN Index — Lessons Learned

Reference document for troubleshooting and rebuilding. Load this when
something breaks, when adding a new data source, or when upgrading LEANN.

---

## Why build_index.py exists (LEANN's built-in readers don't work)

LEANN ships with `apps/chatgpt_rag.py` and `apps/claude_rag.py`, but both
readers are mismatched against the current export formats. Rather than patch
upstream code, we wrote a thin custom script that uses LEANN's core API
(`LeannBuilder`) directly.

### ChatGPT reader: HTML only, exports are JSON ZIPs

LEANN's `ChatGPTReader` parses `chat.html` from the old export format.
The current ChatGPT export (as of 2025) is a ZIP file containing JSON files:

```
<any>.zip
  └── conversations/
        conversations-000.json   # list of ~100 conversations each
        conversations-001.json
        ...
```

`build_index.py` accepts the ZIP directly (placed in `downloads/chatgpt/`),
extracts only the `conversations-*.json` files to a temp dir, and processes them.

Each conversation uses a **mapping tree** — a dict of nodes where each node
has `{id, message, parent}`. This represents the branching history when users
edit and regenerate responses. To get the active thread:

1. Start at `conv["current_node"]`
2. Walk backwards via `node["parent"]` until `parent` is `None`
3. Reverse the list → chronological order

Each node's message content lives at `message["content"]["parts"]` (a list of
strings — join them). Role is at `message["author"]["role"]` (`"user"` or
`"assistant"`; skip `"system"` and `"tool"` nodes).

### Claude reader: wrong key names, ZIP input

LEANN's `ClaudeReader` looks for a `messages` key on each conversation, but
the actual Claude export uses `chat_messages`. It also looks for `role` and
`content` on each message, but the actual fields are `sender` (`"human"` or
`"assistant"`) and `text`. The reader silently produces zero documents.

The Claude export is a ZIP containing a nested batch folder. The downloaded
filename is prefixed with `data-`, not `claude-`:

```
data-<uuid>-<timestamp>-<hash>-batch-NNNN.zip
  └── data-<uuid>-batch-NNNN/
        conversations.json   # list of conversations
        memories.json
        projects/
        users.json
```

`build_index.py` accepts the ZIP directly (placed in `downloads/claude/`),
extracts only files named `conversations.json` to a temp dir, and processes them.

---

## Project field not available in exports

Neither ChatGPT nor Claude includes project membership in their conversation
exports (as of 2026). ChatGPT has a `conversation_template_id` field but it is
`None` for regular and project conversations alike. Claude's export has no
project field at all.

If either service adds project info to their export format in future, it can
be added to the YAML frontmatter written by `extract_chatgpt_zip` /
`extract_claude_zip` in `build_index.py`.

---

## Per-conversation file storage

`build_index.py` writes each conversation as an individual `.md` file:
```
~/.leann/indexes/chatgpt/<conversation-id>.md
~/.leann/indexes/claude/<conversation-uuid>.md
```

The `source` field in search result metadata is the absolute path to this file.
Claude can `Read` the file for full conversation text without re-processing the
ZIP or authenticating with any web app.

`--force-rebuild` clears these subdirectories before re-extracting so stale
conversations from an old export don't linger alongside new ones.

---

## Python version: must be 3.13, not 3.14

`leann-backend-hnsw` only ships wheels for Python 3.10–3.13. Python 3.14
(the Homebrew default as of May 2026) fails with "no matching platform tag."

```bash
# Correct setup:
uv venv --python 3.13
source .venv/bin/activate
uv pip install leann
```

The system has Python 3.13 at `/usr/local/bin/python3.13`.

---

## Brew dependencies

These must be installed before `uv pip install leann`:

```bash
brew install libomp boost protobuf zeromq pkgconf
```

`libomp` is keg-only (not symlinked into `/opt/homebrew`). If you get
linker errors, set:
```bash
export LDFLAGS="-L/opt/homebrew/opt/libomp/lib"
export CPPFLAGS="-I/opt/homebrew/opt/libomp/include"
```

---

## Smoke-testing: always deregister temp indexes afterward

`LeannBuilder.build_index()` followed by `register_project_directory()` writes
the index directory path into `~/.leann/projects.json`. If you use a temp
directory for smoke tests (e.g. `--index-dir /tmp/leann-test`), that path gets
registered and `leann search conversations` will prompt "Found N indexes named
'conversations' — which one?" on every subsequent search.

**After any smoke test, deregister the temp directory:**

```python
import json
from pathlib import Path
p = Path("~/.leann/projects.json").expanduser()
keep = [r for r in json.loads(p.read_text())
        if not r.startswith("/tmp") and not r.startswith("/private/tmp")]
p.write_text(json.dumps(keep, indent=2))
```

Or edit `~/.leann/projects.json` directly — it's a plain JSON array of paths.

**Better practice:** use a named temp dir under `/tmp` and clean it up
explicitly, or avoid registering at all by not calling `register_project_directory`
in test runs. `build_index.py --max-convos 5` still registers; if you're just
testing parsing/extraction, call the extract functions directly without going
through `build()`.

---

## Index naming: leann list vs leann search

These two commands resolve index names differently — a source of confusion:

- **`leann search <name>`** resolves `<name>` as the filename stem (e.g. `conversations`
  for `conversations.leann`), searching across all projects registered in
  `~/.leann/projects.json`.
- **`leann list`** is CWD-based — it scans the current directory and shows
  directory names, not filenames. Running it from the project directory shows
  nothing because the index lives at `~/.leann/indexes/`, not in the project.
  Running it from `~/.leann/indexes/` shows the index labelled `indexes` (the
  directory name).

**Practical consequence:** always use `leann search conversations` (the filename),
and don't rely on `leann list` to confirm the index is present — use search instead.

---

## How LEANN discovers indexes (leann list / leann search)

`leann search` resolves index names against directories registered in
`~/.leann/projects.json` — a JSON array of project directory paths.

`LeannBuilder.build_index(path)` writes the index files but does **not**
automatically register the directory. Registration must be done explicitly:

```python
from leann.registry import register_project_directory
register_project_directory(Path("~/.leann/indexes").expanduser())
```

`build_index.py` calls this after every build. If you bypass the script and
call `LeannBuilder` directly, you must register manually or `leann search`
will silently fail to find the index.

A directory qualifies for registration only if it already contains
`*.leann.meta.json` files — so the call must come *after* `build_index()`.

`leann search <index_name> <query>` resolves `index_name` as the filename
stem of the `.leann` file (e.g. `conversations` for `conversations.leann`),
searching across all registered directories.

---

## How the MCP server works

`leann_mcp` (`.venv/bin/leann_mcp`) is a thin JSON-RPC stdio server. It
exposes two tools:

- **`leann_list`** — shells out to `leann list`
- **`leann_search`** — shells out to `leann search <index_name> <query> --json`

So it requires the `leann` CLI to be on `PATH`. Since the MCP binary is inside
the venv, `leann` is also in the venv's bin — they're co-located, so `PATH`
resolution works automatically when Claude Code spawns the process.

The MCP is registered in `~/.claude.json` (machine-local, not in the repo)
under the project's `mcpServers` key — there is no `.mcp.json` in the project.
If you recreate the venv at a different path, update that registration:

```bash
claude mcp remove leann-server
claude mcp add leann-server -- /path/to/.venv/bin/leann_mcp
```

---

## Embedding model choice

We use `sentence-transformers/all-MiniLM-L6-v2` (384-dim, fast, local).
LEANN's default is `facebook/contriever` — either works, but MiniLM is
noticeably faster for indexing and good enough for personal history search.

The model is downloaded to HuggingFace cache on first run. If offline, it uses
the cached version automatically (a warning is printed but it still works).

---

## Topic summary generation (`conversations.summary.md`)

After building the index, `build_index.py` calls `claude -p` (the Claude Code CLI)
to analyze all conversation titles and write a structured topic summary to
`~/.leann/indexes/conversations.summary.md`. This is useful for embedding in a
user-level skill so Claude knows what's in the index across sessions.

**What gets sent:** Only conversation titles — not full text. Titles average
~6 tokens each, so token count scales with conversation count (a few thousand
conversations ≈ tens of thousands of tokens). Fast and cheap regardless of scale.

**CLAUDE.md is included in the call.** `claude -p` injects your global
`~/.claude/CLAUDE.md` (and any project-level CLAUDE.md) even when you pass
`--system-prompt`. The only way to skip it is `--bare`, but `--bare` disables
OAuth auth and requires `ANTHROPIC_API_KEY` — not practical if you authenticate
via Claude Code's OAuth. In practice the coding guidelines in CLAUDE.md don't affect summarization
quality; the extra overhead is a few hundred tokens regardless of dataset size.

**To skip summary generation** (e.g. for a fast rebuild, or if `claude` CLI is
not on PATH):

```bash
uv run python build_index.py --force-rebuild --skip-summary
```

---

## Chunking: use chunk_doc(), not bare add_text()

`LeannBuilder.add_text(text, metadata)` stores text verbatim — it does **no
chunking**. A 114 KB conversation indexed as one unit embeds as a single vector
and is returned in full on every match, consuming enormous context budget.

`build_index.py` uses a `chunk_doc()` helper (wrapping llama-index's
`SentenceSplitter`) that splits each conversation into 512-token overlapping
chunks before calling `add_text`. Each chunk gets its own vector and is returned
independently, so search results are 1–3 KB instead of 60–115 KB.

```python
_splitter = SentenceSplitter(chunk_size=512, chunk_overlap=128, ...)

def chunk_doc(text, metadata):
    nodes = _splitter.get_nodes_from_documents([LlamaDocument(text=text)])
    base_id = metadata.get("id", "")
    return [
        (node.get_content(), {**metadata, "id": f"{base_id}_{i}", "chunk_index": i})
        for i, node in enumerate(nodes)
    ]
```

Each chunk carries `source_file_size` (bytes of the full source `.md` file) so
the caller can check document size without hitting the filesystem. Chunk ids are
`{conv_id}_{i}` to avoid collisions in the passage store.

LEANN ships `apps/chatgpt_rag.py` as a reference — it calls `create_text_chunks()`
before indexing, confirming this is the intended pattern.

### Don't read source files without checking size first

Search result metadata includes `source` (path to the full conversation `.md`)
and `source_file_size`. Always check size before reading:

```bash
wc -c <path>   # bytes
```

Conversation files can be 50K+ tokens for long Claude Code sessions. Only read
if the user explicitly needs the full text and the file is small (< ~20 KB).
Summarize from the search excerpt otherwise.

---

## ChatGPT and Claude ZIP placement

Drop export ZIPs directly into the source directories — no extraction needed,
and no renaming required:
```
downloads/chatgpt/<any>.zip
downloads/claude/data-<uuid>-<timestamp>-<hash>-batch-NNNN.zip   # Claude: data- prefix
```

`build_index.py` scans each directory for `*.zip`, extracts only the
conversation JSON files to a temp dir, and processes from there. Multiple ZIPs
can coexist in the same directory (e.g. multiple exports over time), but note
that `--force-rebuild` clears `~/.leann/indexes/chatgpt/` and `~/.leann/indexes/claude/`
before re-processing all ZIPs, so you'll always get a clean merged result.
