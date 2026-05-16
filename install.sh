#!/usr/bin/env bash
# install.sh — Set up personal conversation search
#
# Idempotent setup script that:
#   1. Installs Homebrew dependencies needed by LEANN
#   2. Creates a Python 3.13 venv and installs LEANN
#   3. Registers the leann MCP server with Claude Code
#   4. Writes a user-level skill (~/.claude/skills/personal-search/) so
#      leann_search is documented and available in all Claude Code sessions
#   5. Builds the index if export data is present in downloads/; skips with
#      instructions if not (safe to run before exporting conversations)
#
# The user skill uses an @-import to reference ~/.leann/indexes/conversations.summary.md,
# so it reflects the current index content automatically whenever the index is rebuilt.
#
# Usage:
#   bash install.sh [--skip-summary]
#
#   --skip-summary  Skip topic summary generation when building the index

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
SKILL_DIR="$HOME/.claude/skills/personal-search"
SUMMARY_FILE="$HOME/.leann/indexes/conversations.summary.md"
INDEX_FILE="$HOME/.leann/indexes/conversations.leann.meta.json"

SKIP_SUMMARY=false
for arg in "$@"; do
    case "$arg" in
        --skip-summary) SKIP_SUMMARY=true ;;
    esac
done

echo "=== Personal Conversation Search — Install ==="
echo ""

# ── 1. Homebrew dependencies ──────────────────────────────────────────────────
echo "→ Checking Homebrew dependencies..."
BREW_DEPS=(libomp boost protobuf zeromq pkgconf)
MISSING=()
for dep in "${BREW_DEPS[@]}"; do
    brew list "$dep" &>/dev/null || MISSING+=("$dep")
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  Installing: ${MISSING[*]}"
    brew install "${MISSING[@]}"
else
    echo "  All present."
fi

# ── 2. Python 3.13 venv ───────────────────────────────────────────────────────
echo ""
echo "→ Python 3.13 venv..."
if [ ! -d "$VENV_DIR" ]; then
    uv venv --python 3.13 "$VENV_DIR"
    echo "  Created: $VENV_DIR"
else
    echo "  Exists: $VENV_DIR"
fi

# ── 3. Install LEANN ──────────────────────────────────────────────────────────
echo ""
echo "→ Installing LEANN..."
if "$VENV_DIR/bin/python" -c "import leann" 2>/dev/null; then
    echo "  Already installed."
else
    # libomp is keg-only on Apple Silicon; set linker flags for the build step
    export LDFLAGS="-L/opt/homebrew/opt/libomp/lib"
    export CPPFLAGS="-I/opt/homebrew/opt/libomp/include"
    (cd "$SCRIPT_DIR" && uv pip install leann)
fi

# ── 4. Register MCP server ────────────────────────────────────────────────────
echo ""
echo "→ Registering leann MCP server with Claude Code..."
MCP_BINARY="$VENV_DIR/bin/leann_mcp"
if [ ! -f "$MCP_BINARY" ]; then
    echo "  ERROR: $MCP_BINARY not found — did LEANN install successfully?"
    exit 1
fi
# Remove existing registration then re-add (idempotent)
claude mcp remove leann-server 2>/dev/null || true
claude mcp add leann-server -- "$MCP_BINARY"
echo "  Registered: $MCP_BINARY"

# ── 5. User-level skill ───────────────────────────────────────────────────────
# Writes ~/.claude/skills/personal-search/SKILL.md so leann_search is available
# and documented in every Claude Code session, not just this project.
# The @-import on the summary file means the skill always reflects the current
# index content — no manual update needed after rebuilding the index.
echo ""
echo "→ Writing user skill to $SKILL_DIR..."
mkdir -p "$SKILL_DIR"

cat > "$SKILL_DIR/SKILL.md" << SKILL_EOF
---
name: personal-search
description: >
  Search the user's personal AI conversation history (ChatGPT + Claude.ai exports)
  using the leann_search MCP tool. Use this skill whenever the user asks what they've
  discussed before, wants to find a past conversation, or asks about their history
  with any topic — even if they don't say "search" explicitly. Trigger on phrases
  like "have I talked about", "what did I discuss", "find in my history", "do I have
  any convos about", "what have I asked about before". Always use index_name
  "conversations".
---

# Personal Conversation Search

The user has a LEANN semantic search index over their personal AI conversation
history. Sources indexed:
- **ChatGPT** and **Claude.ai** exports (full conversations)
- **Claude Code** sessions from \`~/.claude/projects/\` (user prompts + assistant
  prose; tool calls filtered out)

Use \`leann_search\` to find relevant conversations. The \`source\` field in each
result identifies which source it came from (\`chatgpt\`, \`claude\`, \`claude-code\`).

## How to search

Call \`leann_search\` with:
- \`index_name\`: \`"conversations"\` (always this value)
- \`query\`: a natural-language description of what to find

Results include the conversation title, a text excerpt, and a \`source\` field
in the metadata — the absolute path to an individual conversation file on disk.
Read that file when the user needs the full conversation text.

## What's in the index

**BEGIN index summary** (auto-populated after first \`uv run python build_index.py\`):

@${SUMMARY_FILE}

**END index summary**

If the block above is empty, the index has not been built yet:
- If the user **explicitly** asked to search their conversation history, let them
  know the index doesn't exist yet and show them the "Updating the index" steps below.
- Otherwise (e.g. context-loading or incidental reference), treat it as no content
  found and continue normally.

## Updating the index

When the user wants to build or refresh the index with new exports:
1. Re-export from ChatGPT and/or Claude.ai (see README for export instructions)
2. Extract into \`downloads/chatgpt/\` and \`downloads/claude/\` in the project
3. Run: \`uv run python build_index.py --force-rebuild\`

The topic summary above is regenerated automatically — no skill edit needed.
SKILL_EOF

echo "  Written: $SKILL_DIR/SKILL.md"

# ── 6. Build index (auto-skips if no export data is present) ──────────────────
echo ""
echo "→ Checking for export data..."
HAS_CHATGPT=$(ls "$SCRIPT_DIR/downloads/chatgpt/conversations/conversations-"*.json 2>/dev/null | head -1 || true)
HAS_CLAUDE=$(find "$SCRIPT_DIR/downloads/claude" -name "conversations.json" 2>/dev/null | head -1 || true)

INDEX_BUILT=false
if [ -z "$HAS_CHATGPT" ] && [ -z "$HAS_CLAUDE" ]; then
    echo "  No export data found — skipping index build."
else
    BUILD_ARGS=""
    [ "$SKIP_SUMMARY" = true ] && BUILD_ARGS="--skip-summary"
    (cd "$SCRIPT_DIR" && uv run python build_index.py $BUILD_ARGS)
    INDEX_BUILT=true
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Done! ==="
echo ""
echo "  ✓ leann MCP server registered"
echo "  ✓ User skill: $SKILL_DIR/SKILL.md"

if [ "$INDEX_BUILT" = true ]; then
    echo "  ✓ Index built"
    echo ""
    echo "Ask Claude naturally in any session:"
    echo "  'What have I discussed about Kubernetes?'"
    echo "  'Find conversations about salary negotiation'"
else
    echo "  ○ Index not yet built (no export data found)"
    echo ""
    echo "Next steps to enable search:"
    echo ""
    echo "  1. Export your conversations:"
    echo "       ChatGPT: Settings → Data Controls → Export → download ZIP"
    echo "                extract so downloads/chatgpt/conversations/conversations-000.json exists"
    echo "       Claude:  Settings → Privacy → Export Data → download ZIP"
    echo "                extract into downloads/claude/"
    echo ""
    echo "  2. Build the index:"
    echo "       uv run python build_index.py"
    echo ""
    echo "  3. Then ask Claude: 'What have I discussed about Kubernetes?'"
fi
