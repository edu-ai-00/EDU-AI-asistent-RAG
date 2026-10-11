#!/bin/sh
# Thin wrapper: the runner is scripts/migrate.py (standard library only).
PY=python3
"$PY" -c '' >/dev/null 2>&1 || PY=python
exec "$PY" "$(dirname "$0")/migrate.py" "$@"
