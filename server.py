#!/usr/bin/env python3
"""Local web chat UI backed by the Claude Code CLI (Pro plan login).

Runs `claude -p` with the user's full config: all built-in tools, every MCP
server in the user config (OpenBrain included), CLAUDE.md auto-discovery.
Streams text deltas and tool activity to the browser as server-sent events.
"""
import json, os, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("CHAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHAT_PORT", "8090"))
MODEL = os.environ.get("CHAT_MODEL", "claude-fable-5-1")
EFFORT = os.environ.get("CHAT_EFFORT", "low")
WORKDIR = os.environ.get("CHAT_CWD", os.path.expanduser("~"))
PERMISSION_MODE = os.environ.get("CHAT_PERMISSION_MODE", "bypassPermissions")
APPEND_PROMPT = (
    "You are chatting with Mike through a web chat UI, not a terminal. "
    "Use OpenBrain (mcp__openbrain__recall) before answering questions about prior "
    "work, people, or projects, and mcp__openbrain__remember when Mike asks you to "
    "remember something. Keep replies conversational and short unless asked for depth."
)


def build_cmd(message, session_id):
    cmd = [
        "claude", "-p",
        "--model", MODEL,
        "--effort", EFFORT,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--tools", "default",
        "--permission-mode", PERMISSION_MODE,
        "--append-system-prompt", APPEND_PROMPT,
    ]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(message)
    return cmd


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(f.read(), "text/html; charset=utf-8")
        elif self.path == "/config":
            self._send(json.dumps({"model": MODEL, "effort": EFFORT, "cwd": WORKDIR,
                                   "permission_mode": PERMISSION_MODE}).encode(),
                       "application/json")
        else:
            self.send_error(404)

    def _sse(self, obj):
        self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
        self.wfile.flush()

    def do_POST(self):
        if self.path != "/chat":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", "0"))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400)
            return
        message = (req.get("message") or "").strip()
        session_id = req.get("session_id") or None
        if not message:
            self.send_error(400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # allow spawning from inside a Claude session
        t0 = time.time()
        proc = subprocess.Popen(build_cmd(message, session_id), cwd=WORKDIR, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        log(f"chat start pid={proc.pid} session={session_id} msg={message[:60]!r}")
        self._sse({"type": "started"})
        self.n_events = 0
        self.got_result = False
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    log(f"non-json stdout: {line[:200]}")
                    continue
                self.n_events += 1
                self._handle(ev)
            proc.wait()
            err = proc.stderr.read().strip()
            log(f"chat end pid={proc.pid} rc={proc.returncode} events={self.n_events} "
                f"result={self.got_result} {time.time()-t0:.1f}s"
                + (f" stderr={err[-300:]!r}" if err else ""))
            if proc.returncode != 0 or not self.got_result:
                self._sse({"type": "error",
                           "text": err[-800:] or f"claude exited {proc.returncode} without a result"})
        except (BrokenPipeError, ConnectionResetError):
            log(f"client disconnected pid={proc.pid}; killing")
            proc.kill()  # client hit Stop or navigated away
        finally:
            if proc.poll() is None:
                proc.kill()

    def _handle(self, ev):
        t = ev.get("type")
        if t == "stream_event":
            e = ev["event"]
            et = e.get("type")
            if et == "content_block_start":
                cb = e["content_block"]
                if cb["type"] == "tool_use":
                    self._sse({"type": "tool", "name": cb.get("name", "?")})
                elif cb["type"] == "thinking":
                    self._sse({"type": "thinking"})
            elif et == "content_block_delta":
                d = e["delta"]
                if d["type"] == "text_delta":
                    self._sse({"type": "text", "text": d["text"]})
        elif t == "assistant":
            for b in ev["message"].get("content", []):
                if b.get("type") == "tool_use":
                    self._sse({"type": "tool_input", "name": b.get("name"),
                               "input": json.dumps(b.get("input", {}))[:300]})
        elif t == "user":
            for b in (ev.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    self._sse({"type": "tool_result", "is_error": bool(b.get("is_error")),
                               "text": (str(c) if c is not None else "")[:300]})
        elif t == "rate_limit_event":
            w = ev.get("rate_limit_info", {}).get("unifiedWindows", {})
            self._sse({"type": "rl",
                       "five_hour": w.get("five_hour", {}).get("utilization"),
                       "seven_day": w.get("seven_day", {}).get("utilization")})
        elif t == "result":
            self.got_result = True
            self._sse({"type": "done", "session_id": ev.get("session_id"),
                       "is_error": ev.get("is_error", False),
                       "duration_ms": ev.get("duration_ms"),
                       "num_turns": ev.get("num_turns"),
                       "error": ev.get("result") if ev.get("is_error") else None})


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"claude-chat: http://{HOST}:{PORT}  model={MODEL} effort={EFFORT} "
          f"cwd={WORKDIR} perms={PERMISSION_MODE}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
