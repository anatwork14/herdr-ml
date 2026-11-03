# Manual Core recovery validation — cycle 2 evidence

Herdr `0.9.5` (`herdr --version`; `post/` status), protocol 22. Starting PR #2 head `fbe35d41fb607e2d78f95fa928e0e7f27e43b68a`. The `HERDR_ENV` check passed in pane `w5:p2`, workspace `w5`, run in the session (not saved as a file).

Isolation design: the agent under test ran in a **named disposable session `mlrec-c2`** with its own server and socket (`sessions/mlrec-c2/`). Its workspace is also called `w1` inside that session, which is the same label as a production workspace in the default session. Every command that touched that session carried `--session mlrec-c2`. The default session was never stopped, and the production server was not restarted. The named session was stopped and deleted at the end.

## Agent identity

- Agent: `c2-agent` (Claude), session `7ddaa64d-8a22-40dc-872c-1d5fc5dc60a8`.
- Pane `w1:p1` and terminal `term_65f5d670305601` in session `mlrec-c2`.
- Workspace `w1` (`mlc2-disposable`), cwd is a disposable git fixture.
- Token: `named/token.txt`.

## Test A — agent-pane detach and reattach

Target: the Claude agent's own terminal `term_65f5d670305601`. This is the requirement the previous cycle left untested.

| Step | Command | Exit | Result | Artifact |
|---|---|---|---|---|
| Turn 1 (token and primes 53, 59, 61) | `agent prompt c2-agent … --wait` | 0 | settled `idle`; replies `53`, `59`, `61` in scrollback | `named/06-*` |
| Observer on agent terminal, 40 s, turn 2 sent during it | `timeout 40 herdr --session mlrec-c2 terminal session observe term_65f5d670305601` | 124 | 10 frames: seq 1 `full=true` snapshot (contains token), seq 2–10 `full=false` deltas; frame seq 9 contains a standalone `67` (turn 2 answer) | `named/07-observerA.out`, `observer-A-frames.txt` |
| Detach with documented `ctrl+b q` | `pty_attach_once.py mlrec-c2 term_65f5d670305601 …` | 0 | detach at 11:18:55.116Z, client exited 11:18:55.117Z | `named/08-pty-attach-1.*` |
| Agent works while detached (turn 3) | `agent prompt … --wait` | 0 | settled `idle`; expected answer `71` in transcript | `named/09-*` |
| Reattach | `pty_attach_once.py …` | 0 | attach at 11:19:02.576Z, detach 11:19:07.612Z, exit 0, 9501 bytes before detach | `named/10-pty-attach-2.*` |
| Continuity (recall of token after reattach) | `agent prompt … --wait` with marker | 0 | `marker_found=True token_after_marker=True` | `named/11-recall-*` |

Limit: the observer's streamed deltas were produced by turn 2, which ran during the observer's window. The reattach is a pty attach with a snapshot read, not a long-running observer on the reattached view.

## Test B — Herdr-native restoration

Inspection (`herdr agent --help`, `herdr session --help`, `herdr server --help`, `herdr api schema --json`):
- The schema defines `resume_argv` on `pane report-agent` and `report-agent-session`, described as "Command that resumes this agent's session after a Herdr restart". It is an integration-supplied field.
- `pane get` for the agent reported `resume_argv: null` and `restore_error: null` before and after the restart (`named/12-pane-before-stop.json`, `named/14-*`, `named/15-*`).
- `herdr integration status` reports the installed Claude integration as **outdated (v9 < v11)**. Upgrading it changes the user's global Claude hooks, which also affect production agents. It was not upgraded.
- Herdr persists agent identity in the session state file `sessions/mlrec-c2/session.json`: `agent_name: c2-agent`, `managed_agent_kind: claude`, and `agent_session.value: 7ddaa64d-…` for the pane.

Controlled restart, on the disposable session only:
1. `herdr --session mlrec-c2 server stop` (exit 0, `named/13-*`). The session was `running: false`, and the agent's Claude process was gone (no Claude process with the fixture cwd).
2. `nohup herdr --session mlrec-c2 server` with no agent arguments (`named/14-*`).
3. After restart, `agent list` showed `c2-agent`, `w1:p1`, `claude`, `idle`, same session ID.
4. A live process `claude --resume 7ddaa64d-8a22-40dc-872c-1d5fc5dc60a8` exists. Its ancestry is bash pane shell → `herdr --session mlrec-c2 server` → init (`named/16-process-ancestry.txt`). The `--resume` argument was supplied by Herdr, not by the operator.
5. A recall prompt with a unique marker was answered with the token after the marker (`named/17-recall-check.txt`: `marker_found=True token_after_marker=True`).

Finding: on Herdr 0.9.5 with the installed (outdated) Claude integration, Herdr restored the agent after a clean named-session server restart by relaunching `claude --resume <persisted session id>`, and conversation continuity was shown by recall. Herdr-native restore appears to rely on the persisted `agent_session`, not on the `resume_argv` field, which was null throughout.

Not tested for Test B:
- A server crash (`kill -9` or power loss). Only a clean `server stop` was exercised.
- Restore with an up-to-date Claude integration, or with `resume_argv` reported by the integration.
- Restore of the production default session. It was not stopped.
- Restore after the Claude process died independently of the server.

## Test C — production isolation

Baseline: `baseline/` (taken before any cycle-2 command). Post: `post/`. Comparison: `post/comparison.txt`. Fields were split into stable identifiers and operational state (`post/comparison.txt`) and transient focus/scroll/revision/seq fields.

Result:
- Stable identifiers and operational fields: **0 differences** in workspaces, tabs, agents (name, pane, kind, session), sessions, and panes `w1`, `w3`, `w5`, `w6`. The default session still runs with the same workspace set.
- Transient differences, reported without attribution:
  - `w1:p1` agy agent (default session): `agent_status` `idle` → `done`, `completion_seq` 177 → 180, `state_change_seq` 177 → 180. `w1` workspace and tab `agent_status` also `idle` → `done`.
  - `w1:p1` `scroll.max_offset_from_bottom` 104 → 185.
  - `w6:p1` `scroll.max_offset_from_bottom` 41 → 55.
- Focus fields did not differ in this cycle.
- Not shown: the agy agent's completion (three completion sequence increments) happened during this session, and it may be the user's own work. Every command this cycle that targeted `w1:p1` carried `--session mlrec-c2`, and no default-session command wrote input. The cause of the agy change and the scroll changes is **unverified**.

The files are not byte-identical with the baseline, so isolation is not claimed as byte-identical.

## Cleanup

- `c2-agent` exited with `/exit`. No Claude process with the fixture cwd remained.
- Named server stopped (`named/19-server-stop.*`, exit 0).
- Named session deleted (`named/20-session-delete.json`, `deleted: true`). `sessions/` contains no `mlrec-c2` directory afterward. Session list: only `default`, running.
- Disposable fixture under the scratchpad was not removed; it is outside the repository.

## Files

- `named/` — all raw outputs for the named-session agent, with step-numbered names, timestamps (`*.ts`), exit codes (`*.exit`), and the pty and observer captures.
- `pty_attach_once.py` — one attach/detach through the documented detach key, with the session passed explicitly.
- `observer-A-frames.txt` — decoded observer frame table.
- `baseline/`, `post/` — inventories before and after; `post/comparison.txt` — the field-by-field comparison.
