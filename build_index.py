"""
Build a LEANN semantic search index over ChatGPT and Claude conversation exports.

Parses both export formats (which differ from what LEANN's built-in readers expect)
and indexes each conversation as one text document. After indexing, calls the
`claude` CLI to generate a structured topic summary from conversation titles.

Usage:
    uv run python build_index.py [--max-convos N] [--force-rebuild] [--skip-summary]

Output:
    ~/.leann/indexes/conversations.leann  (+ .meta.json, .data files)
    ~/.leann/indexes/conversations.summary.md  (topic summary, requires `claude` on PATH)

Format notes:
  - ChatGPT export (new JSON format): downloads/chatgpt/conversations/conversations-NNN.json
      Each conversation has a `mapping` tree; we walk backward from `current_node`
      to reconstruct the active thread in chronological order.
  - Claude export: downloads/claude/<batch>/conversations.json
      Each conversation has `chat_messages` list with `text` and `sender` fields.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
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


def load_chatgpt_docs(export_dir: str) -> list[tuple[str, dict]]:
    """
    Load all ChatGPT conversations from conversations-NNN.json files.
    Returns list of (text, metadata) tuples.
    """
    docs = []
    pattern = str(Path(export_dir) / "conversations" / "conversations-*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"  No ChatGPT JSON files found at {pattern}")
        return docs

    for fpath in files:
        with open(fpath, encoding="utf-8") as f:
            conversations = json.load(f)
        for conv in conversations:
            text = parse_chatgpt_conversation(conv)
            if text:
                metadata = {
                    "source": "chatgpt",
                    "title": conv.get("title", "Untitled"),
                    "id": conv.get("id", ""),
                    "created": conv.get("create_time", 0),
                }
                docs.append((text, metadata))

    return docs


def load_claude_docs(export_dir: str) -> list[tuple[str, dict]]:
    """
    Load Claude conversations from the nested export directory structure.
    The export unpacks to: downloads/claude/<batch-id>/conversations.json
    Returns list of (text, metadata) tuples.
    """
    docs = []
    # Find conversations.json anywhere under export_dir
    pattern = str(Path(export_dir) / "**" / "conversations.json")
    files = glob.glob(pattern, recursive=True)
    if not files:
        print(f"  No Claude conversations.json found under {export_dir}")
        return docs

    for fpath in files:
        with open(fpath, encoding="utf-8") as f:
            conversations = json.load(f)
        for conv in conversations:
            text = parse_claude_conversation(conv)
            if text:
                metadata = {
                    "source": "claude",
                    "title": conv.get("name", "Untitled"),
                    "id": conv.get("uuid", ""),
                    "created": conv.get("created_at", ""),
                }
                docs.append((text, metadata))

    return docs


# ---------------------------------------------------------------------------
# Topic summary generator
# ---------------------------------------------------------------------------

def generate_topic_summary(titles: list[str]) -> str | None:
    """
    Use the `claude` CLI to analyze conversation titles and produce a structured
    topic summary grouped by frequency tier (Dominant / Substantial / Moderate).

    Sends only titles — not full conversation text — so input is ~10-15K tokens.
    Returns None if `claude` is not on PATH or the call fails.
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

    index_path = str(Path(args.index_dir).expanduser() / "conversations.leann")

    if Path(f"{index_path}.meta.json").exists() and not args.force_rebuild:
        print(f"Index already exists at {index_path}. Use --force-rebuild to rebuild.")
        return

    # Load documents from both sources
    print("Loading ChatGPT conversations...")
    chatgpt_docs = load_chatgpt_docs("downloads/chatgpt")
    print(f"  {len(chatgpt_docs)} ChatGPT conversations loaded")

    print("Loading Claude conversations...")
    claude_docs = load_claude_docs("downloads/claude")
    print(f"  {len(claude_docs)} Claude conversations loaded")

    all_docs = chatgpt_docs + claude_docs
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
    index_dir = Path(args.index_dir).expanduser()
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = str(index_dir / "conversations.leann")
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
    parser.add_argument("--index-dir", default="~/.leann/indexes", help="Directory to write the index (default: ~/.leann/indexes)")
    parser.add_argument("--max-convos", type=int, default=-1, help="Limit conversations (for testing, -1 = all)")
    parser.add_argument("--force-rebuild", action="store_true", help="Rebuild even if index exists")
    parser.add_argument("--skip-summary", action="store_true", help="Skip topic summary generation (no Anthropic API call)")
    args = parser.parse_args()
    build(args)
