#!/bin/bash
# One-off (2026-11-15): restart Mike's chat server so it loads autohandoff.py with the goals/decisions ledger,
# once the current turn has ended. If it does not come back healthy, put the previous autohandoff.py back.
SID=ed7d514e
B=/home/mikana/astrid-brain
since=$(date -u +"%Y-%m-%d %H:%M:%S")
ok(){ systemctl --user is-active -q claude-chat.service && [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://100.116.82.95:8090/config)" = 200 ]; }
for i in $(seq 1 360); do
  if journalctl --user -u claude-chat.service --since "$since" --no-pager | grep -q "chat end session=$SID"; then
    sleep 3
    systemctl --user restart claude-chat.service
    sleep 8
    if ok; then echo "restarted with the ledger, healthy"; exit 0; fi
    cp -p $B/autohandoff.py $B/autohandoff.py.failed-ledger-20261115
    cp -p $B/autohandoff.py.bak-pre-ledger-20261115 $B/autohandoff.py
    systemctl --user restart claude-chat.service
    sleep 8
    ok && state="rolled back to the previous autohandoff.py and running" || state="STILL DOWN after rollback"
    /home/mikana/bin/chat-telegram "Chat server did not come back with the ledger change; $state." 2>/dev/null || true
    echo "$state"; exit 1
  fi
  sleep 5
done
echo "turn never ended within 30 min; no restart done"
