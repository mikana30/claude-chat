#!/usr/bin/env python3
"""Local web chat UI backed by the Claude Code CLI (Pro plan login).

Runs one long-lived `claude -p --input-format stream-json` per chat session, with the user's full config: all built-in tools, every MCP
server in the user config (OpenBrain included), CLAUDE.md auto-discovery.
Streams text deltas and tool activity to the browser as server-sent events.
"""
import collections, glob, hmac, io, json, os, queue, re, subprocess, sys, threading, time, urllib.error, urllib.request, uuid, wave
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("CHAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHAT_PORT", "8090"))
MODEL = os.environ.get("CHAT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CHAT_EFFORT", "medium")
WORKDIR = os.environ.get("CHAT_CWD", os.path.expanduser("~"))
PERMISSION_MODE = os.environ.get("CHAT_PERMISSION_MODE", "bypassPermissions")
# Whose chat this is (Mike 11/4: "so long as we are still aware of whose chat is whose"). Each
# instance names its owner; the prompt, the Astrid framing and the spoken tag all use it.
CHAT_USER = os.environ.get("CHAT_USER", "Mike")
# Where this instance runs / is used from (Mike 11/4: Astrid must know she is not on the office
# tablet here). Each unit sets it; the browser's user agent adds phone vs computer per message.
CHAT_DEVICE = os.environ.get("CHAT_DEVICE") or f"a computer ({os.uname().nodename.split('.')[0]})"
APPEND_PROMPT = os.environ.get("CHAT_APPEND_PROMPT") or (
    f"You are chatting with {CHAT_USER} through a web chat UI, not a terminal. The person "
    f"typing or speaking here is {CHAT_USER}. "
    "Use OpenBrain (mcp__openbrain__recall) before answering questions about prior "
    f"work, people, or projects, and mcp__openbrain__remember when {CHAT_USER} asks you to "
    "remember something. Keep replies conversational and short unless asked for depth."
)
# Conversation mode (Mike 11/4): the chat is Astrid. Her system prompt is loaded from the same
# file her tablet brain uses, so the two never drift; a spoken message carries SPOKEN_TAG so her
# "How to speak" rules (emotion tag, no markdown, short) apply only when the reply is read aloud.
# CHAT_ASTRID_PROMPT=off disables this (a chat that is not Astrid, e.g. Shana's).
ASTRID_PROMPT_FILE = os.environ.get("CHAT_ASTRID_PROMPT", os.path.expanduser("~/astrid-brain/astrid.md"))
TAG_RE = re.compile(r"^\[(spoken|typed) by [^\]]*\]\s*")   # the per-message tag, hidden in history


def device_from_ua(ua):
    ua = ua or ""
    if "iPhone" in ua or "iPad" in ua:
        return "an iPhone or iPad"
    if "Android" in ua:
        return "a phone" if "Mobile" in ua else "an Android tablet"
    return "a computer"


def message_tag(spoken, device):
    how = "spoken" if spoken else "typed"
    tail = "; your reply is read aloud" if spoken else "; read on screen"
    return f"[{how} by {CHAT_USER} on {device} through the chat app{tail}]"


ASTRID_FRAMING = (
    "\n\n# Astrid in the chat app\n"
    f"This is {CHAT_USER}'s chat app, not the office tablet. It runs on {CHAT_DEVICE}. The person "
    f"typing or speaking here is {CHAT_USER}" + ("" if CHAT_USER == "Mike" else ", not Mike") + ". "
    "You are Astrid here too; her instructions follow, but the parts about the tablet do not apply "
    "in this chat: you are not on the tablet, you cannot hear the office or look through the office "
    "webcam from here, and you never say you live on the tablet. Your ears here are this device's "
    f"microphone while {CHAT_USER} is in a spoken conversation (the orb); your eyes are the photos or "
    f"videos {CHAT_USER} sends with the camera button (they arrive as image paths for you to Read). If "
    "asked to look, ask for a photo instead. Reminders, Do Not Disturb and session control still work "
    "(their scripts run on uriel) but a reminder is spoken on the office tablet, so say so when you set "
    "one. Where Astrid's instructions say Mike, read the general behaviour; the person here is "
    f"{CHAT_USER}.\n"
    "Every message starts with a tag saying how it arrived. "
    f"'{message_tag(False, 'a phone')}' (or 'a computer') was typed and is read on screen: answer as "
    "normal text chat, no emotion tag, markdown as useful. "
    f"'{message_tag(True, 'a phone')}' came in by voice and your whole reply is read aloud: follow "
    "the 'How to speak' rules exactly (emotion tag first, no markdown, lists, tables, code, paths or "
    "URLs, one to three sentences, offer detail instead of reading it, and say a short acknowledgement "
    "before any tool call).\n"
)
_hostname = os.uname().nodename.split(".")[0]


def astrid_prompt_mtime():
    if not ASTRID_PROMPT_FILE or ASTRID_PROMPT_FILE.lower() == "off":
        return None
    try:
        return os.stat(ASTRID_PROMPT_FILE).st_mtime
    except OSError:
        return None


def system_prompt():
    prompt = APPEND_PROMPT
    if astrid_prompt_mtime() is not None:
        with open(ASTRID_PROMPT_FILE) as f:
            prompt += ASTRID_FRAMING + "\n" + f.read()
        if _hostname != "uriel":
            prompt += ("\n\nNote: this chat runs on " + _hostname + ", not uriel. Astrid's scripts "
                       "(~/astrid-brain/remind.py, dnd.py, look.py, session.py) live on uriel; run them "
                       "over ssh as `ssh mikana@uriel python3 ~/astrid-brain/<script> ...`.\n")
    return prompt


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
                c = TAG_RE.sub("", c)
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


# ---- Claude processes: one long-lived `claude -p --input-format stream-json` per chat session,
# ported from Astrid's brain.py (uriel, Mike 11/3). Each turn is one user message on stdin and
# ends at its "result" event, so the 2-4 s CLI startup is paid once per session, not per message.
# A process is restarted with --resume (nothing forgotten) when it dies, hangs past TURN_TIMEOUT_S,
# or the page asks for a different model/effort (those are process flags). Idle processes are
# reaped after PROC_IDLE_S so they are not holding memory; the next turn resumes the session.
PROC_IDLE_S = int(os.environ.get("CHAT_PROC_IDLE_S", "1800"))
PROC_MAX = int(os.environ.get("CHAT_PROC_MAX", "2"))
TURN_TIMEOUT_S = int(os.environ.get("CHAT_TURN_TIMEOUT_S", "900"))
EOF = object()


def build_cmd(session_id, resume, model=None, effort=None):
    cmd = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model", model or MODEL,
        "--effort", effort or EFFORT,
        "--tools", "default",
        "--permission-mode", PERMISSION_MODE,
        "--append-system-prompt", system_prompt(),
    ]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    return cmd


class Session:
    """One chat session and the Claude process serving it (at most one turn at a time)."""

    def __init__(self, sid, resume):
        self.sid = sid
        self.resume = resume          # False only until the first turn has created the transcript
        self.p = None
        self.events = None
        self.stderr = collections.deque(maxlen=40)
        self.model = self.effort = None
        self.prompt_mtime = None      # astrid.md as loaded; a change restarts the process (Astrid's rule)
        self.lock = threading.Lock()  # held for the whole turn
        self.last = time.time()

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        p, self.p = self.p, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(timeout=5)
            except Exception:
                p.kill()

    def start(self, model, effort):
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # allow spawning from inside a Claude session
        p = subprocess.Popen(build_cmd(self.sid, self.resume, model, effort), cwd=WORKDIR, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)
        events = queue.Queue()

        def read():
            for line in p.stdout:
                try:
                    events.put(json.loads(line))
                except ValueError:
                    log(f"non-json stdout: {line[:200]}")
            events.put(EOF)

        def read_err():
            for line in p.stderr:
                self.stderr.append(line.rstrip())

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=read_err, daemon=True).start()
        self.p, self.events, self.model, self.effort = p, events, model, effort
        self.prompt_mtime = astrid_prompt_mtime()
        log(f"claude start pid={p.pid} session={self.sid[:8]} resume={self.resume} model={model} effort={effort}")

    def ensure(self, model, effort):
        """A running process with these flags. True if one was (re)started."""
        if self.alive() and (self.model, self.effort, self.prompt_mtime) == (model, effort, astrid_prompt_mtime()):
            return False
        self.stop()
        self.start(model, effort)
        return True

    def send(self, text):
        self._write({"type": "user", "message": {"role": "user", "content": text}})

    def interrupt(self):
        """Stop the current turn (the CLI answers with a result event); the process lives on."""
        self._write({"type": "control_request", "request_id": f"i{int(time.time()*1000)}",
                     "request": {"subtype": "interrupt"}})

    def _write(self, obj):
        try:
            self.p.stdin.write(json.dumps(obj) + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # dead process: shows up as EOF on the queue

    def drain(self):
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                return


_sessions = {}                 # sid -> Session
_sessions_lock = threading.Lock()


def get_session(sid):
    with _sessions_lock:
        s = _sessions.get(sid) if sid else None
        if s is None:
            s = Session(sid, resume=True) if sid else Session(str(uuid.uuid4()), resume=False)
            _sessions[s.sid] = s
        s.last = time.time()
        idle = [o for o in _sessions.values() if o is not s and o.alive() and not o.lock.locked()]
        idle.sort(key=lambda o: o.last)
        while len(idle) + 1 > PROC_MAX and idle:      # LRU: keep memory flat on the Go
            o = idle.pop(0)
            log(f"claude stop (over {PROC_MAX} live) session={o.sid[:8]}")
            o.stop()
        return s


def reaper():
    while True:
        time.sleep(60)
        now = time.time()
        with _sessions_lock:
            for s in list(_sessions.values()):
                if s.alive() and now - s.last > PROC_IDLE_S and s.lock.acquire(blocking=False):
                    try:
                        log(f"claude stop (idle {int(now - s.last)}s) session={s.sid[:8]}")
                        s.stop()
                    finally:
                        s.lock.release()
                if not s.alive() and now - s.last > 24 * 3600:
                    del _sessions[s.sid]


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
        astrid = astrid_prompt_mtime() is not None
        spoken = bool(req.get("voice")) and astrid
        tag = message_tag(spoken, device_from_ua(self.headers.get("User-Agent"))) + " " if astrid else ""
        if not message or (effort and effort not in EFFORTS) \
                or (model and not model.replace("-", "").replace(".", "").isalnum()):
            self.send_error(400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        sess = get_session(session_id)
        model, effort = model or MODEL, effort or EFFORT
        if not sess.lock.acquire(timeout=5):   # a stopped turn ends within a second
            self._sse({"type": "error", "text": "That chat is still answering the previous message."})
            return
        t0 = time.time()
        self.n_events = 0
        self.got_result = False
        self.message = message
        try:
            started = sess.ensure(model, effort)
            sess.drain()
            log(f"chat start pid={sess.p.pid} session={sess.sid[:8]} new_proc={started} model={model} "
                f"effort={effort} tag={tag.strip()[:40]!r} msg={message[:60]!r}")
            self._sse({"type": "started", "process_started": started, "session_id": sess.sid})
            sess.send(tag + message)
            deadline = t0 + TURN_TIMEOUT_S
            interrupted = False
            while True:
                try:
                    ev = sess.events.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    log(f"chat timeout session={sess.sid[:8]} after {TURN_TIMEOUT_S}s; stopping process")
                    sess.stop()                 # next turn restarts it with --resume
                    self._sse({"type": "error", "text": f"claude timed out after {TURN_TIMEOUT_S} s"})
                    break
                if ev is EOF:
                    err = "\n".join(sess.stderr).strip()
                    rc = sess.p.poll() if sess.p else None
                    log(f"claude exited pid={sess.p and sess.p.pid} rc={rc} session={sess.sid[:8]} stderr={err[-300:]!r}")
                    sess.stop()
                    if not self.got_result:
                        self._sse({"type": "error", "text": err[-800:] or f"claude exited {rc} without a result"})
                    break
                self.n_events += 1
                self._handle(ev)
                if ev.get("type") == "result":
                    break
                if self.client_gone and not interrupted:   # Stop button: end the turn, keep the process
                    sess.interrupt()
                    interrupted = True
            sess.resume = True
            sess.last = time.time()
            log(f"chat end session={sess.sid[:8]} events={self.n_events} result={self.got_result} "
                f"{time.time()-t0:.1f}s" + (" (client gone)" if self.client_gone else ""))
        finally:
            sess.lock.release()

    client_gone = False

    def _sse(self, obj):
        # The client hitting Stop or navigating away never kills the Claude process (Astrid's
        # rule): the turn is interrupted and the session stays ready for the next message.
        if self.client_gone:
            return
        try:
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.client_gone = True
            log("client disconnected; interrupting the turn, process kept")

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
            elif et == "content_block_stop":
                self._sse({"type": "block_end"})   # page flushes speech here, as the tablet does
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
    threading.Thread(target=reaper, daemon=True).start()
    print(f"claude-chat: http://{HOST}:{PORT}  model={MODEL} effort={EFFORT} "
          f"cwd={WORKDIR} perms={PERMISSION_MODE} key={'on' if TOKEN else 'off'} voices={list_voices()}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
