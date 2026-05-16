"""
Build a LEANN semantic search index over ChatGPT and Claude conversation exports.

Parses both export formats (which differ from what LEANN's built-in readers expect)
and indexes each conversation as one text document.

Usage:
    source .venv/bin/activate
    python build_index.py [--max-convos N] [--force-rebuild]

Output:
    indexes/conversations.leann  (+ .meta.json, .data files)

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build LEANN index over conversation exports")
    parser.add_argument("--index-dir", default="~/.leann/indexes", help="Directory to write the index (default: ~/.leann/indexes)")
    parser.add_argument("--max-convos", type=int, default=-1, help="Limit conversations (for testing, -1 = all)")
    parser.add_argument("--force-rebuild", action="store_true", help="Rebuild even if index exists")
    args = parser.parse_args()
    build(args)
