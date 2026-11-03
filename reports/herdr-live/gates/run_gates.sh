#!/usr/bin/env bash
# Re-run of the TERMINAL_AND_LOGS_READY and GIT_WORKTREE_READY checks with every
# output saved. Agent-free. Runs only in the disposable named session "mlrec-gate".
#
# Usage: run_gates.sh <scratch_dir> <out_dir>
# Every herdr/git call is logged to <out_dir>/commands.log as:
#   <utc start> <utc end> <exit> <argv...>
set -u
SCRATCH="$1"
OUT="$2"
SESSION=mlrec-gate
FX="$SCRATCH/gate-fixture"
WT="$SCRATCH/gate-wt"
mkdir -p "$OUT/steps" "$OUT/logs"
: > "$OUT/commands.log"

run() {
  # run <step-name> <cmd...>   stdout -> steps/<step>.out, stderr -> steps/<step>.err
  local name="$1"; shift
  local t0 t1 rc
  t0=$(date -u +%FT%T.%3NZ)
  "$@" > "$OUT/steps/$name.out" 2> "$OUT/steps/$name.err"
  rc=$?
  t1=$(date -u +%FT%T.%3NZ)
  echo "$name" "$t0" "$t1" "$rc" "$*" >> "$OUT/commands.log"
  echo "$rc" > "$OUT/steps/$name.exit"
  return 0
}

H="herdr --session $SESSION"

# 0. Fresh fixture repository (outside the project tree).
rm -rf "$FX" "$WT"; mkdir -p "$FX"
git -C "$FX" init -q -b main
git -C "$FX" -c user.email=gate@example.invalid -c user.name=gate commit -q --allow-empty -m init
run fixture-head git -C "$FX" rev-parse --abbrev-ref HEAD

# 1. Named disposable session server (its own socket; default server untouched).
(nohup herdr --session $SESSION server > "$OUT/steps/server.out" 2> "$OUT/steps/server.err" < /dev/null &)
sleep 4
run session-status $H status

# 2. Workspace, tab, pane run with tee log, pane read, pane metadata.
run workspace-create $H workspace create --cwd "$FX" --label gate-disposable --no-focus
PANE=$(python3 -c "import json;print(json.load(open('$OUT/steps/workspace-create.out'))['result']['root_pane']['pane_id'])")
run tab-create $H tab create --workspace w1 --label gate-tab2 --no-focus
run pane-run $H pane run "$PANE" "cd '$FX' && mkdir -p logs && (for i in 1 2 3; do echo GATE_LOG_LINE_\$i; done) | tee logs/gate.log; echo GATE_\"DONE\""
run pane-wait $H pane wait-output "$PANE" --match GATE_DONE --timeout 30000
run pane-read $H pane read "$PANE" --source recent-unwrapped --lines 40
run pane-get $H pane get "$PANE"
run pane-process-info $H pane process-info --pane "$PANE"
cp "$FX/logs/gate.log" "$OUT/logs/gate.log"
run workspace-get $H workspace get w1

# 3. Git worktree: create via Herdr, commit in the linked checkout, verify isolation, remove.
run worktree-create $H worktree create --cwd "$FX" --branch gate-wt --path "$WT" --label gate-wt --no-focus
WTWS=$(python3 -c "import json;print(json.load(open('$OUT/steps/worktree-create.out'))['result']['workspace']['workspace_id'])")
run worktree-list-before-commit git -C "$FX" worktree list
echo "isolation-probe" > "$WT/ISOLATION.txt"
run wt-commit git -C "$WT" -c user.email=gate@example.invalid -c user.name=gate add ISOLATION.txt
run wt-commit2 git -C "$WT" -c user.email=gate@example.invalid -c user.name=gate commit -q -m isolation
run wt-head git -C "$WT" rev-parse --short HEAD
run main-head git -C "$FX" rev-parse --short HEAD
if [ -e "$FX/ISOLATION.txt" ]; then echo LEAK > "$OUT/steps/isolation-check.out"; else echo MAIN_CLEAN > "$OUT/steps/isolation-check.out"; fi
echo 0 > "$OUT/steps/isolation-check.exit"
run worktree-remove $H worktree remove --workspace "$WTWS"
run branch-delete git -C "$FX" branch -D gate-wt
run worktree-list-after git -C "$FX" worktree list
run branch-after git -C "$FX" branch --list gate-wt

# 4. Cleanup: stop the named server, delete the named session.
run server-stop $H server stop
sleep 3
run session-delete herdr session delete $SESSION --json
run sessions-after herdr session list --json
echo "done $(date -u +%FT%T.%3NZ)" >> "$OUT/commands.log"
