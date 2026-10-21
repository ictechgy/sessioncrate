#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s tests -t . -v
python3 bin/codex-vault --help >/dev/null
python3 skills/codex-vault/scripts/codex-vault --version
python3 bin/sessioncrate --help >/dev/null
python3 skills/sessioncrate/scripts/sessioncrate --version
python3 Scripts/demo.py
