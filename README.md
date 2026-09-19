# claude-chat

A small local web chat UI for Claude that runs on a **Claude Pro plan** (no API key).
It shells out to the Claude Code CLI (`claude -p`) with your normal user config, so the
chat has everything the terminal has: all built-in tools, every configured MCP server
(OpenBrain included), CLAUDE.md, and session resume for multi-turn memory.

- Model: `claude-fable-5-1` at `low` effort (set in the systemd unit or env)
- Streams replies token by token; shows each tool call and its result inline
- Pro plan 5-hour / 7-day usage shown in the header
- Voice: mic input (Chrome speech recognition) and spoken replies (speechSynthesis),
  with hands-free back-and-forth when Voice is on
- Python stdlib only, no pip dependencies

## Run

```
python3 server.py            # http://127.0.0.1:8090
```

Environment overrides: `CHAT_HOST`, `CHAT_PORT`, `CHAT_MODEL`, `CHAT_EFFORT`, `CHAT_CWD`,
`CHAT_PERMISSION_MODE` (default `bypassPermissions`; headless mode has nobody to answer
permission prompts, so this gives the chat full capabilities. Use `acceptEdits` for a
safer default.)

## Install as a user service (starts at login)

```
cp claude-chat.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-chat
```

`claude-chat.desktop` is a GNOME launcher that opens the UI as a Chrome app window.

## Requirements

Claude Code CLI logged in (`claude` on PATH), Python 3, Chrome for the voice features.
The server binds to localhost only and runs with permission prompts bypassed. Don't expose
it beyond the machine without adding auth.
