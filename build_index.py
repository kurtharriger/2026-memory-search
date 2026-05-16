"""
Build a LEANN semantic search index over ChatGPT, Claude, and Claude Code conversations.

Sources:
  downloads/chatgpt/<any>.zip   ChatGPT export ZIP (no extraction needed)
  downloads/claude/<any>.zip    Claude.ai export ZIP (no extraction needed)
  ~/.claude/projects/           Claude Code session transcripts (JSONL, read directly)

Each conversation is written as an individual .md file with YAML frontmatter:
  ~/.leann/indexes/chatgpt/<id>.md
  ~/.leann/indexes/claude/<id>.md
  ~/.leann/indexes/claude-code/<session-id>.md

The `source` field in search result metadata is the path to this file, so
callers can Read it for full context without web authentication.

Usage:
    uv run python build_index.py [--max-convos N] [--force-rebuild] [--skip-summary]

    --force-rebuild  Clears existing per-conversation files and rebuilds from scratch.
    --max-convos N   Limit total conversations (useful for testing).
    --skip-summary   Skip topic summary generation (no `claude` CLI call).

Output:
    ~/.leann/indexes/conversations.leann        (vector index)
    ~/.leann/indexes/chatgpt/<id>.md
    ~/.leann/indexes/claude/<id>.md
    ~/.leann/indexes/claude-code/<session-id>.md
    ~/.leann/indexes/conversations.summary.md   (topic summary, requires `claude` CLI)

Export format notes:
  ChatGPT ZIP: conversations/conversations-NNN.json, mapping tree, walk current_node.
  Claude ZIP: <batch>/conversations.json, chat_messages list with sender/text fields.
  Claude Code JSONL: messages with type user/assistant/ai-title; text blocks indexed,
    tool_use/tool_result/thinking blocks skipped.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Parsers for each export format
# ---------------------------------------------------------------------------

def parse_chatgpt_conversation(conv: dict) -> str | None:
    """
    Convert one ChatGPT conversation dict to a plain-text document.

    Walks backward from `current_node` through the parent chain to get the
    active thread (ignoring edited/regenerated branches), then reverses for
    chronological order. Skips system/tool messages.
    Returns None if the conversation has no substantive messages.
    """
    title = conv.get("title", "Untitled")
    mapping = conv.get("mapping", {})
    current_node = conv.get("current_node")

    # Walk from current_node backward to root
    thread = []
    node_id = current_node
    while node_id:
        node = mapping.get(node_id, {})
        msg = node.get("message")
        if msg:
            role = msg.get("author", {}).get("role", "")
            content = msg.get("content", {})
            # content.parts is a list; entries may be strings or dicts (for images, etc.)
            parts = content.get("parts", [])
            text = " ".join(p for p in parts if isinstance(p, str)).strip()
            if text and role in ("user", "assistant"):
                thread.append((role, text))
        node_id = node.get("parent")

    if not thread:
        return None

    thread.reverse()  # chronological order

    lines = [f"Conversation: {title}", ""]
    for role, text in thread:
        label = "User" if role == "user" else "ChatGPT"
        lines.append(f"[{label}]: {text}")
        lines.append("")

    return "\n".join(lines)


def parse_claude_conversation(conv: dict) -> str | None:
    """
    Convert one Claude conversation dict to a plain-text document.

    Claude exports use `chat_messages` (not `messages`) with `sender` (not `role`)
    and `text` (not `content`) fields.
    Returns None if the conversation has no substantive messages.
    """
    title = conv.get("name", "Untitled")
    messages = conv.get("chat_messages", [])

    lines = [f"Conversation: {title}", ""]
    count = 0
    for msg in messages:
        sender = msg.get("sender", "")
        text = (msg.get("text") or "").strip()
        if not text or sender not in ("human", "assistant"):
            continue
        label = "User" if sender == "human" else "Claude"
        lines.append(f"[{label}]: {text}")
        lines.append("")
        count += 1

    if count == 0:
        return None

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def unix_to_iso(ts) -> str:
    """Convert a Unix timestamp (int or float) to an ISO 8601 UTC string."""
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def trim_iso(s: str) -> str:
    """Trim sub-second precision from an ISO 8601 string, keep the Z suffix."""
    if not s:
        return ""
    # "2026-05-16T17:06:36.911316Z" → "2026-05-16T17:06:36Z"
    return re.sub(r'\.\d+Z$', 'Z', s)


def yaml_str(s: str) -> str:
    """
    Quote a string value for YAML frontmatter using JSON encoding.
    Handles colons (URLs), special chars, and multi-line content safely.
    json.dumps produces double-quoted strings that are valid YAML.
    """
    return json.dumps(str(s))


# ---------------------------------------------------------------------------
# ZIP extraction — reads ZIP exports, writes one .md per conversation
# ---------------------------------------------------------------------------

def extract_chatgpt_zip(zip_path: Path, conv_dir: Path) -> list[tuple[str, dict]]:
    """
    Extract ChatGPT conversations from a ZIP export.

    Finds conversations-NNN.json files inside the ZIP (regardless of nesting),
    parses each conversation, and writes one .md file to conv_dir. Returns
    (text, metadata) tuples ready for indexing. The metadata `source` field is
    the absolute path of the written file so callers can read it for full context.
    """
    docs = []
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist()
                     if re.search(r'conversations.*\.json$', n)]
            if not names:
                print(f"  No conversations*.json found in {zip_path.name}")
                return docs
            zf.extractall(tmpdir, members=names)

        for fpath in sorted(Path(tmpdir).glob("**/conversations*.json")):
            with open(fpath, encoding="utf-8") as f:
                conversations = json.load(f)
            for conv in conversations:
                body = parse_chatgpt_conversation(conv)
                if not body:
                    continue
                conv_id = conv.get("id", "")
                if not conv_id:
                    continue

                # Build YAML frontmatter from available fields
                fm_lines = [
                    "---",
                    f"id: {conv_id}",
                    f"title: {yaml_str(conv.get('title', 'Untitled'))}",
                    "source: chatgpt",
                ]
                if created := unix_to_iso(conv.get("create_time")):
                    fm_lines.append(f"created: {created}")
                if updated := unix_to_iso(conv.get("update_time")):
                    fm_lines.append(f"updated: {updated}")
                if model := conv.get("default_model_slug"):
                    fm_lines.append(f"model: {model}")
                fm_lines.append("---\n")
                text = "\n".join(fm_lines) + body

                out_path = conv_dir / f"{conv_id}.md"
                out_path.write_text(text, encoding="utf-8")
                docs.append((text, {
                    "source": str(out_path),
                    "title": conv.get("title", "Untitled"),
                    "id": conv_id,
                    "created": conv.get("create_time", 0),
                }))
    return docs


def extract_claude_zip(zip_path: Path, conv_dir: Path) -> list[tuple[str, dict]]:
    """
    Extract Claude conversations from a ZIP export.

    Finds conversations.json inside the ZIP (regardless of batch-folder nesting),
    parses each conversation, and writes one .md file to conv_dir. Returns
    (text, metadata) tuples with `source` set to the written file path.
    """
    docs = []
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist()
                     if n.endswith("conversations.json")]
            if not names:
                print(f"  No conversations.json found in {zip_path.name}")
                return docs
            zf.extractall(tmpdir, members=names)

        for fpath in Path(tmpdir).glob("**/conversations.json"):
            with open(fpath, encoding="utf-8") as f:
                conversations = json.load(f)
            for conv in conversations:
                body = parse_claude_conversation(conv)
                if not body:
                    continue
                conv_id = conv.get("uuid", "")
                if not conv_id:
                    continue

                # Build YAML frontmatter from available fields
                fm_lines = [
                    "---",
                    f"id: {conv_id}",
                    f"title: {yaml_str(conv.get('name', 'Untitled'))}",
                    "source: claude",
                ]
                if created := trim_iso(conv.get("created_at", "")):
                    fm_lines.append(f"created: {created}")
                if updated := trim_iso(conv.get("updated_at", "")):
                    fm_lines.append(f"updated: {updated}")
                if summary := (conv.get("summary") or "").strip():
                    fm_lines.append(f"summary: {yaml_str(summary)}")
                fm_lines.append("---\n")
                text = "\n".join(fm_lines) + body

                out_path = conv_dir / f"{conv_id}.md"
                out_path.write_text(text, encoding="utf-8")
                docs.append((text, {
                    "source": str(out_path),
                    "title": conv.get("name", "Untitled"),
                    "id": conv_id,
                    "created": conv.get("created_at", ""),
                }))
    return docs


def load_chatgpt_docs(export_dir: str, conv_dir: Path) -> list[tuple[str, dict]]:
    """
    Find ZIP files in export_dir and extract ChatGPT conversations into conv_dir.
    Returns (text, metadata) tuples ready for indexing.
    """
    zips = sorted(Path(export_dir).glob("*.zip"))
    if not zips:
        print(f"  No ZIP files found in {export_dir}")
        return []

    docs = []
    for zip_path in zips:
        print(f"  Extracting {zip_path.name}...")
        docs.extend(extract_chatgpt_zip(zip_path, conv_dir))
    return docs


def load_claude_docs(export_dir: str, conv_dir: Path) -> list[tuple[str, dict]]:
    """
    Find ZIP files in export_dir and extract Claude conversations into conv_dir.
    Returns (text, metadata) tuples ready for indexing.
    """
    zips = sorted(Path(export_dir).glob("*.zip"))
    if not zips:
        print(f"  No ZIP files found in {export_dir}")
        return []

    docs = []
    for zip_path in zips:
        print(f"  Extracting {zip_path.name}...")
        docs.extend(extract_claude_zip(zip_path, conv_dir))
    return docs


# ---------------------------------------------------------------------------
# Claude Code session transcripts
# ---------------------------------------------------------------------------

def parse_claude_code_session(msgs: list[dict]) -> tuple[str, dict] | None:
    """
    Parse a Claude Code session JSONL (list of decoded message dicts) into
    (body_text, metadata).

    Indexed content:
      - User prompts: type=="user" where content is a string, or the `text`
        items within a content list (tool_result items are skipped).
      - Assistant prose: type=="assistant" content list `text` items only;
        tool_use and thinking blocks are skipped.

    Metadata extracted:
      - title: from the ai-title message (falls back to first user prompt)
      - session_id, cwd, created: from the first user/assistant message

    Returns None if the session has no indexable user/assistant text.
    """
    title = None
    session_id = None
    cwd = None
    created = None
    exchanges = []

    for msg in msgs:
        mtype = msg.get("type")

        if mtype == "ai-title":
            title = msg.get("aiTitle") or title
            session_id = session_id or msg.get("sessionId")
            continue

        # Grab session-level metadata from the first substantive message
        if mtype in ("user", "assistant") and not session_id:
            session_id = msg.get("sessionId")
            cwd = msg.get("cwd")
            created = msg.get("timestamp")

        # Content lives at msg["message"]["content"] for user/assistant,
        # or msg["content"] for queue-operation (which we don't index).
        content = (msg.get("message") or {}).get("content") or msg.get("content")

        if mtype == "user":
            if isinstance(content, str) and content.strip():
                exchanges.append(("User", content.strip()))
            elif isinstance(content, list):
                # List may mix tool_result and text; keep only text items.
                for item in content:
                    if item.get("type") == "text" and (item.get("text") or "").strip():
                        exchanges.append(("User", item["text"].strip()))

        elif mtype == "assistant":
            if isinstance(content, list):
                parts = [
                    item["text"].strip()
                    for item in content
                    if item.get("type") == "text" and (item.get("text") or "").strip()
                ]
                if parts:
                    exchanges.append(("Claude Code", " ".join(parts)))

    if not exchanges:
        return None

    # Fall back to first user prompt as title if ai-title wasn't emitted.
    # Skip content that looks like system injections (starts with < tag).
    if not title:
        first_user = next(
            (text for role, text in exchanges
             if role == "User" and not text.startswith("<")),
            None,
        )
        title = (first_user or "")[:80] or "Untitled session"

    lines = [f"Conversation: {title}", ""]
    for role, text in exchanges:
        lines.append(f"[{role}]: {text}")
        lines.append("")

    return "\n".join(lines), {
        "title": title,
        "session_id": session_id,
        "cwd": cwd,
        "created": created,
    }


def load_claude_code_docs(projects_dir: str, conv_dir: Path) -> list[tuple[str, dict]]:
    """
    Load Claude Code session transcripts from ~/.claude/projects/.

    Walks all *.jsonl files under projects_dir, parses each into a document,
    writes one .md file per session to conv_dir, and returns (text, metadata)
    tuples ready for indexing.
    """
    projects_path = Path(projects_dir).expanduser()
    if not projects_path.exists():
        print(f"  {projects_dir} not found — skipping Claude Code sessions")
        return []

    jsonl_files = sorted(projects_path.glob("**/*.jsonl"))
    print(f"  Found {len(jsonl_files)} session files")

    docs = []
    for jsonl_path in jsonl_files:
        session_id = jsonl_path.stem
        try:
            msgs = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception:
            continue

        result = parse_claude_code_session(msgs)
        if not result:
            continue
        body, meta = result

        fm_lines = [
            "---",
            f"id: {session_id}",
            f"title: {yaml_str(meta['title'])}",
            "source: claude-code",
        ]
        if cwd := meta.get("cwd"):
            fm_lines.append(f"project: {cwd}")
        if created := trim_iso(meta.get("created") or ""):
            fm_lines.append(f"created: {created}")
        fm_lines.append("---\n")
        text = "\n".join(fm_lines) + body

        out_path = conv_dir / f"{session_id}.md"
        out_path.write_text(text, encoding="utf-8")
        docs.append((text, {
            "source": str(out_path),
            "title": meta["title"],
            "id": session_id,
            "project": meta.get("cwd", ""),
            "created": meta.get("created", ""),
        }))

    return docs


# ---------------------------------------------------------------------------
# Topic summary generator
# ---------------------------------------------------------------------------

def generate_topic_summary(titles: list[str]) -> str | None:
    """
    Use the `claude` CLI to analyze conversation titles and produce a structured
    topic summary grouped by frequency tier (Dominant / Substantial / Moderate).

    Sends only titles — not full conversation text — so input scales at ~6 tokens
    per title. Returns None if `claude` is not on PATH or the call fails.
    """
    if not shutil.which("claude"):
        print("  `claude` CLI not found on PATH; skipping summary generation")
        return None

    title_block = "\n".join(titles)
    prompt = f"""Analyze these {len(titles)} conversation titles and produce a structured topic summary in markdown.

Group into frequency tiers based on how many conversations fall into each theme:
- **Dominant** (~100+ conversations): the largest recurring themes
- **Substantial** (~20–100 conversations): significant secondary themes
- **Moderate** (~10–30 conversations): smaller but notable clusters

For each tier, use a bullet list. Each bullet: topic name in bold, followed by an em dash and a brief parenthetical of key subtopics or representative terms. Aim for 3–6 bullets per tier.

Conversation titles (one per line):
{title_block}

Output only the markdown tiers. No preamble, no explanation, no intro sentence."""

    print(f"  Calling `claude` to analyze {len(titles)} titles...")
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"  claude CLI error: {result.stderr.strip()}")
        return None
    return result.stdout.strip()


def write_summary(summary: str, index_dir: Path, total: int) -> None:
    """Write the topic summary to conversations.summary.md in the index directory."""
    path = index_dir / "conversations.summary.md"
    header = f"# Conversation Index — Topic Summary\n\n_{total} conversations indexed._\n\n"
    path.write_text(header + summary + "\n", encoding="utf-8")
    print(f"Summary written to: {path}")


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def build(args):
    from leann.api import LeannBuilder

    index_dir = Path(args.index_dir).expanduser()
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = str(index_dir / "conversations.leann")

    if Path(f"{index_path}.meta.json").exists() and not args.force_rebuild:
        print(f"Index already exists at {index_path}. Use --force-rebuild to rebuild.")
        return

    # Clear per-conversation files on rebuild so stale conversations don't linger
    for subdir in ["chatgpt", "claude", "claude-code"]:
        d = index_dir / subdir
        if d.exists() and args.force_rebuild:
            shutil.rmtree(d)
            print(f"Cleared {d}")
        d.mkdir(parents=True, exist_ok=True)

    # Extract ZIPs and write individual conversation files
    print("Loading ChatGPT conversations...")
    chatgpt_docs = load_chatgpt_docs("downloads/chatgpt", index_dir / "chatgpt")
    print(f"  {len(chatgpt_docs)} ChatGPT conversations loaded")

    print("Loading Claude conversations...")
    claude_docs = load_claude_docs("downloads/claude", index_dir / "claude")
    print(f"  {len(claude_docs)} Claude conversations loaded")

    print("Loading Claude Code sessions...")
    claude_code_docs = load_claude_code_docs("~/.claude/projects", index_dir / "claude-code")
    print(f"  {len(claude_code_docs)} Claude Code sessions loaded")

    all_docs = chatgpt_docs + claude_docs + claude_code_docs
    if args.max_convos > 0:
        all_docs = all_docs[: args.max_convos]
        print(f"Limiting to {args.max_convos} conversations (--max-convos)")

    if not all_docs:
        print("No documents to index. Exiting.")
        return

    print(f"\nIndexing {len(all_docs)} conversations total...")

    builder = LeannBuilder(
        backend_name="hnsw",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        embedding_mode="sentence-transformers",
        graph_degree=32,
        complexity=64,
        is_compact=True,
        is_recompute=True,
        num_threads=1,
    )

    for i, (text, metadata) in enumerate(all_docs):
        builder.add_text(text, metadata)
        if (i + 1) % 200 == 0:
            print(f"  Added {i + 1}/{len(all_docs)}...")

    print("Building index structure (this takes a few minutes)...")
    builder.build_index(index_path)
    print(f"Index saved to: {index_path}")

    # Register the index directory so `leann list` and the MCP can discover it.
    # Must be called after build_index() so the .meta.json file exists.
    from leann.registry import register_project_directory
    register_project_directory(index_dir)
    print(f"Registered {index_dir} for leann discovery")

    # Generate topic summary via `claude` CLI (skipped if claude not on PATH)
    if not args.skip_summary:
        print("\nGenerating topic summary...")
        titles = [meta.get("title", "") for _, meta in all_docs if meta.get("title")]
        summary = generate_topic_summary(titles)
        if summary:
            write_summary(summary, index_dir, len(all_docs))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build LEANN index over conversation exports")
    parser.add_argument("--index-dir", default="~/.leann/indexes",
                        help="Directory to write the index and per-conversation files (default: ~/.leann/indexes)")
    parser.add_argument("--max-convos", type=int, default=-1,
                        help="Limit conversations (for testing, -1 = all)")
    parser.add_argument("--force-rebuild", action="store_true",
                        help="Clear existing conversation files and rebuild from scratch")
    parser.add_argument("--skip-summary", action="store_true",
                        help="Skip topic summary generation (no `claude` CLI call)")
    args = parser.parse_args()
    build(args)
