#!/usr/bin/env bash
# Creates workspace/tabs for existing Git checkouts only. No cloning/training.
set -euo pipefail

# Fail closed until server-scoped identity and crash reconciliation exist.
echo "BLOCKED: Herdr bootstrap reconciliation is not implemented; do not create or reuse workspaces with this script" >&2
exit 2

for bin in herdr jq python3; do
  command -v "$bin" >/dev/null || { echo "Missing command: $bin" >&2; exit 2; }
done
herdr status server >/dev/null || { echo "Start Herdr first, then detach." >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${HERDR_ML_CONFIG:-$SCRIPT_DIR/../config/projects.json}"
ROOT="${RESEARCH_ROOT:-$HOME/research}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/herdr-ml"
STATE="$STATE_DIR/workspaces.tsv"
mkdir -p "$ROOT" "$STATE_DIR"
touch "$STATE"
if command -v flock >/dev/null; then
  exec 9>"$STATE.lock"
  flock -n 9 || { echo "Another bootstrap is running" >&2; exit 2; }
fi

while IFS= read -r project; do
  id="$(jq -er '.id' <<< "$project")"
  label="$(jq -er '.label' <<< "$project")"
  directory="$(jq -er '.directory' <<< "$project")"
  if [[ ! "$id" =~ ^[a-z0-9][a-z0-9_-]*$ || ! "$directory" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
    echo "Unsafe project id/directory: $id / $directory" >&2
    exit 2
  fi
  checkout="$ROOT/$directory"
  if [[ ! -d "$checkout/.git" && ! -f "$checkout/.git" ]]; then
    echo "SKIP $id: not a Git checkout: $checkout"
    continue
  fi
  runtime="$ROOT/.runtime/$id"
  mkdir -p "$runtime/logs" "$runtime/runs" "$runtime/reports"
  touch "$runtime/logs/system.log"

  existing="$(awk -F '\t' -v p="$id" '$1==p { print $2; exit }' "$STATE")"
  if [[ -n "$existing" ]] && herdr workspace get "$existing" >/dev/null 2>&1; then
    echo "OK $id: existing workspace $existing"
    continue
  fi

  created="$(herdr workspace create --cwd "$checkout" --label "$label" --no-focus)"
  workspace="$(jq -er '.result.workspace.workspace_id' <<< "$created")"
  first_tab="$(jq -er '.result.tab.tab_id' <<< "$created")"
  herdr tab rename "$first_tab" "$(jq -er '.tabs[0]' <<< "$project")" >/dev/null

  while IFS= read -r tab_name; do
    response="$(herdr tab create --workspace "$workspace" --cwd "$checkout" --label "$tab_name" --no-focus)"
    pane="$(jq -er '.result.root_pane.pane_id' <<< "$response")"
    case "$tab_name" in
      "04 Runs")
        if command -v pueue >/dev/null && pueue status >/dev/null 2>&1; then
          herdr pane run "$pane" "watch -n 4 pueue status" >/dev/null
        fi
        ;;
      "05 Logs")
        printf -v cmd 'tail -n 50 -F %q' "$runtime/logs/system.log"
        herdr pane run "$pane" "$cmd" >/dev/null
        ;;
      "06 GPU")
        if command -v nvidia-smi >/dev/null 2>&1; then
          herdr pane run "$pane" "watch -n 3 nvidia-smi" >/dev/null
        fi
        ;;
    esac
  done < <(jq -r '.tabs[1:][]' <<< "$project")

  awk -F '\t' -v p="$id" '$1!=p' "$STATE" > "$STATE.new"
  printf '%s\t%s\t%s\n' "$id" "$workspace" "$checkout" >> "$STATE.new"
  mv "$STATE.new" "$STATE"
  echo "CREATED $id -> $workspace"
done < <(jq -c '.projects[]' "$CONFIG")
echo "Done. Attach to Herdr to view workspaces."
