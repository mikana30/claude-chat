#!/usr/bin/env python3
"""Local web chat UI backed by the Claude Code CLI (Pro plan login).

Runs `claude -p` with the user's full config: all built-in tools, every MCP
server in the user config (OpenBrain included), CLAUDE.md auto-discovery.
Streams text deltas and tool activity to the browser as server-sent events.
"""
import glob, hmac, io, json, os, re, subprocess, sys, threading, time, urllib.error, urllib.request, wave
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("CHAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHAT_PORT", "8090"))
MODEL = os.environ.get("CHAT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CHAT_EFFORT", "low")
WORKDIR = os.environ.get("CHAT_CWD", os.path.expanduser("~"))
PERMISSION_MODE = os.environ.get("CHAT_PERMISSION_MODE", "bypassPermissions")
APPEND_PROMPT = os.environ.get("CHAT_APPEND_PROMPT") or (
    "You are chatting with Mike through a web chat UI, not a terminal. "
    "Use OpenBrain (mcp__openbrain__recall) before answering questions about prior "
    "work, people, or projects, and mcp__openbrain__remember when Mike asks you to "
    "remember something. Keep replies conversational and short unless asked for depth."
)
# Optional access key. When set (e.g. a copy exposed through Tailscale Funnel),
# every request needs the key: the first visit carries it as ?t=KEY, which is
# stored as a long-lived cookie, then the browser is redirected to a clean URL.
TOKEN = os.environ.get("CHAT_TOKEN", "")
COOKIE = "cc_key"
APP_NAME = os.environ.get("CHAT_APP_NAME", "Claude Chat")
APP_ICON = os.environ.get("CHAT_APP_ICON", "")   # base name in icon/, e.g. "shana" -> shana-any-512.png

# ---- Text-to-speech: Piper (neural, local). Voices are *.onnx files in VOICE_DIR. ----
VOICE_DIR = os.environ.get("CHAT_VOICE_DIR", os.path.expanduser("~/.local/share/piper-voices"))
DEFAULT_VOICE = os.environ.get("CHAT_VOICE", "en_US-lessac-medium")
_voices = {}                      # name -> PiperVoice; only the most recent one is kept
_voice_lock = threading.Lock()    # guards model load/unload
_synth_lock = threading.Lock()    # piper is CPU-bound; one synthesis at a time


# "astrid" is not a Piper model: /tts proxies it to Astrid's speech service on kali
# (same call as ~/astrid-brain/brain.py tts()). The bearer token stays server-side.
ASTRID = "astrid"
ASTRID_URL = os.environ.get("ASTRID_SPEECH_URL", "http://100.87.231.86:18911")
ASTRID_TOKEN_FILE = os.environ.get("ASTRID_SPEECH_TOKEN_FILE", os.path.expanduser("~/astrid-brain/speech.token"))


def astrid_token():
    try:
        with open(ASTRID_TOKEN_FILE) as f:
            return f.read().strip()   # strip() also drops a CRLF line ending
    except OSError:
        return ""


def astrid_wav(text):
    req = urllib.request.Request(f"{ASTRID_URL}/tts", data=json.dumps({"text": text}).encode(),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "Authorization": f"Bearer {astrid_token()}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:   # message carries only the status, never the header
        raise RuntimeError(f"astrid speech HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"astrid speech unreachable: {getattr(e, 'reason', e)}") from None
    log(f"tts {ASTRID} {len(text)} chars {time.time()-t0:.2f}s {len(body)} bytes")
    return body


def list_voices():
    piper = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(VOICE_DIR, "*.onnx")))
    return ([ASTRID] if astrid_token() else []) + piper


def get_voice(name):
    if name not in list_voices():
        raise ValueError(f"unknown voice {name!r}")
    with _voice_lock:
        if name not in _voices:
            from piper import PiperVoice
            t0 = time.time()
            _voices.clear()  # ~150 MB per model; keep memory flat on an 8 GB box
            _voices[name] = PiperVoice.load(os.path.join(VOICE_DIR, name + ".onnx"))
            log(f"tts loaded {name} in {time.time()-t0:.1f}s")
        return _voices[name]


def tts_wav(text, name):
    if name == ASTRID:
        return astrid_wav(text)
    voice = get_voice(name)
    buf = io.BytesIO()
    with _synth_lock:
        t0 = time.time()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w)
        log(f"tts {name} {len(text)} chars {time.time()-t0:.2f}s")
    return buf.getvalue()


def preload_voice():
    try:
        get_voice(DEFAULT_VOICE)
    except Exception as e:  # missing model or piper: TTS endpoint reports it per request
        log(f"tts preload skipped: {e}")


EFFORTS = ("low", "medium", "high", "xhigh", "max")

# ---- Past chats: this server keeps its own index of the sessions it ran (other `claude -p`
# jobs share the same project dir, so the CLI's transcript folder alone can't tell them apart).
# Messages are read back from the CLI's transcript, ~/.claude/projects/<cwd slug>/<id>.jsonl.
INDEX_PATH = os.path.expanduser(f"~/.local/state/claude-chat/sessions-{PORT}.json")
# Photos/videos from the camera button (Mike 11/4). Photos are shrunk; videos become ~1 frame/s (no sound).
UPLOAD_DIR = os.path.expanduser(f"~/.local/state/claude-chat/uploads-{PORT}")
UPLOAD_MAX = 300 * 1024 * 1024
VIDEO_MAX_FRAMES = 40


def save_upload(data, ctype):
    """Store an upload and return {"kind", "paths"}: image files Claude can Read."""
    import secrets
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    stem = os.path.join(UPLOAD_DIR, time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3))
    raw = stem + ".src"
    with open(raw, "wb") as f:
        f.write(data)
    try:
        if ctype.startswith("video/"):
            os.makedirs(stem, exist_ok=True)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-vf",
                            "fps=1,scale='min(1280,iw)':-2", "-frames:v", str(VIDEO_MAX_FRAMES),
                            "-q:v", "4", os.path.join(stem, "frame-%03d.jpg")], check=True, timeout=180)
            paths = sorted(glob.glob(os.path.join(stem, "frame-*.jpg")))
            if not paths:
                raise ValueError("no frames in video")
            return {"kind": "video", "paths": paths}
        out = stem + ".jpg"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-vf",
                        "scale='min(1600,iw)':-2", "-frames:v", "1", "-q:v", "3", out], check=True, timeout=60)
        return {"kind": "photo", "paths": [out]}
    finally:
        os.remove(raw)
TRANSCRIPT_DIR = os.path.expanduser("~/.claude/projects/" + re.sub(r"[^A-Za-z0-9]", "-", WORKDIR))
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_index_lock = threading.Lock()


def load_index():
    try:
        with open(INDEX_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def record_session(session_id, message):
    with _index_lock:
        idx = load_index()
        e = idx.setdefault(session_id, {"title": message[:80], "created": time.time()})
        e["updated"] = time.time()
        e["last"] = message[:80]
        os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(idx, f)
        os.replace(tmp, INDEX_PATH)


def list_sessions():
    idx = load_index()
    out = [dict(id=k, **v) for k, v in idx.items()
           if os.path.exists(os.path.join(TRANSCRIPT_DIR, k + ".jsonl"))]
    return sorted(out, key=lambda e: e.get("updated", 0), reverse=True)


def session_history(session_id):
    """User prompts and Claude's final reply text per turn, oldest first."""
    msgs, reply = [], []
    with open(os.path.join(TRANSCRIPT_DIR, session_id + ".jsonl")) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("isSidechain"):
                continue
            c = (e.get("message") or {}).get("content")
            if e.get("type") == "user" and isinstance(c, str):
                reply = []  # a hook-forced rewrite replaces the earlier draft of this turn
                if e.get("isMeta"):
                    continue
                msgs.append({"role": "user", "text": c})
                msgs.append({"role": "assistant", "text": ""})
            elif e.get("type") == "assistant" and isinstance(c, list) and msgs:
                reply += [b["text"] for b in c if b.get("type") == "text" and b.get("text")]
                msgs[-1]["text"] = "\n\n".join(reply)
    return [m for m in msgs if m["text"]]

# Model list comes from Claude Code's own catalog cache, which the CLI refreshes as
# Anthropic publishes models; read per request so new models show up without a restart.
CATALOG_GLOB = os.path.expanduser("~/.claude/cache/model-catalog/*-cc.json")


def catalog_models():
    try:
        path = max(glob.glob(CATALOG_GLOB), key=os.path.getmtime)
        with open(path) as f:
            models = json.load(f)["catalog"]["config"]["models"]
        return [{"id": m["id"], "name": m.get("name") or m["id"], "section": m.get("section", "main")}
                for m in models if m.get("id")]
    except (ValueError, OSError, KeyError, TypeError) as e:  # no cache yet or format change: UI keeps its static list
        log(f"model catalog unavailable: {e}")
        return []


def build_cmd(message, session_id, model=None, effort=None):
    cmd = [
        "claude", "-p",
        "--model", model or MODEL,
        "--effort", effort or EFFORT,
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

    def _authed(self):
        if not TOKEN:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, TOKEN):
                return True
        return False

    def _deny(self):
        body = b"Not authorized."
        self.send_response(401)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if TOKEN and url.path in ("/", "/index.html"):
            t = (parse_qs(url.query).get("t") or [""])[0]
            if t and hmac.compare_digest(t, TOKEN):
                self.send_response(302)
                self.send_header("Set-Cookie", f"{COOKIE}={TOKEN}; Max-Age=315360000; Path=/; "
                                 "HttpOnly; Secure; SameSite=Lax")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if url.path == "/manifest.json" and APP_ICON:
            man = {"name": APP_NAME, "short_name": APP_NAME, "start_url": "/", "scope": "/",
                   "display": "standalone", "background_color": "#101420", "theme_color": "#101420",
                   "icons": [{"src": f"/icon/{APP_ICON}-any-512.png", "sizes": "512x512",
                              "type": "image/png", "purpose": "any"},
                             {"src": f"/icon/{APP_ICON}-maskable-512.png", "sizes": "512x512",
                              "type": "image/png", "purpose": "maskable"}]}
            return self._send(json.dumps(man).encode(), "application/manifest+json")
        if APP_ICON and url.path in (f"/icon/{APP_ICON}-any-512.png", f"/icon/{APP_ICON}-maskable-512.png"):
            with open(os.path.join(HERE, url.path.lstrip("/")), "rb") as f:
                return self._send(f.read(), "image/png")
        if not self._authed():
            return self._deny()
        self.path = url.path
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                html = f.read()
            if APP_ICON:
                html = html.replace(b"<title>Claude Chat</title>",
                                    f'<title>{APP_NAME}</title><link rel="manifest" href="/manifest.json">'
                                    f'<meta name="theme-color" content="#101420">'.encode(), 1)
            self._send(html, "text/html; charset=utf-8")
        elif self.path == "/config":
            self._send(json.dumps({"model": MODEL, "effort": EFFORT, "cwd": WORKDIR,
                                   "permission_mode": PERMISSION_MODE,
                                   "models": catalog_models()}).encode(),
                       "application/json")
        elif self.path == "/sessions":
            self._send(json.dumps({"sessions": list_sessions()}).encode(), "application/json")
        elif self.path == "/history":
            sid = (parse_qs(url.query).get("id") or [""])[0]
            if not UUID_RE.match(sid):
                return self.send_error(400)
            try:
                self._send(json.dumps({"messages": session_history(sid)}).encode(), "application/json")
            except OSError:
                self.send_error(404)
        elif self.path == "/voices":
            voices = list_voices()
            self._send(json.dumps({"voices": voices,
                                   "default": ASTRID if ASTRID in voices else DEFAULT_VOICE}).encode(),
                       "application/json")
        else:
            self.send_error(404)

    def _sse(self, obj):
        self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
        self.wfile.flush()

    def do_POST(self):
        if not self._authed():
            return self._deny()
        if self.path not in ("/chat", "/tts", "/upload"):
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", "0"))
        if self.path == "/upload":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not (ctype.startswith("image/") or ctype.startswith("video/")) or not 0 < n <= UPLOAD_MAX:
                self.send_error(400)
                return
            try:
                res = save_upload(self.rfile.read(n), ctype)
            except Exception as e:
                log(f"upload error: {e}")
                self.send_error(422, "Could not read that file")
                return
            log(f"upload {res['kind']} {len(res['paths'])} image(s)")
            return self._send(json.dumps(res).encode(), "application/json")
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400)
            return
        if self.path == "/tts":
            text = (req.get("text") or "").strip()[:2000]
            if not text:
                self.send_error(400)
                return
            try:
                self._send(tts_wav(text, req.get("voice") or DEFAULT_VOICE), "audio/wav")
            except Exception as e:
                log(f"tts error: {e}")
                self.send_error(500, str(e)[:200])
            return
        message = (req.get("message") or "").strip()
        session_id = req.get("session_id") or None
        model = (req.get("model") or "").strip() or None
        effort = (req.get("effort") or "").strip() or None
        if not message or (effort and effort not in EFFORTS) \
                or (model and not model.replace("-", "").replace(".", "").isalnum()):
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
        proc = subprocess.Popen(build_cmd(message, session_id, model, effort), cwd=WORKDIR, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        log(f"chat start pid={proc.pid} session={session_id} model={model or MODEL} "
            f"effort={effort or EFFORT} msg={message[:60]!r}")
        self._sse({"type": "started"})
        self.n_events = 0
        self.got_result = False
        self.message = message
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
            sid = ev.get("session_id")
            if sid and UUID_RE.match(sid):
                try:
                    record_session(sid, self.message)
                except OSError as e:
                    log(f"session index write failed: {e}")
            self._sse({"type": "done", "session_id": ev.get("session_id"),
                       "is_error": ev.get("is_error", False),
                       "duration_ms": ev.get("duration_ms"),
                       "num_turns": ev.get("num_turns"),
                       "error": ev.get("result") if ev.get("is_error") else None})


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=preload_voice, daemon=True).start()
    print(f"claude-chat: http://{HOST}:{PORT}  model={MODEL} effort={EFFORT} "
          f"cwd={WORKDIR} perms={PERMISSION_MODE} key={'on' if TOKEN else 'off'} voices={list_voices()}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
