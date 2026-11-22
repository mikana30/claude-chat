#!/bin/bash
# One-off (2026-11-15): restart Mike's chat server on the handoff fixes once the current turn has ended.
# If it does not come back healthy, put the previous server.py back and restart again.
SID=0090c612
D=/home/mikana/projects/claude-chat
since=$(date -u +"%Y-%m-%d %H:%M:%S")
ok(){ systemctl --user is-active -q claude-chat.service && [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://100.116.82.95:8090/config)" = 200 ]; }
for i in $(seq 1 360); do
  if journalctl --user -u claude-chat.service --since "$since" --no-pager | grep -q "chat end session=$SID"; then
    systemctl --user restart claude-chat.service
    sleep 8
    if ok; then echo "restarted on the handoff fixes, healthy"; exit 0; fi
    cp -p $D/server.py $D/server.py.failed-handofffix-20261115
    cp -p $D/server.py.bak-pre-handofffix-20261115 $D/server.py
    systemctl --user restart claude-chat.service
    sleep 8
    ok && state="rolled back to the previous server.py and running" || state="STILL DOWN after rollback"
    /home/mikana/bin/chat-telegram "Chat server did not come back on the handoff fix; $state." 2>/dev/null || true
    echo "$state"; exit 1
  fi
  sleep 5
done
echo "turn never ended within 30 min; no restart done"
