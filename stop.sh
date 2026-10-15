#!/bin/bash
set -euo pipefail
umask 077
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ ! -x "$project_root/.venv/bin/python" ]]; then
    echo "Missing .venv/bin/python. Restore the project virtual environment to use stop.sh." >&2
    exit 1
fi
exec "$project_root/.venv/bin/python" "$project_root/scripts/desktop_launcher.py" stop "$@"
