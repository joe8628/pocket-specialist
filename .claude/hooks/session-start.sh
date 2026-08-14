#!/usr/bin/env bash
# SessionStart hook: inject the *volatile* session context.
# ANCHOR.md (North Star + Locked Decisions) is already loaded via CLAUDE.md's
# `@ANCHOR.md` import, so this only adds what changes between sessions.
# For SessionStart, stdout on exit 0 is added to Claude's context directly.
# Note: since Claude Code 2.1.0 this injection is SILENT — Claude sees it, but
# it is not displayed in the user-visible transcript.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

echo "=== Session anchor (volatile state from STATE.md) ==="
if [ -f "$ROOT/STATE.md" ]; then
  # Print the "## Current Focus" section up to the next "## " heading.
  awk '/^## Current Focus/{f=1;print;next} /^## /{f=0} f' "$ROOT/STATE.md"
else
  echo "(STATE.md not found)"
fi

branch="$(git -C "$ROOT" branch --show-current 2>/dev/null || true)"
[ -n "$branch" ] && echo "Active branch: $branch"

echo
echo "Before resuming: re-read CLAUDE.md + STATE.md, and check wiki/INDEX.md"
echo "before proposing any new approach. Run /reground if unsure."
