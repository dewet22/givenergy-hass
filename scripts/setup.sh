#!/usr/bin/env bash
# One-shot local development setup. Idempotent — safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

# A uv-*managed* interpreter matching .python-version. prek builds each hook in
# its own venv via `uv venv --python-preference managed --no-python-downloads`;
# with no managed interpreter present, uv falls back to the system python (macOS
# ships 3.9.6) and hooks requiring >=3.10 (e.g. pyupgrade) fail to install,
# aborting the whole pre-commit run. `uv sync` alone does NOT guarantee this — it
# happily uses a system python for the project venv — so provision it explicitly.
echo "==> Installing the managed Python pinned in .python-version"
uv python install

echo "==> Installing dependencies (uv sync --dev)"
uv sync --dev

# Git hooks live in .git/hooks, which git never clones — so this is irreducibly
# per-clone. prek is a local-only tool (CI runs HACS + hassfest, not these hooks).
if command -v prek >/dev/null 2>&1; then
  echo "==> Wiring the pre-commit hook (prek install)"
  prek install
else
  echo "!! prek not found — install it (https://prek.j178.dev, e.g. 'brew install prek')"
  echo "   then re-run this script to wire the git pre-commit hook."
  exit 1
fi

echo "==> Done."
