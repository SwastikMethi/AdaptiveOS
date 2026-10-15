#!/bin/bash
set -euo pipefail
umask 077
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ ! -x "$project_root/.venv/bin/python" ]]; then
    echo "Missing .venv. Run from the project folder:" >&2
    echo "  python3.11 -m venv .venv" >&2
    echo "  .venv/bin/python -m pip install -e '.[desktop]'" >&2
    exit 1
fi
exec "$project_root/.venv/bin/python" "$project_root/scripts/desktop_launcher.py" start "$@"
