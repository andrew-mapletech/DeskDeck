#!/usr/bin/env python3
"""DeskDeck all-in-one installer for macOS.

  python3 install.py              install, or repair after moving the folder / new Mac
  python3 install.py --check      only report what's set up, change nothing
  python3 install.py --uninstall  stop DeskDeck and remove the background service
  add --yes to answer yes to every question

It sets up the background service (starts at login, restarts if it stops),
optionally adds the Claude Code status hooks, checks your shortcut buttons,
and prints the link to open on the iPad. Safe to run as often as you like.
Only Python's standard library is used.
"""
import json
import os
import plistlib
import platform
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
LABEL = "com.deskdeck.server"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LOG = os.path.expanduser("~/Library/Logs/DeskDeck.log")
PORT = int(os.environ.get("DESKDECK_PORT", "8770"))
DOMAIN = f"gui/{os.getuid()}"
ARGS = set(sys.argv[1:])
YES = "--yes" in ARGS or "-y" in ARGS

G, Y, R, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    G = Y = R = B = X = ""


def ok(msg):
    print(f"  {G}✓{X} {msg}")


def warn(msg):
    print(f"  {Y}!{X} {msg}")


def fail(msg):
    print(f"  {R}✗{X} {msg}")
    sys.exit(1)


def step(msg):
    print(f"\n{B}{msg}{X}")


def sh(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def ask(question, default=True):
    if YES:
        return True
    if not sys.stdin.isatty():
        return default
    hint = "[Y/n]" if default else "[y/N]"
    ans = input(f"  ? {question} {hint} ").strip().lower()
    return default if not ans else ans.startswith("y")


# ------------------------------------------------------------------ checks
def check_system():
    step("Checking this Mac")
    if platform.system() != "Darwin":
        fail("DeskDeck only runs on macOS.")
    ok(f"macOS {platform.mac_ver()[0]} ({platform.machine()})")
    if sys.version_info < (3, 9):
        fail(f"Python 3.9 or newer is needed (this is {platform.python_version()}). "
             "Get it from https://www.python.org/downloads/")
    ok(f"Python {platform.python_version()}")
    for f in ("server.py", "index.html", "shortcuts.json"):
        if not os.path.exists(os.path.join(HERE, f)):
            fail(f"{f} is missing from {HERE}. Run this from the DeskDeck folder.")
    rc, out = sh([sys.executable, "-m", "py_compile", SERVER])
    if rc != 0:
        fail(f"server.py has an error:\n{out}")
    try:
        with open(os.path.join(HERE, "shortcuts.json")) as f:
            json.load(f)
    except ValueError as e:
        fail(f"shortcuts.json isn't valid JSON: {e}")
    ok(f"DeskDeck folder: {HERE}")


def existing_plist():
    try:
        with open(PLIST, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return None


def pick_python():
    """Keep the Python the service already uses (macOS permissions are tied to it), else this one."""
    old = existing_plist()
    if old:
        prev = (old.get("ProgramArguments") or [None])[0]
        if prev and os.path.exists(prev):
            return prev
    return sys.executable


def service_loaded():
    return sh(["launchctl", "print", f"{DOMAIN}/{LABEL}"])[0] == 0


def port_owner():
    """(pid, command) of whatever is listening on the port, or None."""
    rc, out = sh(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-t"])
    pids = [p for p in out.split() if p.isdigit()]
    if not pids:
        return None
    _, cmd = sh(["ps", "-o", "command=", "-p", pids[0]])
    return int(pids[0]), cmd


def wait_until(cond, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return cond()


def token():
    try:
        with open(os.path.join(HERE, ".token")) as f:
            return f.read().strip()
    except OSError:
        return None


def lan_ip():
    _, out = sh(["route", "-n", "get", "default"])
    iface = next((l.split(":", 1)[1].strip() for l in out.splitlines() if "interface:" in l), "")
    for i in [iface, "en0", "en1"]:
        if i:
            rc, ip = sh(["ipconfig", "getifaddr", i])
            if rc == 0 and ip:
                return ip
    return "127.0.0.1"


def server_answers():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True  # it answered (e.g. 401 without the key)
    except Exception:
        return False


_APP_JXA = """function run(argv){ObjC.import('AppKit');
var p=$.NSWorkspace.sharedWorkspace.fullPathForApplication(argv[0]);return (p&&p.js)?p.js:'';}"""


def app_path(name):
    _, out = sh(["osascript", "-l", "JavaScript", "-e", _APP_JXA, name])
    return out if out.startswith("/") else ""


# ------------------------------------------------------------------ install
def install_service():
    step("Background service")
    python = pick_python()
    if python != sys.executable:
        ok(f"Keeping the Python the service already uses: {python}")
    else:
        ok(f"Using Python: {python}")

    owner = port_owner()
    if owner and not service_loaded():
        pid, cmd = owner
        if "server.py" in cmd:
            warn(f"An old DeskDeck is already running outside the service (pid {pid}).")
            if not ask("Stop it so the service can take over?"):
                fail("Can't continue while it's using the port. Close it and run this again.")
            os.kill(pid, 15)
            wait_until(lambda: port_owner() is None, 10)
        else:
            fail(f"Port {PORT} is used by another program: {cmd}\n"
                 f"    Run with a different port, e.g.  DESKDECK_PORT=8771 python3 install.py")

    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    if "DESKDECK_PORT" in os.environ:
        env["DESKDECK_PORT"] = str(PORT)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [python, SERVER],
        "WorkingDirectory": HERE,
        "EnvironmentVariables": env,
        "RunAtLoad": True,       # start at login
        "KeepAlive": True,       # start again if it ever stops
        "ThrottleInterval": 10,
        "StandardOutPath": LOG,
        "StandardErrorPath": LOG,
    }
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    changed = existing_plist() != plist
    if changed:
        with open(PLIST, "wb") as f:
            plistlib.dump(plist, f)
        ok(f"Wrote {PLIST.replace(os.path.expanduser('~'), '~')}")
    else:
        ok("Service file already up to date")

    if service_loaded():
        sh(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"])
        wait_until(lambda: not service_loaded() and port_owner() is None, 10)
    rc, out = sh(["launchctl", "bootstrap", DOMAIN, PLIST])
    if rc != 0 and not service_loaded():
        fail(f"launchctl couldn't start the service: {out}")
    if not wait_until(server_answers, 15):
        fail(f"The service started but DeskDeck isn't answering. Last lines of the log:\n"
             + "\n".join("    " + l for l in open(LOG).read().splitlines()[-8:]))
    ok("DeskDeck is running and will start automatically at login")

    cmd_file = os.path.join(HERE, "start-deskdeck.command")
    if os.path.exists(cmd_file):
        os.chmod(cmd_file, 0o755)


def install_hooks():
    step("Claude Code live status")
    if not (os.path.isdir(os.path.expanduser("~/.claude")) or shutil.which("claude")):
        warn("Claude Code isn't installed on this Mac, skipping. Run this again after installing it.")
        return
    settings = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings) as f:
            already = "/api/hook" in f.read()
    except OSError:
        already = False
    if already:
        ok("Hooks already installed")
        return
    if not ask("Add DeskDeck hooks to Claude Code so the iPad shows live session status?"):
        warn("Skipped. Run  python3 install_hooks.py  any time to add them.")
        return
    rc, out = sh([sys.executable, os.path.join(HERE, "install_hooks.py")])
    if rc == 0:
        ok("Hooks installed. Restart any open Claude Code sessions.")
    else:
        warn(f"Couldn't add hooks: {out}")


def check_shortcuts():
    step("Shortcut buttons")
    with open(os.path.join(HERE, "shortcuts.json")) as f:
        buttons = json.load(f).get("buttons", [])
    missing, needs_access = [], False
    for b in buttons:
        if b.get("type") in ("app", "url") and b.get("app"):
            if not app_path(b["app"]):
                missing.append(f'{b.get("label", b["app"])} (needs the app "{b["app"]}")')
        if b.get("type") in ("keys", "text"):
            needs_access = True
    if missing:
        for m in missing:
            warn(f"Not installed on this Mac: {m}")
        print("    Install those apps, or change/remove the buttons in shortcuts.json.")
    else:
        ok(f"All {len(buttons)} buttons point at apps that exist")
    return needs_access


def finish(needs_access):
    step("One-time steps on this Mac")
    python = pick_python()
    todo = []
    browsers = [n for n in ("Brave Browser", "Google Chrome", "Microsoft Edge", "Vivaldi", "Opera", "Arc")
                if app_path(n)]
    if browsers:
        todo.append(f"In {', '.join(browsers)}: menu bar  View → Developer → Allow JavaScript from Apple Events\n"
                    "      (lets Now Playing see and control YouTube etc. in that browser)")
    if app_path("Safari"):
        todo.append("In Safari (only if you play music there): Settings → Advanced → Show features for web "
                    "developers,\n      then Develop → Allow JavaScript from Apple Events")
    todo.append("The first time you use Now Playing or a button, macOS asks to let Python control that app.\n"
                "      Click Allow. (If you missed it: System Settings → Privacy & Security → Automation → Python)")
    if needs_access:
        todo.append("For keys/text buttons: System Settings → Privacy & Security → Accessibility → turn on Python\n"
                    f"      (the one at {python})")
    todo.append("If macOS asks whether Python may accept incoming network connections, click Allow\n"
                "      (otherwise the iPad can't reach DeskDeck).")
    for i, t in enumerate(todo, 1):
        print(f"  {i}. {t}")

    step("Open it on the iPad")
    tok = token()
    if tok:
        print(f"  {B}http://{lan_ip()}:{PORT}/?k={tok}{X}")
        print("  Open that in Safari on the iPad (same Wi-Fi), then Share → Add to Home Screen.")
    else:
        warn(f"No access key yet. It's printed in {LOG} once DeskDeck starts.")
    print(f"\n  Log: {LOG.replace(os.path.expanduser('~'), '~')}")
    print(f"  Restart after editing server.py:  launchctl kickstart -k {DOMAIN}/{LABEL}")
    print("  Uninstall:  python3 install.py --uninstall\n")


def uninstall():
    step("Uninstalling DeskDeck")
    if service_loaded():
        sh(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"])
        ok("Service stopped")
    if os.path.exists(PLIST):
        os.remove(PLIST)
        ok("Removed the background service (DeskDeck won't start at login)")
    settings = os.path.expanduser("~/.claude/settings.json")
    try:
        has_hooks = "/api/hook" in open(settings).read()
    except OSError:
        has_hooks = False
    if has_hooks and ask("Also remove the DeskDeck hooks from Claude Code?"):
        rc, out = sh([sys.executable, os.path.join(HERE, "install_hooks.py"), "--remove"])
        ok("Hooks removed") if rc == 0 else warn(out)
    print(f"\n  The DeskDeck folder itself is untouched: {HERE}\n")


def check_only():
    check_system()
    step("Status")
    plist = existing_plist()
    if plist:
        ok(f"Service file exists, runs: {' '.join(plist.get('ProgramArguments', []))}")
        if plist.get("ProgramArguments", [None, None])[1:2] != [SERVER]:
            warn("It points at a different folder. Run  python3 install.py  to fix.")
    else:
        warn("Background service not installed")
    ok("Service loaded") if service_loaded() else warn("Service not loaded")
    ok(f"Answering on port {PORT}") if server_answers() else warn(f"Nothing answering on port {PORT}")
    needs_access = check_shortcuts()
    finish(needs_access)


def main():
    print(f"{B}DeskDeck installer{X}")
    if "--uninstall" in ARGS:
        uninstall()
    elif "--check" in ARGS:
        check_only()
    else:
        check_system()
        install_service()
        install_hooks()
        finish(check_shortcuts())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
