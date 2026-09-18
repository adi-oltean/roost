#!/usr/bin/env bash
# Attach to (or create) a named dtach session. Run BY ttyd — one ttyd serves
# every session, and the name arrives as $1 from ttyd's --url-arg, so the URL
# is  …/?arg=<name>  rather than a port per session.
set -uo pipefail
umask 077
DIR="$HOME/.dtach"
name="${1:-}"

list() {
  echo "sessions:"
  for f in "$DIR"/*.cmd; do
    [ -e "$f" ] || continue
    b=$(basename "$f" .cmd)
    live=" "; [ -S "$DIR/$b" ] && live="*"
    printf "  %s %-14s %s\n" "$live" "$b" "$(head -c 70 "$f")"
  done
  echo
  echo "  * = running.  Open one with  ?arg=<name>"
}

# The name indexes a file, so constrain it before it touches the filesystem —
# it arrives from a query string.
case "$name" in
  *[!A-Za-z0-9_-]*|"") list; echo; echo "(no session selected)"; sleep 20; exit 0 ;;
esac
[ -f "$DIR/$name.cmd" ] || { echo "no such session: $name"; echo; list; sleep 20; exit 0; }

cmd=$(cat "$DIR/$name.cmd")
# -A: attach if the session exists, create it otherwise. bash -lc because the
# command is a shell string (dtach itself execs, it is not a shell).
# -r winch, not the default ctrl_l: codex refuses Ctrl+L while a task is
# running ("Ctrl+L is disabled while a task is in progress"), so on reattach
# the screen was never repainted and the terminal came back blank. A WINCH
# signal makes the TUI redraw itself regardless of what it is doing.
# The browser must never BE the session. `dtach -A` creates one when the
# socket is missing, which makes that first client the master: the session
# then lives inside a tab, and closing the tab takes it down. So create it
# detached first -- exactly what the Start button does -- and attach to it
# as a plain client afterwards. A client can come and go freely; that is
# what lets a page hand the terminal over to another page.
if [ ! -S "$DIR/$name" ]; then
  [ -e "$DIR/$name" ] && rm -f "$DIR/$name"     # a stale file, not a socket
  dtach -n "$DIR/$name" -r winch bash -lc "$cmd"
  for _ in $(seq 1 40); do [ -S "$DIR/$name" ] && break; sleep .05; done
fi
exec dtach -a "$DIR/$name" -r winch
