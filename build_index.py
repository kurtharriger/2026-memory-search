"""
Build LEANN semantic search indexes over personal AI conversation history.

Each source gets its own named index so searches can target a specific source:
  ~/.leann/indexes/chatgpt/      → leann search chatgpt
  ~/.leann/indexes/claude/       → leann search claude
  ~/.leann/indexes/claude-code/  → leann search claude-code

Individual conversation files live in each source's sources/ subdirectory:
  ~/.leann/indexes/<source>/sources/<id>.md

Topic summaries:
  ~/.leann/indexes/<source>/<source>.summary.md  (per-source)
  ~/.leann/indexes/summary.md                    (combined, @-imported by user skill)

Usage:
    uv run python build_index.py [options]

    --sources chatgpt,claude,claude-code   Which sources to build (default: all)
    --force-rebuild                        Clear and rebuild selected source indexes
    --skip-summary                         Skip topic summary generation
    --max-convos N                         Limit per-source count (for testing, -1 = all)
    --index-dir PATH                       Base directory (default: ~/.leann/indexes)

Export inputs:
  downloads/chatgpt/<any>.zip   ChatGPT export ZIP (no extraction needed)
  downloads/claude/<any>.zip    Claude.ai export ZIP (no extraction needed)
  ~/.claude/projects/           Claude Code JSONL sessions (read directly)
    Note: only top-level session files are indexed; nested agent sub-sessions
    (stored one directory deeper) are skipped.
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

from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import Document as LlamaDocument


VALID_SOURCES = ("chatgpt", "claude", "claude-code")

SOURCE_LABEL = {
    "chatgpt":    "ChatGPT",
    "claude":     "Claude",
    "claude-code": "Claude Code",
}

SOURCE_UNIT = {
    "chatgpt":    "conversations",
    "claude":     "conversations",
    "claude-code": "sessions",
}


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
    return re.sub(r'\.\d+Z$', 'Z', s)


def yaml_str(s: str) -> str:
    """
    Quote a string value for YAML frontmatter using JSON encoding.
    Handles colons (e.g. in URLs), special chars, and newlines safely.
    json.dumps produces double-quoted strings that are valid YAML scalar values.
    """
    return json.dumps(str(s))


_splitter = SentenceSplitter(chunk_size=512, chunk_overlap=128, separator=" ", paragraph_separator="\n\n")


def chunk_doc(text: str, metadata: dict) -> list[tuple[str, dict]]:
    """Split a document into overlapping sentence chunks, carrying metadata to each.

    chunk_size=512 tokens (~2000 chars) keeps each search result to a single
    focused passage rather than returning an entire conversation verbatim.
    Each chunk gets a unique id derived from the base id so add_text passage
    ids don't collide.
    """
    nodes = _splitter.get_nodes_from_documents([LlamaDocument(text=text)])
    base_id = metadata.get("id", "")
    return [
        (node.get_content(), {**metadata, "id": f"{base_id}_{i}" if base_id else str(i), "chunk_index": i})
        for i, node in enumerate(nodes)
    ]


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

    thread = []
    node_id = current_node
    while node_id:
        node = mapping.get(node_id, {})
        msg = node.get("message")
        if msg:
            role = msg.get("author", {}).get("role", "")
            parts = msg.get("content", {}).get("parts", [])
            text = " ".join(p for p in parts if isinstance(p, str)).strip()
            if text and role in ("user", "assistant"):
                thread.append((role, text))
        node_id = node.get("parent")

    if not thread:
        return None

    thread.reverse()
    lines = [f"Conversation: {title}", ""]
    for role, text in thread:
        lines.append(f"[{'User' if role == 'user' else 'ChatGPT'}]: {text}")
        lines.append("")
    return "\n".join(lines)


def parse_claude_conversation(conv: dict) -> str | None:
    """
    Convert one Claude conversation dict to a plain-text document.

    Claude exports use `chat_messages` (not `messages`) with `sender` (not
    `role`) and `text` (not `content`) fields.
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
        lines.append(f"[{'User' if sender == 'human' else 'Claude'}]: {text}")
        lines.append("")
        count += 1

    return "\n".join(lines) if count else None


def parse_claude_code_session(msgs: list[dict]) -> tuple[str, dict] | None:
    """
    Parse a Claude Code session (list of decoded JSONL dicts) into (body, meta).

    Indexed content:
      - User prompts: type=="user" where content is a string, or the text items
        within a content list (tool_result items are skipped).
      - Assistant prose: type=="assistant" content list text items only;
        tool_use and thinking blocks are skipped.

    Metadata extracted from ai-title and the first user/assistant message.
    Returns None if the session has no indexable text.
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

        if mtype in ("user", "assistant") and not session_id:
            session_id = msg.get("sessionId")
            cwd = msg.get("cwd")
            created = msg.get("timestamp")

        # Content is at msg["message"]["content"] for user/assistant messages
        content = (msg.get("message") or {}).get("content") or msg.get("content")

        if mtype == "user":
            if isinstance(content, str) and content.strip():
                exchanges.append(("User", content.strip()))
            elif isinstance(content, list):
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

    # Fall back to first user prompt as title (skip system-injection content)
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


# ---------------------------------------------------------------------------
# ZIP extraction (ChatGPT and Claude)
# ---------------------------------------------------------------------------

def extract_chatgpt_zip(zip_path: Path, sources_dir: Path) -> list[tuple[str, dict]]:
    """
    Extract ChatGPT conversations from a ZIP export.

    Finds conversations-NNN.json files inside the ZIP, writes one .md file per
    conversation to sources_dir. Returns (text, metadata) tuples for indexing.
    Metadata `source` is the written file path.
    """
    docs = []
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist()
                     if re.search(r'conversations.*\.json$', n)]
            if not names:
                print(f"    No conversations*.json in {zip_path.name}")
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

                fm = [
                    "---",
                    f"id: {conv_id}",
                    f"title: {yaml_str(conv.get('title', 'Untitled'))}",
                    "source: chatgpt",
                ]
                if created := unix_to_iso(conv.get("create_time")):
                    fm.append(f"created: {created}")
                if updated := unix_to_iso(conv.get("update_time")):
                    fm.append(f"updated: {updated}")
                if model := conv.get("default_model_slug"):
                    fm.append(f"model: {model}")
                fm.append("---\n")
                text = "\n".join(fm) + body

                out_path = sources_dir / f"{conv_id}.md"
                out_path.write_text(text, encoding="utf-8")
                docs.extend(chunk_doc(text, {
                    "source": str(out_path),
                    "title": conv.get("title", "Untitled"),
                    "id": conv_id,
                    "created": conv.get("create_time", 0),
                    "source_file_size": len(text.encode()),
                }))
    return docs


def extract_claude_zip(zip_path: Path, sources_dir: Path) -> list[tuple[str, dict]]:
    """
    Extract Claude conversations from a ZIP export.

    Finds conversations.json inside the ZIP (regardless of batch-folder nesting),
    writes one .md per conversation to sources_dir. Metadata `source` is the path.
    """
    docs = []
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist()
                     if n.endswith("conversations.json")]
            if not names:
                print(f"    No conversations.json in {zip_path.name}")
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

                fm = [
                    "---",
                    f"id: {conv_id}",
                    f"title: {yaml_str(conv.get('name', 'Untitled'))}",
                    "source: claude",
                ]
                if created := trim_iso(conv.get("created_at", "")):
                    fm.append(f"created: {created}")
                if updated := trim_iso(conv.get("updated_at", "")):
                    fm.append(f"updated: {updated}")
                if summary := (conv.get("summary") or "").strip():
                    fm.append(f"summary: {yaml_str(summary)}")
                fm.append("---\n")
                text = "\n".join(fm) + body

                out_path = sources_dir / f"{conv_id}.md"
                out_path.write_text(text, encoding="utf-8")
                docs.extend(chunk_doc(text, {
                    "source": str(out_path),
                    "title": conv.get("name", "Untitled"),
                    "id": conv_id,
                    "created": conv.get("created_at", ""),
                    "source_file_size": len(text.encode()),
                }))
    return docs


def load_chatgpt_docs(export_dir: str, sources_dir: Path) -> list[tuple[str, dict]]:
    """Find ZIP files in export_dir and extract ChatGPT conversations into sources_dir."""
    zips = sorted(Path(export_dir).glob("*.zip"))
    if not zips:
        print(f"    No ZIP files in {export_dir}")
        return []
    docs = []
    for zip_path in zips:
        print(f"    Extracting {zip_path.name}...")
        docs.extend(extract_chatgpt_zip(zip_path, sources_dir))
    return docs


def load_claude_docs(export_dir: str, sources_dir: Path) -> list[tuple[str, dict]]:
    """Find ZIP files in export_dir and extract Claude conversations into sources_dir."""
    zips = sorted(Path(export_dir).glob("*.zip"))
    if not zips:
        print(f"    No ZIP files in {export_dir}")
        return []
    docs = []
    for zip_path in zips:
        print(f"    Extracting {zip_path.name}...")
        docs.extend(extract_claude_zip(zip_path, sources_dir))
    return docs


def load_claude_code_docs(projects_dir: str, sources_dir: Path) -> list[tuple[str, dict]]:
    """
    Load Claude Code session transcripts from ~/.claude/projects/.

    Uses */*.jsonl glob (one level deep) to pick up only top-level session files
    and skip nested agent sub-sessions stored in session subdirectories.
    """
    projects_path = Path(projects_dir).expanduser()
    if not projects_path.exists():
        print(f"    {projects_dir} not found")
        return []

    # */*.jsonl: project-dir/session.jsonl — skips agent sub-sessions at depth 3
    jsonl_files = sorted(projects_path.glob("*/*.jsonl"))
    print(f"    Found {len(jsonl_files)} session files")

    docs = []
    for jsonl_path in jsonl_files:
        session_id = jsonl_path.stem
        try:
            msgs = [
                json.loads(line)
                for line in jsonl_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except Exception:
            continue

        result = parse_claude_code_session(msgs)
        if not result:
            continue
        body, meta = result

        fm = [
            "---",
            f"id: {session_id}",
            f"title: {yaml_str(meta['title'])}",
            "source: claude-code",
        ]
        if cwd := meta.get("cwd"):
            fm.append(f"project: {cwd}")
        if created := trim_iso(meta.get("created") or ""):
            fm.append(f"created: {created}")
        fm.append("---\n")
        text = "\n".join(fm) + body

        out_path = sources_dir / f"{session_id}.md"
        out_path.write_text(text, encoding="utf-8")
        docs.extend(chunk_doc(text, {
            "source": str(out_path),
            "title": meta["title"],
            "id": session_id,
            "project": meta.get("cwd", ""),
            "created": meta.get("created", ""),
            "source_file_size": len(text.encode()),
        }))
    return docs


# ---------------------------------------------------------------------------
# Topic summary generation
# ---------------------------------------------------------------------------

def generate_topic_summary(titles: list[str]) -> str | None:
    """
    Call the `claude` CLI to analyze conversation titles and return structured
    topic tiers (Dominant / Substantial / Moderate) as markdown.

    Sends only titles (~6 tokens each). Returns None if `claude` is not on PATH
    or the call fails.
    """
    if not shutil.which("claude"):
        print("    `claude` CLI not found; skipping summary")
        return None

    title_block = "\n".join(titles)
    prompt = f"""Analyze these {len(titles)} conversation titles and produce a structured topic summary in markdown.

Group into frequency tiers based on how many conversations fall into each theme:
- **Dominant** (~100+ conversations): the largest recurring themes
- **Substantial** (~20–100 conversations): significant secondary themes
- **Moderate** (~10–30 conversations): smaller but notable clusters

For each tier, use a bullet list. Each bullet: topic name in bold, followed by \
an em dash and a brief parenthetical of key subtopics or representative terms. \
Aim for 3–6 bullets per tier.

Conversation titles (one per line):
{title_block}

Output only the markdown tiers. No preamble, no explanation, no intro sentence."""

    print(f"    Calling `claude` to analyze {len(titles)} titles...")
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print(f"    claude CLI error: {result.stderr.strip()}")
        return None
    return result.stdout.strip()


def write_source_summary(tier_md: str, source_dir: Path, source: str, total: int) -> None:
    """Write the per-source summary file with a labelled section header."""
    label = SOURCE_LABEL[source]
    unit = SOURCE_UNIT[source]
    content = f"## {label} (`leann search {source}`)\n\n_{total} {unit}_\n\n{tier_md}\n"
    path = source_dir / f"{source}.summary.md"
    path.write_text(content, encoding="utf-8")
    print(f"    Summary: {path}")


def combine_summaries(base_dir: Path) -> None:
    """
    Concatenate all per-source summary files into ~/.leann/indexes/summary.md.

    Bumps tier headers (## Dominant → ### Dominant) so they nest cleanly under
    the source section headers (## ChatGPT, ## Claude, ## Claude Code).
    Only includes sources whose summary file exists.
    """
    sections = []
    for source in VALID_SOURCES:
        path = base_dir / source / f"{source}.summary.md"
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8").strip()
        # Bump all ## to ### so tier headers nest under source section
        content = re.sub(r'^## ', '### ', content, flags=re.MULTILINE)
        # Restore the first line (source section header) back to ##
        lines = content.split('\n')
        lines[0] = lines[0].replace('### ', '## ', 1)
        sections.append('\n'.join(lines))

    if not sections:
        return

    combined = "# Conversation Index — Summary\n\n" + "\n\n".join(sections) + "\n"
    path = base_dir / "summary.md"
    path.write_text(combined, encoding="utf-8")
    print(f"\nCombined summary written to: {path}")


# ---------------------------------------------------------------------------
# Per-source index builder
# ---------------------------------------------------------------------------

def build_source(args, base_dir: Path, source: str) -> bool:
    """
    Build (or skip) the index for one source. Returns True if index was built.
    """
    from leann.api import LeannBuilder

    source_dir = base_dir / source
    sources_dir = source_dir / "sources"
    index_path = str(source_dir / f"{source}.leann")

    if Path(f"{index_path}.meta.json").exists() and not args.force_rebuild:
        print(f"\n[{source}] Index exists — skipping. Use --force-rebuild to rebuild.")
        return False

    print(f"\n=== {SOURCE_LABEL[source]} ===")

    # Clear the source directory on rebuild so stale files don't linger
    if source_dir.exists() and args.force_rebuild:
        shutil.rmtree(source_dir)
        print(f"  Cleared {source_dir}")
    source_dir.mkdir(parents=True, exist_ok=True)
    sources_dir.mkdir(parents=True, exist_ok=True)

    # Load documents
    if source == "chatgpt":
        docs = load_chatgpt_docs("downloads/chatgpt", sources_dir)
    elif source == "claude":
        docs = load_claude_docs("downloads/claude", sources_dir)
    elif source == "claude-code":
        docs = load_claude_code_docs("~/.claude/projects", sources_dir)
    else:
        return False

    print(f"  {len(docs)} {SOURCE_UNIT[source]} loaded")

    if args.max_convos > 0:
        docs = docs[:args.max_convos]
        print(f"  Limited to {args.max_convos} (--max-convos)")

    if not docs:
        print("  Nothing to index.")
        return False

    # Build vector index
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
    for i, (text, metadata) in enumerate(docs):
        builder.add_text(text, metadata)
        if (i + 1) % 200 == 0:
            print(f"  Added {i + 1}/{len(docs)}...")

    print("  Building index structure...")
    builder.build_index(index_path)
    print(f"  Index: {index_path}")

    # Register so `leann search <source>` can find it
    from leann.registry import register_project_directory
    register_project_directory(source_dir)
    print(f"  Registered: {source_dir}")

    # Per-source summary
    if not args.skip_summary:
        titles = [meta.get("title", "") for _, meta in docs if meta.get("title")]
        tier_md = generate_topic_summary(titles)
        if tier_md:
            write_source_summary(tier_md, source_dir, source, len(docs))

    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build(args):
    base_dir = Path(args.index_dir).expanduser()
    base_dir.mkdir(parents=True, exist_ok=True)

    selected = [s.strip() for s in args.sources.split(",")]
    unknown = [s for s in selected if s not in VALID_SOURCES]
    if unknown:
        print(f"Unknown sources: {unknown}. Valid: {', '.join(VALID_SOURCES)}")
        return

    for source in selected:
        build_source(args, base_dir, source)

    # Always regenerate the combined summary (picks up any existing per-source files)
    if not args.skip_summary:
        combine_summaries(base_dir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build LEANN indexes over personal conversation history"
    )
    parser.add_argument(
        "--sources", default=",".join(VALID_SOURCES),
        help=f"Comma-separated sources to build (default: all). Valid: {', '.join(VALID_SOURCES)}",
    )
    parser.add_argument(
        "--index-dir", default="~/.leann/indexes",
        help="Base directory for all indexes (default: ~/.leann/indexes)",
    )
    parser.add_argument(
        "--max-convos", type=int, default=-1,
        help="Limit per-source conversation count (for testing, -1 = all)",
    )
    parser.add_argument(
        "--force-rebuild", action="store_true",
        help="Clear and rebuild selected source indexes from scratch",
    )
    parser.add_argument(
        "--skip-summary", action="store_true",
        help="Skip topic summary generation (no `claude` CLI call)",
    )
    args = parser.parse_args()
    build(args)
