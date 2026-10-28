#!/usr/bin/env python3
"""crabby - bridge between the Noctalia panel plugin and the claude CLI.

  crabby serve          run the bridge (systemd socket activation or plain bind)
  crabby voice [cancel] toggle voice input / drop the recording unsent
  crabby send TEXT      send a message
  crabby open|stop|new  open the panel / end the session / start a new one

The bridge owns one `claude -p` process in stream-json mode (only while a
session is active), answers its permission prompts, and serves the plugin a
small HTTP API on 127.0.0.1: GET /events is an SSE stream of full view
snapshots, POST endpoints are commands.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PANEL_ID = "dvaxert/crabby:panel"
MODELS = ("haiku", "sonnet", "opus")
MAX_ITEMS = 80
MAX_TOOL_OUTPUT = 4000

HOME = os.path.expanduser("~")
CONFIG_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME", f"{HOME}/.config"), "crabby", "config.toml")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", f"{HOME}/.local/state"), "crabby")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", f"{HOME}/.local/share"), "crabby")
ICON_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "assets", "crabby.svg")

DEFAULTS = {
    "port": 18766,
    "cwd": "~",
    "claude": "claude",
    "extra_args": [],
    "default_model": "haiku",
    "idle_minutes": 15,  # kill claude after this much idle time; 0 disables
    "bridge_idle_minutes": 5,  # socket-activated bridge exits when unused this long
    "terminal": ["kitty", "--directory", "{cwd}"],
    "notify": True,
    "whisper_bin": "whisper-cli",
    "whisper_model": f"{DATA_DIR}/models/ggml-large-v3-turbo-q5_0.bin",
    "voice_language": "auto",  # whisper language code, or "auto"
    "max_record_sec": 60,  # recording stops on the hotkey or after this long
}

SYSTEM_PROMPT = """\
You are running inside Crabby, a chat panel in the user's Noctalia desktop shell, not in a terminal.
- The panel is narrow (~460px): answer briefly, prefer short lists, avoid wide tables.
- The user cannot interact with a TTY: never start interactive programs or TUIs, always use non-interactive flags.
- Typical requests are about the user's own Linux desktop ("what's up with my headphones", "find X").
- Environment: CachyOS (Arch Linux), niri compositor, Noctalia shell (`noctalia msg ...` IPC),
  PipeWire + WirePlumber (wpctl, pw-cli), bluetoothctl, systemd user services, fish shell, kitty terminal.
- Reply in the language the user writes in.
"""


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg.update(tomllib.load(f))
    except FileNotFoundError:
        pass
    cfg["cwd"] = os.path.expanduser(cfg["cwd"])
    cfg["whisper_model"] = os.path.expanduser(cfg["whisper_model"])
    return cfg


def now_ms():
    return int(time.time() * 1000)


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def tool_summary(name, inp):
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "path", "url", "query", "pattern", "description", "prompt"):
        val = inp.get(key)
        if isinstance(val, str) and val:
            return val.splitlines()[0][:200]
    return json.dumps(inp, ensure_ascii=False)[:200] if inp else ""


def sys_item(code, error=False, **args):
    """A system line in the chat; the plugin renders it via its translations."""
    return {"k": "system", "code": code, "args": args, "error": error}


# Notification texts; the plugin overrides them with its current language via POST /client.
DEFAULT_STRINGS = {
    "notify_done": "Claude replied",
    "notify_done_body": "Done",
    "notify_permission": "Claude needs permission: {tool}",
    "action_allow": "Allow",
    "action_open": "Open",
}


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(parts)
    return ""


class Notifier:
    """Desktop notifications sent as "Crabby", with optional action buttons."""

    def __init__(self, bridge):
        self.bridge = bridge

    def send(self, title, body, actions=None, urgency="normal"):
        if not self.bridge.cfg["notify"] or not shutil.which("notify-send"):
            return
        icon = os.path.realpath(ICON_PATH)
        argv = ["notify-send", "-a", "Crabby", "-u", urgency, "-i", icon if os.path.exists(icon) else "dialog-information"]
        for key, (label, _cb) in (actions or {}).items():
            argv += ["-A", f"{key}={label}"]
        argv += [f"🦀 {title}", body[:300]]

        def run():
            try:
                out = subprocess.run(argv, capture_output=True, text=True, timeout=600).stdout.strip()
            except Exception as exc:  # notification daemon gone, timeout, ...
                log("notify failed:", exc)
                return
            if actions and out in actions:
                actions[out][1]()

        threading.Thread(target=run, daemon=True).start()


class Loudness:
    """Tells whether anything louder than the background was said during a recording.

    Whisper invents text on silence, so a recording without speech is not sent.
    Levels are 100 ms RMS; thresholds are relative to the mic's own background,
    since quiet mics sit at 50-100 and soft speech there peaks at only 200-300.
    """

    SKIP = 3  # 300 ms: hotkey click and the mic opening

    def __init__(self):
        self.chunks = 0
        self.floor = None  # quietest chunk: the gaps between words are background
        self.peak = 0.0

    def feed(self, level):
        self.chunks += 1
        if self.chunks <= self.SKIP:
            return
        self.floor = level if self.floor is None else min(self.floor, level)
        self.peak = max(self.peak, level)

    @property
    def heard(self):
        return self.floor is not None and self.peak > max(120.0, self.floor * 2.5)

    def summary(self):
        floor = round(self.floor) if self.floor is not None else None
        return f"chunks={self.chunks} heard={self.heard} floor={floor} peak={round(self.peak)}"


class Voice:
    """Records from the mic until toggled again or max_record_sec, then transcribes with whisper.cpp."""

    RATE = 16000
    CHUNK = 1600  # 100 ms of s16 mono samples

    def __init__(self, bridge):
        self.bridge = bridge
        self.proc = None
        self.stop_flag = threading.Event()
        self.cancelled = False

    @property
    def state(self):
        return self.bridge.view["voice"]

    def toggle(self):
        if self.state == "recording":
            self.stop_flag.set()
        elif self.state == "idle":
            self.start()

    def cancel(self):
        if self.state == "recording":
            self.cancelled = True
            self.stop_flag.set()

    def start(self):
        cfg = self.bridge.cfg
        if not shutil.which(cfg["whisper_bin"]) or not os.path.exists(cfg["whisper_model"]):
            self.bridge.add_item(sys_item("voice_unavailable", True, bin=cfg["whisper_bin"], model=cfg["whisper_model"]))
            return
        self.stop_flag.clear()
        self.cancelled = False
        self.bridge.set_voice("recording")
        self.bridge.open_panel()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        cfg = self.bridge.cfg
        self.proc = subprocess.Popen(
            ["pw-record", "--raw", "--rate", str(self.RATE), "--channels", "1", "--format", "s16", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        pcm = bytearray()
        loudness = Loudness()
        max_chunks = max(1, int(cfg["max_record_sec"] * 10))
        reason = "toggle"
        try:
            while not self.stop_flag.is_set():
                chunk = self.proc.stdout.read(self.CHUNK * 2)
                if not chunk:
                    reason = "eof"
                    break
                pcm += chunk
                loudness.feed(rms(chunk))
                if loudness.chunks >= max_chunks:
                    reason = "max_length"
                    break
        finally:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None
        heard = loudness.heard
        log(f"voice: stop={reason} {loudness.summary()}")
        if self.cancelled:
            self.bridge.set_voice("idle")
            return
        if not heard:
            # Whisper hallucinates text on silence; nothing said means nothing sent.
            self.bridge.set_voice("idle")
            self.bridge.add_item(sys_item("nothing_heard"))
            return
        self.bridge.set_voice("transcribing")
        text = self.transcribe(bytes(pcm))
        self.bridge.set_voice("idle")
        if text:
            self.bridge.send(text, voice=True)

    def transcribe(self, pcm):
        cfg = self.bridge.cfg
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "voice.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(pcm)
        try:
            res = subprocess.run([cfg["whisper_bin"], "-m", cfg["whisper_model"], "-l", cfg["voice_language"],
                                  "-nt", "-np", "-f", path], capture_output=True, text=True, timeout=120)
        except Exception as exc:
            self.bridge.add_item(sys_item("voice_failed", True, error=str(exc)))
            return ""
        finally:
            os.unlink(path)
        if res.returncode != 0:
            self.bridge.add_item(sys_item("voice_failed", True, error=res.stderr.strip()[-300:]))
            return ""
        return " ".join(line.strip() for line in res.stdout.splitlines() if line.strip())


def rms(chunk):
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    samples = memoryview(chunk).cast("h")
    return (sum(s * s for s in samples) / n) ** 0.5


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.proc = None
        self.pending = {}  # request_id -> can_use_tool request
        self.stream_items = {}  # (api message id, block index) -> item
        self.tool_items = {}  # tool_use id -> item
        self.turn_tokens = 0
        self.msg_tokens = self.msg_chars = 0
        self.interrupted = False
        self.last_activity = time.time()
        self.last_request = time.time()
        self.sse_clients = 0
        self.panel_open = False
        self.strings = dict(DEFAULT_STRINGS)
        self.notifier = Notifier(self)
        self.voice = Voice(self)
        self.view = {
            "active": False,
            "status": "off",  # off | starting | idle | thinking | tool | permission | terminal
            "session_id": "",
            "model": cfg["default_model"],
            "allow_all": self.load_state().get("allow_all", False),
            "items": [],
            "permissions": [],
            "turn": None,  # {started, tokens, tool}
            "voice": "idle",  # idle | recording | transcribing
        }

    # --- persistent state ---------------------------------------------------

    def load_state(self):
        try:
            with open(os.path.join(STATE_DIR, "state.json")) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_state(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "state.json"), "w") as f:
            json.dump({"allow_all": self.view["allow_all"]}, f)

    # --- view ---------------------------------------------------------------

    def touch(self):
        """Mark the view changed; SSE handlers wake up and push a snapshot."""
        with self.lock:
            self.version += 1
            self.changed.notify_all()

    def snapshot(self):
        with self.lock:
            view = dict(self.view)
            view["permissions"] = [
                {"id": rid, "tool": r.get("display_name") or r.get("tool_name", "?"),
                 "summary": tool_summary(r.get("tool_name"), r.get("input")),
                 "description": r.get("description") or ""}
                for rid, r in self.pending.items()
            ]
            if view["turn"]:
                view["turn"] = dict(view["turn"], tokens=self.turn_tokens + self.msg_tokens)
            return json.dumps(view, ensure_ascii=False)

    def add_item(self, item):
        with self.lock:
            items = self.view["items"]
            items.append(item)
            del items[:-MAX_ITEMS]
        self.touch()
        return item

    def set_status(self, status):
        with self.lock:
            self.view["status"] = status
        self.touch()

    def set_voice(self, state):
        with self.lock:
            self.view["voice"] = state
        self.touch()

    # --- session ------------------------------------------------------------

    def start(self):
        with self.lock:
            if self.proc:
                return
            self.view.update(active=True, status="starting", session_id="", items=[], turn=None)
            self.pending.clear()
            self.stream_items.clear()
            self.tool_items.clear()
            argv = [self.cfg["claude"], "-p", "--verbose",
                    "--input-format", "stream-json", "--output-format", "stream-json",
                    "--include-partial-messages",
                    "--permission-prompts", "host", "--permission-prompt-tool", "stdio",
                    "--model", self.view["model"],
                    "--append-system-prompt", SYSTEM_PROMPT, *self.cfg["extra_args"]]
            self.proc = subprocess.Popen(argv, cwd=self.cfg["cwd"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
            proc = self.proc
            self.last_activity = time.time()
        threading.Thread(target=self.read_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self.read_stderr, args=(proc,), daemon=True).start()
        self.control({"subtype": "initialize"})
        self.touch()

    def stop(self, reason=None, status="off"):
        """End the session; `reason` is a sys_item shown in the chat."""
        with self.lock:
            proc, self.proc = self.proc, None
            self.pending.clear()
            self.view.update(active=False, status=status, turn=None, model=self.cfg["default_model"])
            if reason:
                self.view["items"].append(reason)
        if proc:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.touch()

    def write(self, obj):
        with self.lock:
            proc = self.proc
            if not proc:
                return False
            try:
                proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError):
                return False

    def control(self, request):
        self.write({"type": "control_request", "request_id": f"crabby-{uuid.uuid4().hex[:8]}", "request": request})

    def send(self, text, voice=False):
        text = text.strip()
        if not text:
            return
        if not self.proc:
            self.start()
        with self.lock:
            self.view["items"].append({"k": "user", "text": text, "voice": voice})
            self.view["status"] = "thinking"
            self.view["turn"] = {"started": now_ms(), "tool": ""}
            self.turn_tokens = self.msg_tokens = self.msg_chars = 0
            self.interrupted = False
            self.last_activity = time.time()
        self.write({"type": "user", "message": {"role": "user", "content": text},
                    "parent_tool_use_id": None, "session_id": self.view["session_id"]})
        self.touch()

    def interrupt(self):
        with self.lock:
            self.interrupted = True
            for rid in list(self.pending):
                self.answer(rid, False, "Interrupted by the user")
        self.control({"subtype": "interrupt"})

    def set_model(self, model):
        if model not in MODELS:
            return
        with self.lock:
            self.view["model"] = model
        if self.proc:
            self.control({"subtype": "set_model", "model": model})
        self.touch()

    def set_allow_all(self, value):
        with self.lock:
            self.view["allow_all"] = bool(value)
            self.save_state()
            if value:
                for rid in list(self.pending):
                    self.answer(rid, True)
        self.touch()

    def answer(self, request_id, allow, message="The user denied this"):
        with self.lock:
            req = self.pending.pop(request_id, None)
            if req is None:
                return
            if allow:
                response = {"behavior": "allow", "updatedInput": req.get("input", {})}
            else:
                response = {"behavior": "deny", "message": message}
            self.write({"type": "control_response",
                        "response": {"subtype": "success", "request_id": request_id, "response": response}})
            if not self.pending and self.view["status"] == "permission":
                self.view["status"] = "tool"
        self.touch()

    def to_terminal(self):
        with self.lock:
            sid, model = self.view["session_id"], self.view["model"]
        if not sid:
            return
        self.stop(sys_item("moved_to_terminal"), status="terminal")
        argv = [a.replace("{cwd}", self.cfg["cwd"]) for a in self.cfg["terminal"]]
        argv += [self.cfg["claude"], "--resume", sid, "--model", model]
        subprocess.Popen(argv, cwd=self.cfg["cwd"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def set_client(self, data):
        if "panel_open" in data:
            self.panel_open = bool(data["panel_open"])
        strings = data.get("strings")
        if isinstance(strings, dict):
            self.strings.update({k: str(v) for k, v in strings.items() if k in DEFAULT_STRINGS and v})

    def open_panel(self):
        if shutil.which("noctalia"):
            subprocess.Popen(["noctalia", "msg", "panel-open", PANEL_ID],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- claude output ------------------------------------------------------

    def read_stderr(self, proc):
        tail = []
        for line in proc.stderr:
            tail = (tail + [line.rstrip()])[-20:]
        proc.stderr_tail = "\n".join(tail)

    def read_stdout(self, proc):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                with self.lock:
                    if self.proc is not proc:
                        break
                    self.handle(msg)
                    self.last_activity = time.time()
            except Exception as exc:  # a protocol surprise must not kill the reader
                log("handle failed:", exc, line[:300])
            self.touch()
        code = proc.wait()
        with self.lock:
            if self.proc is proc:  # died on its own, not via stop()
                time.sleep(0.05)
                err = getattr(proc, "stderr_tail", "")
                self.proc = None
                self.stop(sys_item("claude_exited", True, code=code, error=err[-300:]))

    def handle(self, msg):
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init":
            self.view["session_id"] = msg.get("session_id", "")
            if self.view["status"] == "starting":
                self.view["status"] = "idle"
        elif kind == "stream_event":
            self.handle_stream(msg)
        elif kind == "assistant":
            self.handle_assistant(msg.get("message", {}))
        elif kind == "user":
            self.handle_tool_results(msg)
        elif kind == "result":
            self.handle_result(msg)
        elif kind == "control_request":
            self.handle_control(msg)
        elif kind == "control_cancel_request":
            self.pending.pop(msg.get("request_id"), None)

    def handle_stream(self, msg):
        ev = msg.get("event", {})
        et = ev.get("type")
        mid = msg.get("api_message_id") or ""
        if msg.get("parent_tool_use_id"):
            return  # subagent internals stay hidden
        if et == "message_start":
            self.msg_tokens = self.msg_chars = 0
        elif et == "content_block_start":
            block = ev.get("content_block", {})
            if block.get("type") == "text":
                item = {"k": "assistant", "text": block.get("text", ""), "streaming": True}
                self.stream_items[(mid, ev.get("index"))] = item
                self.view["items"].append(item)
                self.set_turn(status="thinking", tool="")
            elif block.get("type") == "tool_use":
                self.tool_item(block.get("id"), block.get("name"), {})
                self.set_turn(status="tool", tool=block.get("name", ""))
        elif et == "content_block_delta":
            delta = ev.get("delta", {})
            item = self.stream_items.get((mid, ev.get("index")))
            text = delta.get("text") or delta.get("thinking") or delta.get("partial_json") or ""
            if item is not None and delta.get("type") == "text_delta":
                item["text"] += text
            # Real usage only arrives with message_delta; estimate ~4 chars per token meanwhile.
            self.msg_chars += len(text)
            self.msg_tokens = max(self.msg_tokens, self.msg_chars // 4)
        elif et == "message_delta":
            self.msg_tokens = (ev.get("usage") or {}).get("output_tokens", self.msg_tokens)
        elif et == "message_stop":
            self.turn_tokens += self.msg_tokens
            self.msg_tokens = self.msg_chars = 0
        del self.view["items"][:-MAX_ITEMS]

    def set_turn(self, status, tool):
        if self.view["turn"] is None:
            self.view["turn"] = {"started": now_ms(), "tool": ""}
        self.view["turn"]["tool"] = tool
        if self.view["status"] != "permission":
            self.view["status"] = status

    def tool_item(self, tool_id, name, inp):
        item = self.tool_items.get(tool_id)
        if item is None:
            item = {"k": "tool", "id": tool_id, "name": name, "summary": "", "output": "", "state": "running"}
            self.tool_items[tool_id] = item
            self.view["items"].append(item)
        if inp:
            item["summary"] = tool_summary(name, inp)
            if self.view["turn"]:
                self.view["turn"]["tool"] = f"{name}: {item['summary']}"[:120]
        return item

    def handle_assistant(self, message):
        if not isinstance(message, dict):
            return
        mid = message.get("id", "")
        for block in message.get("content", []):
            bt = block.get("type")
            if bt == "text":
                item = next((it for (m, _), it in sorted(self.stream_items.items(), key=lambda kv: kv[0][1] or 0)
                             if m == mid and it.get("streaming")), None)
                if item is None:
                    item = {"k": "assistant", "text": ""}
                    self.view["items"].append(item)
                item["text"] = block.get("text", "")
                item.pop("streaming", None)
            elif bt == "tool_use":
                self.tool_item(block.get("id"), block.get("name"), block.get("input") or {})

    def handle_tool_results(self, msg):
        content = (msg.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                item = self.tool_items.get(block.get("tool_use_id"))
                if item:
                    item["output"] = result_text(block.get("content"))[-MAX_TOOL_OUTPUT:]
                    item["state"] = "error" if block.get("is_error") else "done"
        if self.view["status"] == "tool":
            self.set_turn(status="thinking", tool="")

    def handle_result(self, msg):
        for item in self.stream_items.values():
            item.pop("streaming", None)
        self.stream_items.clear()
        for item in self.tool_items.values():
            if item["state"] == "running":
                item["state"] = "done"
        self.view["status"] = "idle"
        self.view["turn"] = None
        if self.interrupted:
            self.view["items"].append(sys_item("interrupted"))
        elif msg.get("is_error") and msg.get("subtype") != "success":
            self.view["items"].append(sys_item("turn_error", True, error=str(msg.get("result") or msg.get("subtype"))))
        if not self.panel_open and not self.interrupted:
            last = next((it["text"] for it in reversed(self.view["items"]) if it["k"] == "assistant"), "")
            t = self.strings
            self.notifier.send(t["notify_done"], last.strip() or t["notify_done_body"],
                               {"open": (t["action_open"], self.open_panel)})

    def handle_control(self, msg):
        req = msg.get("request", {})
        rid = msg.get("request_id")
        if req.get("subtype") != "can_use_tool":
            # Unknown host requests must still be answered or claude waits forever.
            self.write({"type": "control_response",
                        "response": {"subtype": "error", "request_id": rid, "error": "unsupported"}})
            return
        self.pending[rid] = req
        if self.view["allow_all"]:
            self.answer(rid, True)
            return
        self.view["status"] = "permission"
        if not self.panel_open:
            name = req.get("display_name") or req.get("tool_name", "?")
            summary = req.get("description") or tool_summary(req.get("tool_name"), req.get("input"))
            t = self.strings
            self.notifier.send(t["notify_permission"].replace("{tool}", name), summary,
                               {"allow": (t["action_allow"], lambda: self.answer(rid, True)),
                                "open": (t["action_open"], self.open_panel)}, urgency="critical")

    # --- housekeeping -------------------------------------------------------

    def watchdog(self, server, activated):
        while True:
            time.sleep(15)
            idle_min = self.cfg["idle_minutes"]
            with self.lock:
                idle = self.proc and self.view["status"] in ("idle", "starting") and not self.pending
                if idle and idle_min and time.time() - self.last_activity > idle_min * 60:
                    self.stop(sys_item("idle_timeout", minutes=idle_min))
                unused = (not self.proc and self.sse_clients == 0 and self.view["voice"] == "idle"
                          and time.time() - self.last_request > self.cfg["bridge_idle_minutes"] * 60)
            if activated and unused:
                log("idle, exiting (systemd will restart on demand)")
                threading.Thread(target=server.shutdown, daemon=True).start()
                return


class Handler(BaseHTTPRequestHandler):
    bridge: Bridge = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def reply(self, code=200, body=b'{"ok":true}', ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        b = self.bridge
        b.last_request = time.time()
        if self.path == "/state":
            self.reply(body=b.snapshot().encode())
        elif self.path == "/events":
            self.events()
        else:
            self.reply(404, b'{"error":"not found"}')

    def events(self):
        b = self.bridge
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with b.lock:
            b.sse_clients += 1
        seen = -1
        try:
            while True:
                with b.lock:
                    if not b.changed.wait_for(lambda: b.version != seen, timeout=15):
                        payload = b": ping\n\n"
                    else:
                        seen = b.version
                        payload = f"data: {b.snapshot()}\n\n".encode()
                self.wfile.write(payload)
                self.wfile.flush()
                time.sleep(0.08)  # coalesce streaming deltas to ~12 fps
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with b.lock:
                b.sse_clients -= 1
                if b.sse_clients == 0:
                    b.panel_open = False

    def do_POST(self):
        b = self.bridge
        b.last_request = time.time()
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            return self.reply(400, b'{"error":"bad json"}')
        routes = {
            "/send": lambda: b.send(str(data.get("text", ""))),
            "/interrupt": b.interrupt,
            "/permission": lambda: b.answer(str(data.get("id")), bool(data.get("allow"))),
            "/model": lambda: b.set_model(str(data.get("model"))),
            "/allow_all": lambda: b.set_allow_all(bool(data.get("value"))),
            "/session/start": b.start,
            "/session/stop": lambda: b.stop(sys_item("session_ended")),
            "/session/new": lambda: (b.stop(), b.start()),
            "/terminal": b.to_terminal,
            "/voice/toggle": b.voice.toggle,
            "/voice/cancel": b.voice.cancel,
            "/open": b.open_panel,
            "/client": lambda: b.set_client(data),
        }
        action = routes.get(self.path)
        if action is None:
            return self.reply(404, b'{"error":"not found"}')
        # Commands may block on process startup/teardown; answer the HTTP client first.
        self.reply()
        threading.Thread(target=action, daemon=True).start()


def serve():
    cfg = load_config()
    bridge = Bridge(cfg)
    Handler.bridge = bridge
    activated = os.environ.get("LISTEN_PID") == str(os.getpid()) and int(os.environ.get("LISTEN_FDS", "0")) >= 1
    if activated:
        server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), Handler, bind_and_activate=False)
        server.socket = socket.socket(fileno=3)
    else:
        server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), Handler)
    server.daemon_threads = True
    threading.Thread(target=bridge.watchdog, args=(server, activated), daemon=True).start()

    def on_signal(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log(f"crabby bridge on 127.0.0.1:{cfg['port']}{' (socket-activated)' if activated else ''}")
    server.serve_forever()
    bridge.stop()


def post(path, data=None):
    cfg = load_config()
    req = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}{path}", method="POST",
                                 data=json.dumps(data or {}).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    simple = {"voice": "/voice/toggle", "open": "/open", "stop": "/session/stop", "new": "/session/new"}
    if cmd == "serve":
        serve()
    elif cmd == "voice" and args[1:] == ["cancel"]:
        post("/voice/cancel")
    elif cmd in simple:
        post(simple[cmd])
    elif cmd == "send" and len(args) > 1:
        post("/open")
        post("/send", {"text": " ".join(args[1:])})
    else:
        print(__doc__.strip())
        sys.exit(0 if cmd in ("help", "-h", "--help") else 2)


if __name__ == "__main__":
    main()
