#!/bin/zsh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

if [ -x "./.venv/bin/python" ]; then
  ./.venv/bin/python main.py --ui
else
  python3 main.py --ui
fi
