#!/usr/bin/env bash
# Update Claude Code and restart the sessions still running the old build.
#
#   ./update-claude.sh              # update binary, restart idle stale sessions
#   ./update-claude.sh --check      # report only, change nothing
#   ./update-claude.sh --force      # also restart busy sessions (loses in-flight work)
#   ./update-claude.sh --only NAME  # just one session
#
# Why this exists: `claude update` replaces the binary on disk, but a running
# session keeps the old code in memory until it restarts. That shows up later
# as "Claude Code X does not support this model; version Y or newer is
# required" — the binary is fine, the session is stale.
#
# Restarts use `--resume <sessionId>`, NOT `--continue`. Resuming by ID brings
# back the same conversation and therefore the same remote-control bridge, so
# pinned claude.ai sessions keep working. `--continue` can fall through to a
# fresh conversation and silently orphan the pinned entry.
set -uo pipefail
cd "$(dirname "$0")"

CHECK=0; FORCE=0; ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --force) FORCE=1 ;;
    --only)  ONLY="${2:-}"; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# Identify the claude process this script is running *inside* by walking its
# own parent chain, and skip that one. Two cheaper ideas do not work: the
# session record keeps the tmux name it had at START (a reboot leaves you in
# "0", so a renamed session never matches), and tmux's pane_pid is the pane's
# shell, not the claude process. Without this, --force kills the session
# running the script.
SELF_PID=$(python3 -c "
import os
pid = os.getppid()
for _ in range(12):
    try:
        comm = open(f'/proc/{pid}/comm').read().strip()
        if comm == 'claude':
            print(pid); break
        pid = int(next(l for l in open(f'/proc/{pid}/status') if l.startswith('PPid:')).split()[1])
        if pid <= 1: break
    except Exception:
        break
print(0)
" 2>/dev/null || echo 0)
SELF_PID=$(printf '%s\n' "$SELF_PID" | head -1)
case "$SELF_PID" in ''|*[!0-9]*) SELF_PID=0 ;; esac

if [ "$CHECK" = 0 ]; then
  echo "== claude update"
  claude update 2>&1 | tail -3
fi
CUR=$(claude --version 2>/dev/null | awk '{print $1}')
if [ -z "$CUR" ]; then
  echo "cannot read a version from 'claude --version' — refusing to call every session stale" >&2
  exit 1
fi
echo "binary version: $CUR"

# Live sessions whose recorded version differs from the binary.
mapfile -t STALE < <(python3 - "$CUR" "$ONLY" <<'PY'
import json, glob, sys
from pathlib import Path
cur, only = sys.argv[1], sys.argv[2]
for f in glob.glob(str(Path.home() / ".claude/sessions/*.json")):
    try:
        j = json.load(open(f))
        if Path(f"/proc/{j['pid']}/comm").read_text().strip() != "claude":
            continue
    except Exception:
        continue
    name = (j.get("tmux") or "").split(":")[0]
    if not name or j.get("version") == cur:
        continue
    if only and name != only:
        continue
    print("\t".join([name, j.get("version", "?"), j.get("sessionId", ""), j.get("status", "?"), str(j.get("pid", 0))]))
PY
)

[ ${#STALE[@]} -eq 0 ] && { echo "every live session is on $CUR — nothing to do"; exit 0; }

echo "== stale sessions"
for row in "${STALE[@]}"; do
  IFS=$'\t' read -r name ver sid status pid <<<"$row"
  note=""
  [ "$pid" = "$SELF_PID" ] && note="  (this pane — restart it yourself)"
  [ "$status" != "idle" ] && [ "$FORCE" = 0 ] && note="$note  (busy: $status — skipped, use --force)"
  printf "  %-12s v%-9s %s%s\n" "$name" "$ver" "$status" "$note"
done
[ "$CHECK" = 1 ] && { echo "(--check: nothing changed)"; exit 0; }

restart_one() {
  local name="$1" sid="$2"
  # The id is typed into a shell below; it must be an id and nothing more.
  [[ $sid =~ ^[0-9a-fA-F-]{36}$ ]] || { echo "    bad session id — left alone"; return 1; }
  # /exit rather than C-c: Ctrl-C only clears the TUI input line, so a command
  # sent after it is typed into the conversation instead of the shell.
  tmux send-keys -t "$name:" -l "/exit"; sleep 1; tmux send-keys -t "$name:" Enter
  for _ in $(seq 1 30); do
    [ "$(tmux display-message -p -t "$name:" '#{pane_current_command}' 2>/dev/null)" = "bash" ] && break
    sleep 2
  done
  if [ "$(tmux display-message -p -t "$name:" '#{pane_current_command}' 2>/dev/null)" != "bash" ]; then
    echo "    did not reach a shell — left alone"; return 1
  fi
  tmux send-keys -t "$name:" -l "claude --resume $sid --autocompact 1m"
  sleep 1; tmux send-keys -t "$name:" Enter
  for _ in $(seq 1 40); do
    [ "$(tmux display-message -p -t "$name:" '#{pane_current_command}' 2>/dev/null)" != "bash" ] && break
    sleep 2
  done
  return 0
}

echo "== restarting"
for row in "${STALE[@]}"; do
  IFS=$'\t' read -r name ver sid status pid <<<"$row"
  [ "$pid" = "$SELF_PID" ] && { echo "  $name: this pane, skipped"; continue; }
  [ "$status" != "idle" ] && [ "$FORCE" = 0 ] && { echo "  $name: $status, skipped"; continue; }
  printf "  %-12s v%s -> " "$name" "$ver"
  restart_one "$name" "$sid" && echo "restarted" || true
done

sleep 10
echo "== after"
python3 - "$CUR" <<'PY'
import json, glob, sys
from pathlib import Path
cur = sys.argv[1]
for f in sorted(glob.glob(str(Path.home() / ".claude/sessions/*.json"))):
    try:
        j = json.load(open(f))
        if Path(f"/proc/{j['pid']}/comm").read_text().strip() != "claude":
            continue
    except Exception:
        continue
    name = (j.get("tmux") or "").split(":")[0]
    if not name:
        continue
    mark = "ok   " if j.get("version") == cur else "STALE"
    print(f"  {mark} {name:12} v{j.get('version','?'):9} bridge={j.get('bridgeSessionId') or '-'}")
PY
echo "pinned links survive: sessions resume by ID, keeping their bridge."
