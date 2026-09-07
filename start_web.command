#!/bin/zsh
project_root="${0:A:h}"
cd "$project_root" || exit 1
exec "$project_root/.venv/bin/python" "$project_root/web.py"
