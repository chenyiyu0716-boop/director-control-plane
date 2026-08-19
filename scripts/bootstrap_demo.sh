#!/bin/zsh

set -eu

PROJECT_DIR=${0:A:h:h}
cd "$PROJECT_DIR"

init_fixture() {
  local dir=$1
  if [[ ! -d "$dir/.git" ]]; then
    git -C "$dir" init -q
    git -C "$dir" config user.email "demo@example.invalid"
    git -C "$dir" config user.name "Chief Demo"
  fi
  if ! git -C "$dir" rev-parse --verify HEAD >/dev/null 2>&1; then
    git -C "$dir" add docs
    git -C "$dir" commit -qm "demo fixture"
  fi
}

init_fixture fixtures/demo-julius
init_fixture fixtures/demo-director

export PYTHONPATH="$PROJECT_DIR/src"
export CONTROL_PLANE_DB="$PROJECT_DIR/var/demo-control-plane.sqlite3"
CONFIG="$PROJECT_DIR/config/projects.example.json"

python3 -m control_plane.main --config "$CONFIG" init-db
python3 -m control_plane.main --config "$CONFIG" run-all --trigger manual
print -- "--- projects ---"
python3 -m control_plane.main --config "$CONFIG" list project
print -- "--- checks ---"
python3 -m control_plane.main --config "$CONFIG" list check_result
