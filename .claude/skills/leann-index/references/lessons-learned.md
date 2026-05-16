# LEANN Index — Lessons Learned

Reference document for troubleshooting and rebuilding. Load this when
something breaks, when adding a new data source, or when upgrading LEANN.

---

## Why build_index.py exists (LEANN's built-in readers don't work)

LEANN ships with `apps/chatgpt_rag.py` and `apps/claude_rag.py`, but both
readers are mismatched against the current export formats. Rather than patch
upstream code, we wrote a thin custom script that uses LEANN's core API
(`LeannBuilder`) directly.

### ChatGPT reader: HTML only, exports are JSON

LEANN's `ChatGPTReader` parses `chat.html` from the old export format.
The current ChatGPT export (as of 2025) is a directory of JSON files:

```
downloads/chatgpt/conversations/
  conversations-000.json   # list of 100 conversations each
  conversations-001.json
  ...
```

Each conversation uses a **mapping tree** — a dict of nodes where each node
has `{id, message, parent}`. This represents the branching history when users
edit and regenerate responses. To get the active thread:

1. Start at `conv["current_node"]`
2. Walk backwards via `node["parent"]` until `parent` is `None`
3. Reverse the list → chronological order

Each node's message content lives at `message["content"]["parts"]` (a list of
strings — join them). Role is at `message["author"]["role"]` (`"user"` or
`"assistant"`; skip `"system"` and `"tool"` nodes).

### Claude reader: wrong key names

LEANN's `ClaudeReader` looks for a `messages` key on each conversation, but
the actual Claude export uses `chat_messages`. It also looks for `role` and
`content` on each message, but the actual fields are `sender` (`"human"` or
`"assistant"`) and `text`. The reader silently produces zero documents.

The Claude export unpacks to a nested structure:
```
downloads/claude/
  data-<uuid>-batch-0000/
    conversations.json   # list of 23 conversations
    memories.json
    projects/
    users.json
```

`build_index.py` finds `conversations.json` with `glob("**/conversations.json",
recursive=True)` so the nested batch folder doesn't need to be renamed.

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

## One document per conversation (no chunking)

`build_index.py` indexes each conversation as a single document. This means:
- Very long conversations may exceed the model's max token window (384 tokens
  for MiniLM) and get truncated on embedding
- Search returns whole conversations, not individual messages

For most personal history searches this is fine. If retrieval quality suffers
on long technical conversations, consider splitting conversations into chunks
of N messages with `create_text_chunks()` from LEANN's `apps/chunking` module.

---

## ChatGPT zip file in downloads/

There's a leftover zip file in `downloads/` from the original export:
`chatgpt-<hash>-2026-05-15-03-02-07-<id>.zip`

The extracted `conversations/` folder is what `build_index.py` reads.
The zip can be deleted or kept as a backup.
