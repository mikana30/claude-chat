#!/bin/bash
# One-off (2026-11-16): restart Mike's chat server so it loads voice ID (chat_voice.py) once the current turn
# has ended. If it does not come back healthy, put the previous server.py back and drop CHAT_VOICEID.
SID=7dcc143f
D=/home/mikana/projects/claude-chat
since=$(date -u +"%Y-%m-%d %H:%M:%S")
ok(){ systemctl --user is-active -q claude-chat.service && [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://100.116.82.95:8090/config)" = 200 ]; }
for i in $(seq 1 360); do
  if journalctl --user -u claude-chat.service --since "$since" --no-pager | grep -q "chat end session=$SID"; then
    sleep 3
    systemctl --user restart claude-chat.service
    sleep 8
    if ok; then echo "restarted with voice ID, healthy"; exit 0; fi
    cp -p $D/server.py $D/server.py.failed-chatvoice-20261116
    cp -p $D/server.py.bak-pre-chatvoice-20261116 $D/server.py
    rm -f /home/mikana/.config/systemd/user/claude-chat.service.d/voiceid.conf
    systemctl --user daemon-reload; systemctl --user restart claude-chat.service
    sleep 8
    ok && state="rolled back to the previous server.py and running" || state="STILL DOWN after rollback"
    /home/mikana/bin/chat-telegram "Chat server did not come back with voice ID; $state." 2>/dev/null || true
    echo "$state"; exit 1
  fi
  sleep 5
done
echo "turn never ended within 30 min; no restart done"
