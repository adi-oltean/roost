#!/usr/bin/env bash
# Run the roost server forever. Lives in the tmux session `roost-web`, which
# restart.sh creates; the plain name `roost` is left for a session of yours.
#
# The restart pause is short on purpose: the terminal websocket is relayed
# through this process, so every second it is down is a terminal that has
# lost its connection and has to come back. Long enough not to spin hot on a
# server that cannot start, short enough that a deliberate restart is a blink.
cd "$(dirname "$0")"
while true; do
  python3 server.py
  rc=$?
  # 3: it refused to start (socket already served, unsafe directory). Trying
  # again every half second changes nothing; say so and wait.
  if [ "$rc" = 3 ]; then
    echo "roost server refused to start — retrying in 30s (ctrl-c to stop)"
    sleep 30
    continue
  fi
  echo "roost server exited — restarting (ctrl-c to stop)"
  sleep 0.5
done
