#!/bin/zsh

set -eu

PROJECT_DIR=${0:A:h:h}
cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR/src"
exec /usr/bin/python3 -m control_plane.main --config config/projects.local.json serve
