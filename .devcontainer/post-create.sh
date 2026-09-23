#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[studio]'
npm install -g @openai/codex@0.156.1

# LangGraph's Studio config references .env. Codespaces Secrets remain in the
# process environment; create only an empty, private placeholder if needed.
if [[ ! -e .env ]]; then
  (umask 077; : > .env)
fi

.venv/bin/python --version
node --version
codex --version
