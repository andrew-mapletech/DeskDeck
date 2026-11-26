#!/usr/bin/env python3
"""DeskDeck: a dashboard + shortcut deck for an iPad, served from your Mac.

Run:  python3 server.py
Then open the printed URL on the iPad (same Wi-Fi) and Add to Home Screen.
Only Python's standard library is used.
"""
import glob
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("DESKDECK_PORT", "8770"))
CLAUDE_DIR = os.path.expanduser("~/.claude")
TOKEN_FILE = os.path.join(HERE, ".token")
SHORTCUTS_FILE = os.path.join(HERE, "shortcuts.json")


# --------------------------------------------------------------- access key
def load_token():
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    tok = secrets.token_urlsafe(12)
    with open(TOKEN_FILE, "w") as f:
        f.write(tok)
    os.chmod(TOKEN_FILE, 0o600)
    return tok


TOKEN = load_token()


def project_name(cwd):
    """Folder name for display; sessions started without a folder show as 'No folder'."""
    if not cwd:
        return "?"
    if "scratch-workspaces" in cwd:
        return "No folder"
    return os.path.basename(cwd.rstrip("/")) or cwd


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def osa(script):
    return run(["osascript", "-e", script]).strip()


# --------------------------------------------------------------- system stats
try:  # read CPU ticks via mach host_statistics(HOST_CPU_LOAD_INFO)
    import ctypes
    _libsys = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    _libsys.mach_host_self.restype = ctypes.c_uint
    _HOST = _libsys.mach_host_self()
except Exception:
    _libsys = None


def cpu_ticks():
    if not _libsys:
        return None
    info = (ctypes.c_uint * 4)()
    count = ctypes.c_uint(4)
    if _libsys.host_statistics(_HOST, 3, ctypes.byref(info), ctypes.byref(count)) != 0:
        return None
    return list(info)


class Stats:
    def __init__(self):
        self.data = {"cpu": 0, "ram": {}, "disk": {}, "net": {"down": 0, "up": 0}, "load": [0, 0, 0]}
        self.cpu_hist = deque(maxlen=60)
        self.net_hist = deque(maxlen=60)
        self._last_net = None
        self._last_ticks = None
        self.last_viewed = 0.0
        self._wake = threading.Event()
        self.memsize = int(run(["sysctl", "-n", "hw.memsize"]) or 0)
        self.model = run(["sysctl", "-n", "hw.model"]).strip()
        self.chip = run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        threading.Thread(target=self._loop, daemon=True).start()

    def _cpu(self):
        """CPU % since the previous sample, read straight from the kernel (no subprocess)."""
        ticks = cpu_ticks()
        if not ticks:
            return self.data["cpu"]
        last, self._last_ticks = self._last_ticks, ticks
        if not last:
            return self.data["cpu"]
        d = [a - b for a, b in zip(ticks, last)]  # user, system, idle, nice
        total = sum(d)
        return round(100 * (total - d[2]) / total, 1) if total > 0 else self.data["cpu"]

    def _ram(self):
        out = run(["vm_stat"])
        page = 16384
        vals = {}
        for line in out.splitlines():
            if "page size of" in line:
                try:
                    page = int(line.split("page size of")[1].split()[0])
                except Exception:
                    pass
            elif ":" in line:
                k, v = line.split(":", 1)
                try:
                    vals[k.strip()] = int(v.strip().rstrip("."))
                except ValueError:
                    pass
        used = (vals.get("Pages active", 0) + vals.get("Pages wired down", 0)
                + vals.get("Pages occupied by compressor", 0)) * page
        total = self.memsize or 1
        return {"used": used, "total": total, "pct": round(used * 100 / total, 1)}

    def _disk(self):
        path = "/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/"
        u = shutil.disk_usage(path)
        return {"used": u.used, "total": u.total, "pct": round(u.used * 100 / u.total, 1)}

    def _net_bytes(self):
        rx = tx = 0
        for line in run(["netstat", "-ibn"]).splitlines():
            p = line.split()
            if len(p) < 10 or not p[0].startswith("en") or not p[2].startswith("<Link"):
                continue
            try:
                rx += int(p[-5])
                tx += int(p[-2])
            except ValueError:
                pass
        return rx, tx, time.time()

    def touch(self):
        """Called whenever the iPad asks for data; wakes the sampler."""
        self.last_viewed = time.time()
        self._wake.set()

    def _loop(self):
        while True:
            # Only sample while someone has the dashboard open (asked in the last 20 s).
            if time.time() - self.last_viewed > 20:
                self._last_net = self._last_ticks = None  # don't average across the idle gap
                self._wake.clear()
                self._wake.wait()
                self._cpu()  # take a first reading so the next one has something to compare to
                time.sleep(1)
            try:
                cpu = self._cpu()
                rx, tx, t = self._net_bytes()
                net = self.data["net"]
                if self._last_net:
                    lrx, ltx, lt = self._last_net
                    dt = max(t - lt, 0.1)
                    net = {"down": max(rx - lrx, 0) / dt, "up": max(tx - ltx, 0) / dt}
                self._last_net = (rx, tx, t)
                self.cpu_hist.append(cpu)
                self.net_hist.append(net["down"])
                self.data = {
                    "cpu": cpu,
                    "ram": self._ram(),
                    "disk": self._disk(),
                    "net": net,
                    "load": [round(x, 2) for x in os.getloadavg()],
                    "cpu_hist": list(self.cpu_hist),
                    "net_hist": list(self.net_hist),
                    "chip": self.chip,
                    "model": self.model,
                }
            except Exception as e:  # keep sampling no matter what
                print("stats error:", e, flush=True)
            time.sleep(2)


# --------------------------------------------------------------- now playing
def app_running(name):
    return run(["pgrep", "-x", name]).strip() != ""


# Browsers we can read via AppleScript. Chromium-based ones use
# `execute <tab> javascript`, Safari uses `do JavaScript ... in <tab>`.
# Both need "Allow JavaScript from Apple Events" turned on (Chromium:
# View > Developer; Safari: Develop menu) to read play state / control playback.
# Without it we still find the tab and show its title.
CHROMIUM_BROWSERS = ("Brave Browser", "Google Chrome", "Microsoft Edge", "Arc", "Vivaldi", "Opera")
BROWSERS = ("Safari",) + CHROMIUM_BROWSERS

# Only tabs on these sites are inspected (keeps it cheap when lots of tabs are open).
MEDIA_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com", "open.spotify.com", "music.apple.com",
               "twitch.tv", "vimeo.com", "bandcamp.com", "deezer.com", "tidal.com", "pandora.com")

_US, _RS = "\x1f", "\x1e"  # field / record separators in osascript output

_DETECT_JS = r"""(function(){try{
var v=Array.prototype.slice.call(document.querySelectorAll('video,audio'));
var p=v.find(function(m){return !m.paused&&!m.ended&&m.readyState>2;});
var m=p||v.find(function(m){return m.currentTime>0;});
if(!m)return 'none';
var md=navigator.mediaSession&&navigator.mediaSession.metadata;
var S=String.fromCharCode(31);
return (p?'playing':'paused')+S+((md&&md.title)||document.title)+S+((md&&md.artist)||'');
}catch(e){return 'error';}})()"""

_FIND_MEDIA = r"""
var v=Array.prototype.slice.call(document.querySelectorAll('video,audio'));
var m=v.find(function(x){return !x.paused&&!x.ended;})||v.find(function(x){return x.currentTime>0;})||v[0];
function btn(s){var b=document.querySelector(s);if(b){b.click();return true;}return false;}
"""

_CONTROL_JS = {
    "playpause": "(function(){" + _FIND_MEDIA + """
        if(!m)return 'none'; if(m.paused){m.play();}else{m.pause();} return 'ok';})()""",
    "next": "(function(){" + _FIND_MEDIA + """
        if(btn('.ytp-next-button, ytmusic-player-bar .next-button, [data-testid="control-button-skip-forward"], [aria-label="Next"], [aria-label="Next track"]'))return 'ok';
        if(m&&m.duration){m.currentTime=m.duration;return 'ok';} return 'none';})()""",
    "prev": "(function(){" + _FIND_MEDIA + """
        if(m&&m.currentTime>3){m.currentTime=0;return 'ok';}
        if(btn('.ytp-prev-button, ytmusic-player-bar .previous-button, [data-testid="control-button-skip-back"], [aria-label="Previous"], [aria-label="Previous track"]'))return 'ok';
        if(m){m.currentTime=0;return 'ok';} return 'none';})()""",
}


def _as_str(s):
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _js_cmd(app, tab, js):
    if app == "Safari":
        return f"do JavaScript {_as_str(js)} in {tab}"
    return f"execute {tab} javascript {_as_str(js)}"


def _clean_title(title):
    title = re.sub(r"^\(\d+\)\s*", "", title or "")  # "(10) " unread-notification prefix
    return re.sub(r"\s*[-–]\s*(YouTube Music|YouTube|SoundCloud|Spotify|Twitch)$", "", title).strip()


def _looks_like_playback(url):
    """For tabs we can't run JS in: only count pages that actually play something."""
    if "youtube.com" in url and "music.youtube.com" not in url:
        return any(p in url for p in ("/watch", "/shorts/", "/live/", "/embed/"))
    return True


def _browser_tabs(app):
    """Inspect every media-site tab in `app`. Returns a list of candidate dicts."""
    title_prop = "name" if app == "Safari" else "title"
    host_test = " or ".join(f"u contains {_as_str(h)}" for h in MEDIA_HOSTS)
    script = f'''
    set US to character id 31
    set RS to character id 30
    set out to ""
    tell application "{app}"
        repeat with w in windows
            set wid to id of w
            set ti to 0
            repeat with t in tabs of w
                set ti to ti + 1
                set u to ""
                try
                    set u to (URL of t) as text
                end try
                if {host_test} then
                    try
                        set r to ({_js_cmd(app, "t", _DETECT_JS)}) as text
                    on error
                        set r to "nojs"
                    end try
                    set out to out & wid & US & ti & US & u & US & ({title_prop} of t) & US & r & RS
                end if
            end repeat
        end repeat
    end tell
    return out
    '''
    out = run(["osascript", "-e", script], timeout=6).rstrip("\n")
    found = []
    for rec in out.split(_RS):
        f = rec.split(_US)
        if len(f) < 5:
            continue
        wid, ti, url, tab_title = f[0], f[1], f[2], f[3]
        res = f[4:]
        base = {"app": app, "tab": [wid, ti]}
        if res[0] in ("playing", "paused"):
            title, artist = (res + ["", ""])[1:3]
            found.append(dict(base, state=res[0], title=_clean_title(title) or "Unknown",
                              artist=artist or "Unknown"))
        elif res[0] in ("nojs", "error") and _looks_like_playback(url):
            # No JS access: we know the tab, not whether it's playing.
            title = _clean_title(tab_title)
            artist = "YouTube" if "youtu" in url else app
            if " - " in title:
                artist, title = title.split(" - ", 1)
            found.append(dict(base, state="unknown", title=title or "Unknown", artist=artist, nojs=True))
    return found


def now_playing():
    """Best source wins: anything playing > browser tab we can't read (likely playing) > paused."""
    paused = []
    for app in ("Spotify", "Music"):
        if not app_running(app):
            continue
        out = osa(
            f'tell application "{app}"\n'
            f'  try\n'
            f'    set player_state to (player state as text)\n'
            f'    set track_name to (name of current track)\n'
            f'    set track_artist to (artist of current track)\n'
            f'    return player_state & "|" & track_name & "|" & track_artist\n'
            f'  on error\n'
            f'    return "stopped"\n'
            f'  end try\n'
            f'end tell'
        )
        if out and out != "stopped" and "|" in out:
            state, title, artist = (out.split("|") + ["", ""])[:3]
            item = {"app": app, "state": state, "title": title, "artist": artist}
            if state == "playing":
                return item
            paused.append(item)

    browser_items = []
    for app in BROWSERS:
        if app_running(app):
            browser_items += _browser_tabs(app)
    for item in browser_items:
        if item["state"] == "playing":
            return item

    unknown = [i for i in browser_items if i["state"] == "unknown"]
    rest = unknown + paused + [i for i in browser_items if i["state"] == "paused"]
    return rest[0] if rest else None


def _browser_media_control(np, action):
    js = _CONTROL_JS.get(action)
    if not js or not np.get("tab"):
        return False
    app = np["app"]
    wid, ti = np["tab"]
    tab = f"tab {ti} of (first window whose id is {wid})"
    script = f'''
    tell application "{app}"
        try
            return ({_js_cmd(app, tab, js)}) as text
        on error
            return "false"
        end try
    end tell
    '''
    return run(["osascript", "-e", script], timeout=5).strip() == "ok"


def media(action):
    np = now_playing()
    if action in ("playpause", "next", "prev"):
        if np and np["app"] in BROWSERS:
            _browser_media_control(np, action)
        else:
            app = np["app"] if np else ("Spotify" if app_running("Spotify") else "Music")
            cmds = {"playpause": "playpause", "next": "next track", "prev": "previous track"}
            osa(f'tell application "{app}" to {cmds[action]}')

    if action in ("volup", "voldown", "mute"):
        if action == "mute":
            osa("set volume output muted (not (output muted of (get volume settings)))")
        else:
            delta = 6 if action == "volup" else -6
            osa(f"set volume output volume ((output volume of (get volume settings)) + {delta})")


def volume():
    out = osa("output volume of (get volume settings) & \"|\" & output muted of (get volume settings)")
    try:  # osascript prints e.g. "69, |, false"
        v, m = [p.strip(" ,") for p in out.split("|")]
        return {"level": int(v), "muted": m == "true"}
    except Exception:
        return None


# --------------------------------------------------------------- shortcuts
MOD_MAP = {"cmd": "command down", "command": "command down", "shift": "shift down",
           "opt": "option down", "option": "option down", "alt": "option down",
           "ctrl": "control down", "control": "control down"}
KEY_CODES = {"return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "esc": 53,
             "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126,
             "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
             "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111}


def load_shortcuts():
    try:
        with open(SHORTCUTS_FILE) as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e), "buttons": []}


def send_keys(combo):
    """combo like 'cmd+shift+4' or 'ctrl+left'."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    key = parts[-1]
    mods = [MOD_MAP[p] for p in parts[:-1] if p in MOD_MAP]
    using = f" using {{{', '.join(mods)}}}" if mods else ""
    if key in KEY_CODES:
        action = f"key code {KEY_CODES[key]}{using}"
    else:
        action = f'keystroke "{key}"{using}'
    osa_checked(f'tell application "System Events" to {action}')


class ShortcutError(Exception):
    pass


def osa_checked(script):
    """Run AppleScript and turn macOS errors into a message the iPad can show."""
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise ShortcutError("macOS is asking for permission. Check the Mac's screen and allow it, "
                            "then try again")
    if p.returncode != 0:
        err = p.stderr.strip()
        if "not allowed to send keystrokes" in err or "(1002)" in err or "assistive access" in err:
            raise ShortcutError("Mac needs Accessibility permission for the app running DeskDeck "
                                "(System Settings > Privacy & Security > Accessibility)")
        raise ShortcutError(err.split("execution error:")[-1].strip() or "AppleScript failed")


def open_checked(args):
    p = subprocess.run(["open", *args], capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        raise ShortcutError(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "could not open it")


_FRONT_JXA = """
function run(argv) {
    ObjC.import('AppKit');
    var ws = $.NSWorkspace.sharedWorkspace;
    var path = ws.fullPathForApplication(argv[0]);
    if (!path || !path.js) return 'notfound';
    var bid = $.NSBundle.bundleWithPath(path).bundleIdentifier.js;
    var front = ws.frontmostApplication;
    if (!front || front.bundleIdentifier.js !== bid) return 'background';
    if (argv[1] === 'hide') front.hide;
    return 'front';
}
"""


def app_is_front(name, hide=False):
    """True if `name` is the frontmost app; with hide=True it's also hidden (like Cmd+H)."""
    out = run(["osascript", "-l", "JavaScript", "-e", _FRONT_JXA, name, "hide" if hide else ""])
    return out.strip() == "front"


def lock_screen():
    """Lock like Ctrl+Cmd+Q: login screen, display stays on, nothing sleeps or quits."""
    try:
        import ctypes
        ctypes.CDLL("/System/Library/PrivateFrameworks/login.framework/login").SACLockScreenImmediate()
    except Exception:  # Apple removed the call: turn the display off instead (locks if a password is required)
        subprocess.Popen(["pmset", "displaysleepnow"])


def run_shortcut(btn):
    kind = btn.get("type")
    if kind == "lock":
        lock_screen()
    elif kind == "keys":
        send_keys(btn["keys"])
    elif kind == "app":  # press once to bring it forward, again to hide it
        if not app_is_front(btn["app"], hide=True):
            open_checked(["-a", btn["app"]])
    elif kind == "url":  # optional "app" opens the link in that app instead of the default browser
        open_checked((["-a", btn["app"]] if btn.get("app") else []) + [btn["url"]])
    elif kind == "shortcut":  # an Apple Shortcuts shortcut, by name
        name = btn["shortcut"]
        if name not in run(["shortcuts", "list"]).splitlines():
            raise ShortcutError(f'No shortcut named "{name}" in the Shortcuts app on the Mac')
        subprocess.Popen(["shortcuts", "run", name])
    elif kind == "shell":  # only commands written in shortcuts.json on the Mac
        subprocess.Popen(btn["command"], shell=True, cwd=os.path.expanduser("~"))
    elif kind == "text":
        osa_checked('tell application "System Events" to keystroke ' + json.dumps(btn["text"]))
    else:
        raise ShortcutError(f"unknown shortcut type {kind!r}")


# --------------------------------------------------------------- Claude Code
class Claude:
    """Reads Claude Code's local files and live hook events."""

    def __init__(self):
        self.events = deque(maxlen=60)
        self.live = {}  # session_id -> {state, tool, detail, cwd, ts, message}
        self._file_cache = {}  # path -> (mtime, size, parsed)
        self.lock = threading.Lock()

    # ---- hook events (POSTed by hook.sh)
    def on_hook(self, ev):
        name = ev.get("hook_event_name", "?")
        sid = ev.get("session_id", "?")
        now = time.time()
        with self.lock:
            s = self.live.setdefault(sid, {"cwd": ev.get("cwd"), "state": "idle"})
            s["ts"] = now
            s["cwd"] = ev.get("cwd") or s.get("cwd")
            s["transcript"] = ev.get("transcript_path") or s.get("transcript")
            detail = ""
            if name == "UserPromptSubmit":
                s["state"], s["message"] = "working", ""
            elif name == "PreToolUse":
                ti = ev.get("tool_input") or {}
                detail = ti.get("file_path") or ti.get("command") or ti.get("pattern") or ti.get("url") or ""
                detail = os.path.basename(detail) if ti.get("file_path") else str(detail)[:80]
                s["state"], s["tool"], s["detail"], s["message"] = "working", ev.get("tool_name"), detail, ""
            elif name == "Notification":
                s["state"], s["message"] = "waiting", ev.get("message", "Needs your attention")
                detail = s["message"]
            elif name == "Stop":
                s["state"], s["tool"], s["message"] = "done", None, ""
            elif name == "SessionEnd":
                s["state"] = "ended"
            elif name == "SessionStart":
                s["state"] = "idle"
            self.events.appendleft({"ts": now, "event": name, "session": sid[:8],
                                    "project": project_name(s.get("cwd")),
                                    "tool": ev.get("tool_name"), "detail": detail})

    # ---- transcripts
    def _parse(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        cached = self._file_cache.get(path)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2]
        msgs, cwd, last_ts, model = {}, None, None, None
        custom_title = ai_title = last_prompt = None
        wanted = ('"assistant"', '"user"', '"ai-title"', '"custom-title"', '"last-prompt"')
        with open(path, errors="ignore") as f:
            for line in f:
                if not any(w in line for w in wanted):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = d.get("type")
                if t == "custom-title":  # the name you gave the session (or the app set)
                    custom_title = d.get("customTitle") or custom_title
                    continue
                if t == "ai-title":
                    ai_title = d.get("aiTitle") or ai_title
                    continue
                if t == "last-prompt":
                    last_prompt = d.get("lastPrompt") or last_prompt
                    continue
                if d.get("cwd"):
                    cwd = d["cwd"]
                if d.get("timestamp"):
                    last_ts = d["timestamp"]
                if t == "assistant":
                    m = d.get("message") or {}
                    u = m.get("usage")
                    if not u:
                        continue
                    model = m.get("model") or model
                    mid = m.get("id") or d.get("uuid")
                    msgs[mid] = (d.get("timestamp"), m.get("model"), u)  # last write wins (streaming)
        prompt = " ".join((last_prompt or "").split())
        title = custom_title or ai_title or (prompt[:60] + ("…" if len(prompt) > 60 else "") if prompt else None)
        parsed = {"msgs": msgs, "title": title, "cwd": cwd, "last_ts": last_ts,
                  "model": model, "last_prompt": prompt, "mtime": st.st_mtime}
        self._file_cache[path] = (st.st_mtime, st.st_size, parsed)
        return parsed

    @staticmethod
    def _ts(s):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0

    def running_pids(self):
        alive = {}
        for f in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
            try:
                d = json.load(open(f))
                os.kill(int(d["pid"]), 0)
                alive[d["sessionId"]] = d
            except Exception:
                continue
        return alive

    # ---- plan limits (5-hour and weekly)
    _plan_cache = (0, None)

    def _plan_usage(self):
        """Claude Code's own cached copy of your plan limits, from ~/.claude.json.
        It is refreshed by Claude Code (e.g. when you run /usage), so it can be old."""
        path = os.path.expanduser("~/.claude.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        if self._plan_cache[0] != mtime:
            try:
                with open(path) as f:
                    cu = json.load(f).get("cachedUsageUtilization") or {}
            except Exception:
                cu = {}
            self._plan_cache = (mtime, cu)
        return self._plan_cache[1] or None

    def limits(self, msg_times, now):
        cu = self._plan_usage() or {}
        util = cu.get("utilization") or {}
        fetched = (cu.get("fetchedAtMs") or 0) / 1000

        def cached(key):
            u = util.get(key) or {}
            return self._ts(u.get("resets_at") or ""), u.get("utilization")

        # 5-hour window: use Claude's exact reset if it's still ahead, otherwise rebuild the
        # windows from your message times (a window starts at the first message after the last one ended).
        five_reset, five_pct = cached("five_hour")
        five = {"resets_at": None, "pct": None, "exact": False}
        if five_reset > now:
            five.update(resets_at=five_reset, pct=five_pct, exact=True)
        else:
            end = five_reset or 0
            for t in sorted(x for x in msg_times if x):
                if t >= end:
                    end = t + 5 * 3600
            if end > now:
                five["resets_at"] = end

        # Weekly: resets at the same time every week, so roll Claude's last known reset forward.
        week_reset, week_pct = cached("seven_day")
        week = {"resets_at": None, "pct": None, "exact": False}
        if week_reset:
            fresh = week_reset > now
            while week_reset <= now:
                week_reset += 7 * 86400
            week.update(resets_at=week_reset, pct=week_pct if fresh else None, exact=True)
        return {"five_hour": five, "weekly": week, "fetched_at": fetched or None}

    def snapshot(self):
        now = time.time()
        files = [p for p in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl"))
                 if now - os.path.getmtime(p) < 86400 * 7]
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        win5 = now - 5 * 3600
        week = now - 7 * 86400
        tot = {"5h": self._z(), "today": self._z(), "week": self._z()}
        by_model = {}
        sessions = []
        running = self.running_pids()
        # A session can have copies in two project folders (e.g. after moving folders):
        # count each reply once, and show each session once (its newest file).
        all_msgs, newest = {}, {}
        for p in files:
            parsed = self._parse(p)
            if not parsed:
                continue
            all_msgs.update(parsed["msgs"])
            sid = os.path.splitext(os.path.basename(p))[0]
            if sid not in newest or parsed["mtime"] > newest[sid]["mtime"]:
                newest[sid] = parsed
        for ts, model, u in all_msgs.values():
            t = self._ts(ts or "")
            if t < week:
                continue
            self._add(tot["week"], u)
            if t >= midnight:
                self._add(tot["today"], u)
                self._add(by_model.setdefault(self._short(model), self._z()), u)
            if t >= win5:
                self._add(tot["5h"], u)
        for sid, parsed in newest.items():
            last = self._ts(parsed["last_ts"] or "")
            if sid in running or now - last < 3 * 3600:
                live = self.live.get(sid, {})
                state = live.get("state")
                if not state:
                    state = "running" if sid in running else "idle"
                if sid not in running and state not in ("waiting",):
                    state = "closed" if now - last > 600 else state
                sessions.append({
                    "id": sid[:8],
                    "title": parsed["title"] or "New session",
                    "prompt": parsed["last_prompt"][:140],
                    "project": project_name(parsed["cwd"] or live.get("cwd")),
                    "model": self._short(parsed["model"]),
                    "last": max(last, live.get("ts", 0)),
                    "state": state,
                    "tool": live.get("tool"),
                    "detail": live.get("detail"),
                    "message": live.get("message"),
                    "running": sid in running,
                    "kind": running.get(sid, {}).get("entrypoint"),
                })
        sessions.sort(key=lambda s: (s["state"] != "waiting", not s["running"], -s["last"]))
        limits = self.limits([self._ts(ts or "") for ts, _, _ in all_msgs.values()], now)
        # tokens used inside each limit's current window
        for key, span in (("five_hour", 5 * 3600), ("weekly", 7 * 86400)):
            lim = limits[key]
            start = (lim["resets_at"] - span) if lim["resets_at"] else now
            acc = self._z()
            for ts, _, u in all_msgs.values():
                if self._ts(ts or "") >= start:
                    self._add(acc, u)
            lim["tokens"] = acc
        return {
            "usage": tot, "by_model": by_model, "limits": limits,
            "sessions": sessions[:12], "events": list(self.events)[:25],
            "waiting": [s for s in sessions if s["state"] == "waiting"],
            "hooks_seen": len(self.events) > 0,
        }

    @staticmethod
    def _z():
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "messages": 0}

    @staticmethod
    def _add(acc, u):
        acc["input"] += u.get("input_tokens", 0) or 0
        acc["output"] += u.get("output_tokens", 0) or 0
        acc["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
        acc["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
        acc["messages"] += 1

    @staticmethod
    def _short(model):
        if not model:
            return "?"
        for k in ("opus", "sonnet", "haiku", "fable"):
            if k in model:
                return k.capitalize()
        return model


STATS = Stats()
CLAUDE = Claude()
_np_cache = {"t": 0, "v": None, "vol": None}


def state():
    if time.time() - _np_cache["t"] > 3:
        _np_cache.update(t=time.time(), v=now_playing(), vol=volume())
    sc = load_shortcuts()
    return {
        "time": time.time(),
        "host": socket.gethostname().replace(".local", ""),
        "stats": STATS.data,
        "media": _np_cache["v"],
        "volume": _np_cache["vol"],
        "claude": CLAUDE.snapshot(),
        "shortcuts": [{k: b.get(k) for k in ("id", "label", "icon", "color", "type")}
                      for b in sc.get("buttons", [])],
        "shortcuts_error": sc.get("error"),
    }


# --------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "DeskDeck"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self, q):
        if q.get("k", [""])[0] == TOKEN:
            return True
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "dd" in c and secrets.compare_digest(c["dd"].value, TOKEN)

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            if not self._authed(q):
                return self._send(401, "<h2 style='font-family:sans-serif'>Open the link printed by "
                                       "server.py on your Mac (it includes the access key).</h2>",
                                  "text/html")
            html = open(os.path.join(HERE, "index.html"), "rb").read()
            return self._send(200, html, "text/html; charset=utf-8", {
                "Set-Cookie": f"dd={TOKEN}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"})
        if u.path == "/manifest.json":
            return self._send(200, json.dumps({
                "name": "DeskDeck", "short_name": "DeskDeck", "display": "standalone",
                "background_color": "#000000", "theme_color": "#000000", "start_url": "/"}))
        if u.path == "/api/state":
            if not self._authed(q):
                return self._send(401, '{"error":"unauthorized"}')
            STATS.touch()
            return self._send(200, json.dumps(state()))
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 1_000_000)) if n else b""
        if u.path == "/api/hook":
            if not self._local():  # hooks only come from this Mac
                return self._send(403, '{"error":"local only"}')
            try:
                CLAUDE.on_hook(json.loads(raw or b"{}"))
            except ValueError:
                pass
            return self._send(200, "{}")
        if not self._authed(q):
            return self._send(401, '{"error":"unauthorized"}')
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        try:
            if u.path == "/api/run":
                btn = next((b for b in load_shortcuts().get("buttons", []) if b.get("id") == body.get("id")), None)
                if not btn:
                    return self._send(404, '{"error":"no such shortcut"}')
                run_shortcut(btn)
            elif u.path == "/api/media":
                media(body.get("action", ""))
                _np_cache["t"] = 0
            else:
                return self._send(404, '{"error":"not found"}')
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}))
        self._send(200, '{"ok":true}')


def lan_ip():
    default = ""
    for line in run(["route", "-n", "get", "default"]).splitlines():
        if "interface:" in line:
            default = line.split(":", 1)[1].strip()
    for iface in [default, "en0", "en1"]:
        if not iface:
            continue
        ip = run(["ipconfig", "getifaddr", iface]).strip()
        if ip:
            return ip
    return "127.0.0.1"


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = lan_ip()
    print("DeskDeck is running.\n", flush=True)
    print(f"  On the iPad open:  http://{ip}:{PORT}/?k={TOKEN}", flush=True)
    print(f"  On this Mac:       http://127.0.0.1:{PORT}/?k={TOKEN}\n", flush=True)
    print("  Then in Safari: Share -> Add to Home Screen.  Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
