#!/usr/bin/env python3
"""digisol-mcp: let AI assistants read, and safely configure, Digisol fibre ONTs over SSH.

One file, Python standard library only. Speaks MCP (JSON-RPC 2.0, newline-delimited) over stdio.

How it works
  * Every tool call opens one SSH session to the ONT, waits for the CLI prompt, types one
    command, reads up to the next prompt, and closes. Sessions are paced (MIN_GAP seconds apart)
    because ONTs have very little memory.
  * Nothing about your ONT is hardcoded. The server learns its commands by asking the CLI itself
    ("?" and "show ?"), so different Digisol models and firmware versions work unchanged.
  * Reads ("show ...", help with "?") run freely. Every other command is a change: it runs only
    after a person types a confirmation word into a desktop dialog. The AI cannot answer it.

Configuration (your ONT details never live in this repo): see README.md.
  ONT_CONFIG=/path/to/ont.json  or  ~/.config/digisol-mcp/ont.json  or  ONT_HOST=... env vars.

Usage
  digisol-mcp            run the MCP server (your AI client starts this for you)
  digisol-mcp --check    test your configuration and log in to every ONT once
"""

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

NAME = "digisol-mcp"
VERSION = "1.0.0"
PROTOCOL = "2025-06-18"

MIN_GAP = 5             # seconds between SSH sessions to one ONT
LOGIN_TIMEOUT = 30      # seconds to reach the first prompt
CMD_TIMEOUT = 20        # seconds to wait for the prompt after a command
CONSENT_TIMEOUT = 120   # seconds the approval dialog stays open

DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".config", "digisol-mcp", "ont.json")
DEFAULT_PROMPT = r"(?:^|\n)[\w.@()\-]{0,32}[>#] ?\Z"   # a short last line ending in > or #
FIELDS = {"host", "port", "user", "label", "password", "password_file", "keychain", "read_only", "prompt"}

# Words that make a change high-impact: the person must type the word itself, not just YES.
CRITICAL = {
    "reboot": "restarts the ONT: internet, voice and IPTV drop until it boots again",
    "restart": "restarts the ONT or a service: connections drop",
    "reset": "may FACTORY RESET the ONT: provider settings can be wiped and service stay down",
    "factory": "factory defaults: provider settings can be wiped and service stay down",
    "restore": "replaces the configuration: a bad file can take the line offline",
    "upgrade": "firmware change: a failure can brick the ONT",
    "firmware": "firmware change: a failure can brick the ONT",
    "backup": "writes a configuration file on the ONT",
    "debug": "low-level diagnostics: can disrupt the ONT",
    "account": "changes login accounts: a mistake can lock everyone out",
    "password": "changes a password: a mistake can lock everyone out",
    "passwd": "changes a password: a mistake can lock everyone out",
    "remoteaccess": "changes how the ONT can be reached: a mistake can lock you out",
    "wan": "changes the internet (WAN) side: a mistake can cut internet or voice",
    "ppp": "changes PPPoE/PPP: a mistake can cut internet",
    "lan": "changes the LAN side: a mistake can break local access",
    "route": "changes routing",
    "bridge": "changes bridging: a mistake can cut downstream equipment",
    "vlan": "changes VLANs: a mistake can cut internet, voice or IPTV",
}

LOGIN_HINTS = [
    ("Permission denied", "wrong username or password"),
    ("Connection refused", "SSH is off on the ONT, or wrong port: enable SSH under Remote Access in its web page"),
    ("timed out", "the ONT did not answer: check the IP and that this computer is on the ONT's network"),
    ("No route to host", "the ONT is unreachable from this computer: check the IP"),
    ("Could not resolve", "unknown hostname: use the ONT's IP address"),
    ("no matching", "the ONT uses old SSH algorithms your ssh client refuses (see README, Troubleshooting)"),
    ("IDENTIFICATION HAS CHANGED", "the ONT's SSH key changed: if expected, run  ssh-keygen -R <host>"),
    ("Too many authentication failures", "ssh offered too many keys: see README, Troubleshooting"),
]
NOISE = re.compile(r"^(Warning: Permanently added .*|ip6tables v[\d.]+: .*|Try `ip6tables -h'.*|arg_(name|value):.*)$")


class OntError(Exception):
    pass


# ---------------------------------------------------------------- configuration

DEVICES, DEFAULT_ID, CONFIG_ERROR = {}, None, None


def make_device(dev_id, raw):
    if not isinstance(raw, dict):
        raise OntError("device %r must be an object" % dev_id)
    unknown = set(raw) - FIELDS
    if unknown:  # catches typos like "pasword" before they become confusing login errors
        raise OntError("device %r: unknown field(s) %s; allowed: %s"
                       % (dev_id, ", ".join(sorted(unknown)), ", ".join(sorted(FIELDS))))
    host = str(raw.get("host", "")).strip()
    if not host:
        raise OntError("device %r needs a host (the ONT's IP address)" % dev_id)
    d = {"id": dev_id, "host": host, "port": int(raw.get("port") or 22),
         "user": str(raw.get("user") or "admin"), "label": str(raw.get("label") or "Digisol ONT"),
         "read_only": raw.get("read_only") is True, "prompt": re.compile(raw.get("prompt") or DEFAULT_PROMPT)}
    for k in ("password", "password_file", "keychain"):
        if raw.get(k):
            d[k] = str(raw[k])
    return d


def load_config(environ=os.environ):
    """Return (devices_by_id, default_id). JSON file first, then ONT_* environment variables."""
    path = environ.get("ONT_CONFIG") or (DEFAULT_CONFIG if os.path.isfile(DEFAULT_CONFIG) else "")
    if path:
        path = os.path.expanduser(path)
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            raise OntError("cannot read %s: %s" % (path, e))
        devs = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(devs, dict) or not devs:
            raise OntError('%s must look like {"devices": {"home": {"host": "192.168.1.1", ...}}}' % path)
        devices = {k: make_device(k, v) for k, v in devs.items()}
        default = raw.get("default") or next(iter(devs))
        if default not in devices:
            raise OntError("%s: default %r is not one of the devices" % (path, default))
        return devices, default
    if environ.get("ONT_HOST"):
        env_map = {"host": "ONT_HOST", "port": "ONT_PORT", "user": "ONT_USER", "label": "ONT_LABEL",
                   "password": "ONT_PASSWORD", "password_file": "ONT_PASSWORD_FILE",
                   "keychain": "ONT_KEYCHAIN", "prompt": "ONT_PROMPT"}
        raw = {k: environ[v] for k, v in env_map.items() if environ.get(v)}
        if environ.get("ONT_READ_ONLY", "").lower() in ("1", "true", "yes"):
            raw["read_only"] = True
        return {"ont": make_device("ont", raw)}, "ont"
    raise OntError("no ONT configured: set ONT_HOST (plus a password) in your MCP client config, "
                   "or create %s. See the README." % DEFAULT_CONFIG)


def resolve(device_id):
    if CONFIG_ERROR:
        raise OntError(CONFIG_ERROR)
    key = (device_id or "").strip() or DEFAULT_ID
    if key not in DEVICES:
        raise OntError("unknown device %r; configured: %s" % (key, ", ".join(sorted(DEVICES))))
    return DEVICES[key]


def get_password(d):
    if "password_file" in d:
        try:
            with open(os.path.expanduser(d["password_file"]), encoding="utf-8") as f:
                pw = f.read().rstrip("\r\n")
        except OSError as e:
            raise OntError("password_file: %s" % e)
        if not pw:
            raise OntError("password_file %s is empty" % d["password_file"])
        return pw
    if "keychain" in d:
        if sys.platform == "darwin":
            cmd = ["security", "find-generic-password", "-s", d["keychain"], "-a", d["user"], "-w"]
        else:
            cmd = ["secret-tool", "lookup", "service", d["keychain"], "user", d["user"]]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OntError("keychain lookup failed: %s" % e)
        if out.returncode == 0 and out.stdout.rstrip("\r\n"):
            return out.stdout.rstrip("\r\n")
        raise OntError("no password in the keychain for service %r, account %r" % (d["keychain"], d["user"]))
    if "password" in d:
        return d["password"]
    raise OntError("device %r has no password: set password_file, keychain or password" % d["id"])


# ---------------------------------------------------------------- SSH session

_lock = threading.Lock()
_last = {}
_askpass = None


def askpass_program():
    """A tiny wrapper so ssh can ask *this* file for the password (SSH_ASKPASS takes no arguments)."""
    global _askpass
    if _askpass is None:
        folder = tempfile.mkdtemp(prefix="digisol-mcp-")   # private: mode 700
        me = os.path.abspath(__file__)
        if os.name == "nt":
            path, body = os.path.join(folder, "askpass.cmd"), '@"%s" "%s" %%*\r\n' % (sys.executable, me)
        else:
            path = os.path.join(folder, "askpass.sh")
            body = "#!/bin/sh\nexec %s %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(me))
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o700)
        _askpass = path
    return _askpass


def _pump(stream, q):
    try:
        for chunk in iter(lambda: stream.read1(4096), b""):
            q.put(chunk)
    except (OSError, ValueError):   # pipe closed when the session ends
        pass
    q.put(None)


def read_until_prompt(q, timeout, prompt):
    buf, end = b"", time.monotonic() + timeout
    while True:
        try:
            chunk = q.get(timeout=max(0.0, end - time.monotonic()))
        except queue.Empty:
            chunk = None
        text = buf.decode("utf-8", "replace")
        if chunk is None:
            return text, False
        buf += chunk
        text = buf.decode("utf-8", "replace").replace("\r", "")
        if prompt.search(text):
            return text, True


def login_error(d, text):
    hint = next((h for k, h in LOGIN_HINTS if k in text), "no CLI prompt seen")
    tail = " ".join(text.split())[-300:]
    return "could not log in to %s@%s: %s%s" % (d["user"], d["host"], hint, (" (ssh said: %s)" % tail) if tail else "")


def clean(output, command, prompt):
    lines = output.replace("\r", "").split("\n")
    if lines and lines[0].strip() == command:   # the CLI echoes the command
        lines = lines[1:]
    if command.endswith("?"):                   # some firmware prefixes help with this
        lines = [l for l in lines if l.strip() != "command error!"]
    lines = [l.rstrip() for l in lines if not NOISE.match(l.strip())]
    if lines and prompt.search("\n" + lines[-1]):
        lines.pop()
    return "\n".join(lines).strip("\n")


def run_on_ont(command, d):
    with _lock:
        wait = _last.get(d["host"], 0.0) + MIN_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        env = dict(os.environ, SSH_ASKPASS=askpass_program(), SSH_ASKPASS_REQUIRE="force",
                   DISPLAY=os.environ.get("DISPLAY", ":0"), DIGISOL_MCP_ASKPASS="1",
                   DIGISOL_MCP_SECRET=get_password(d))
        cmd = ["ssh", "-T", "-p", str(d["port"]),
               "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
               "-o", "NumberOfPasswordPrompts=1",
               "-o", "PreferredAuthentications=password,keyboard-interactive",
               "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
               "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
               "%s@%s" % (d["user"], d["host"])]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env)
        except FileNotFoundError:
            raise OntError("the 'ssh' program was not found: install an OpenSSH client")
        q = queue.Queue()
        threading.Thread(target=_pump, args=(proc.stdout, q), daemon=True).start()
        try:
            banner, ok = read_until_prompt(q, LOGIN_TIMEOUT, d["prompt"])
            if not ok:
                raise OntError(login_error(d, banner))
            proc.stdin.write((command + "\n").encode())
            proc.stdin.flush()
            out, ok = read_until_prompt(q, CMD_TIMEOUT, d["prompt"])
            result = clean(out, command, d["prompt"])
            if not ok:
                result += ("\n[no prompt within %ds: the command may still be running, or the ONT asked a "
                           "question; the session was closed without answering]" % CMD_TIMEOUT)
            return result
        finally:
            for step in (proc.stdin.close, proc.terminate):
                try:
                    step()
                except OSError:
                    pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc.stdout.close()
            _last[d["host"]] = time.monotonic()


# ---------------------------------------------------------------- learning the CLI

_cli = {}


def parse_help(text):
    """Command words from a CLI help table: '    status      System status'."""
    return re.findall(r"^\s{2,}([A-Za-z][\w-]*)\s{2,}\S", text, re.M)


def cli_words(d, path):
    """What the ONT offers after `path` (asked once per run, then cached)."""
    key = (d["id"], path)
    if key not in _cli:
        _cli[key] = parse_help(run_on_ont((path + " ?").strip(), d))
    return _cli[key]


# ---------------------------------------------------------------- classifying commands

def check_text(command):
    if not command or not command.strip():
        raise OntError("empty command")
    if re.search(r"[\x00-\x1f\x7f]", command):
        raise OntError("control characters or line breaks are not allowed (one command per call)")
    if len(command) > 300:
        raise OntError("command too long")
    return " ".join(command.split())


def classify(cmd, d):
    """Return (level, warning, confirm_word); level is read, change or critical. Raises if refused."""
    words = cmd.split()
    head = words[0].lower()
    if head in ("exit", "quit", "logout"):
        raise OntError("not needed: each call ends its own session")
    if words[-1] == "?" or head in ("?", "help"):
        return "read", None, None
    if head == "show":
        if len(words) < 2:
            raise OntError("say what to show, e.g. 'show status'; ask ont_help('show') for the list")
        items = cli_words(d, "show")
        if items and words[1] not in items:
            raise OntError("this ONT has no 'show %s'; it offers: %s" % (words[1], ", ".join(items)))
        return "read", None, None
    top = cli_words(d, "")
    if top and words[0] not in top:
        raise OntError("this ONT has no command %r; it offers: %s" % (words[0], ", ".join(top)))
    if d["read_only"]:
        raise OntError("device %r is read_only in your config: changes are disabled" % d["id"])
    for w in words:
        if w.lower() in CRITICAL:
            return "critical", CRITICAL[w.lower()], w.upper()
    return "change", "changes a setting on the ONT", "YES"


# ---------------------------------------------------------------- approval (a person, not the AI)

def _dialog_mac(title, body, critical):
    q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    script = ('display dialog %s with title %s default answer "" buttons {"Cancel", "Run"} '
              'default button "Cancel" cancel button "Cancel" with icon %s giving up after %d'
              % (q(body), q(title), "stop" if critical else "caution", CONSENT_TIMEOUT))
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                         timeout=CONSENT_TIMEOUT + 15, check=False)
    m = re.search(r"text returned:(.*?)(?:, gave up:|$)", out.stdout.strip())
    if out.returncode or "gave up:true" in out.stdout or "button returned:Run" not in out.stdout or not m:
        return None
    return m.group(1)


def _dialog_windows(title, body):
    s = lambda v: "'" + v.replace("'", "''") + "'"
    ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
          "[Microsoft.VisualBasic.Interaction]::InputBox(%s, %s, '')" % (s(body), s(title)))
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True, timeout=CONSENT_TIMEOUT + 15, check=False)
    return out.stdout if out.returncode == 0 else None


def _dialog_linux(title, body):
    if shutil.which("zenity"):
        cmd = ["zenity", "--entry", "--title", title, "--text", body, "--timeout", str(CONSENT_TIMEOUT)]
    elif shutil.which("kdialog"):
        cmd = ["kdialog", "--title", title, "--inputbox", body, ""]
    else:
        raise OSError("install zenity or kdialog to approve changes")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=CONSENT_TIMEOUT + 15, check=False)
    return out.stdout if out.returncode == 0 else None


def ask_consent(cmd, level, warning, word, d, reason=""):
    """Desktop dialog. True only if a person typed the confirmation word and confirmed."""
    title = "ONT %s: approval needed" % ("DANGER" if level == "critical" else "change")
    body = ("Your AI assistant wants to run this on %s (%s@%s):\n\n    %s\n\n%s.%s\n\n"
            "Type %s and confirm to allow it. Anything else cancels."
            % (d["label"], d["user"], d["host"], cmd, warning.capitalize(),
               ("\nReason given: " + reason) if reason else "", word))
    try:
        if sys.platform == "darwin":
            typed = _dialog_mac(title, body, level == "critical")
        elif os.name == "nt":
            typed = _dialog_windows(title, body)
        else:
            typed = _dialog_linux(title, body)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "no approval dialog could be shown (%s)" % e
    if typed is not None and typed.strip() == word:
        return True, "approved by the person at this computer"
    return False, "the person did not approve it (it needs %s typed and confirmed)" % word


def execute(command, reason="", device_id=None):
    d = resolve(device_id)
    cmd = check_text(command)
    level, warning, word = classify(cmd, d)
    note = ""
    if level != "read":
        ok, why = ask_consent(cmd, level, warning, word, d, reason)
        if not ok:
            raise OntError("not run: " + why)
        note = "\n\n[%s]" % why
    return "[%s · %s] %s\n\n%s%s" % (d["id"], d["host"], cmd, run_on_ont(cmd, d) or "(no output)", note)


# ---------------------------------------------------------------- MCP tools

def _schema(props, required):
    props = dict(props)
    if len(DEVICES) > 1:
        props["device"] = {"type": "string", "enum": sorted(DEVICES),
                           "description": "Which ONT (default: %s)." % DEFAULT_ID}
    return {"type": "object", "properties": props, "required": required}


def tools():
    multi = " Several ONTs are configured: pick one with 'device'." if len(DEVICES) > 1 else ""
    return [
        {"name": "ont_show", "fn": lambda a: execute("show " + str(a.get("item", "")).strip(), device_id=a.get("device")),
         "description": "Read status or settings from the Digisol fibre ONT with 'show <item>'. Read-only, "
                        "safe. Common items: status (uptime, firmware, WAN), wan, interface, lan, dhcp, arp, route, "
                        "remoteaccess, portforward, tr069. Call ont_help with path 'show' to list every item "
                        "this ONT offers." + multi,
         "inputSchema": _schema({"item": {"type": "string", "description": "What to show, e.g. 'status'. "
                                          "Extra words are passed through, e.g. 'interface eth0'."}}, ["item"])},
        {"name": "ont_help", "fn": lambda a: execute((str(a.get("path", "")).strip() + " ?").strip(), device_id=a.get("device")),
         "description": "Ask the ONT's own CLI what commands exist under a path: '' for the top level, "
                        "'show', 'config', 'config wan' and so on. Read-only: it only appends '?'. Use it to "
                        "explore before running a change." + multi,
         "inputSchema": _schema({"path": {"type": "string", "description": "Command path; empty for top level."}}, [])},
        {"name": "ont_run", "fn": lambda a: execute(str(a.get("command", "")), str(a.get("reason", "")), a.get("device")),
         "description": "Run a command that changes the ONT (config ..., save, reboot, ...). Before it runs, "
                        "the person at the computer sees a dialog naming the ONT, the command and its risk, and "
                        "must type a confirmation word; you cannot answer it. High-impact commands (reboot, reset, "
                        "restore, firmware, WAN/LAN/PPP/VLAN, accounts, remote access) need that word typed. "
                        "Explain in 'reason' why the change is needed. Many ONTs need a save command (for "
                        "example 'config save') before a change survives a reboot." + multi,
         "inputSchema": _schema({"command": {"type": "string", "description": "One CLI command."},
                                 "reason": {"type": "string", "description": "Why, shown in the dialog."}}, ["command"])},
    ]


# ---------------------------------------------------------------- MCP protocol

class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                "serverInfo": {"name": NAME, "version": VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in tools()]}
    if method == "tools/call":
        tool = next((t for t in tools() if t["name"] == params.get("name")), None)
        if not tool:
            raise RpcError(-32602, "unknown tool: %s" % params.get("name"))
        try:
            text, is_error = tool["fn"](params.get("arguments") or {}), False
        except (OntError, OSError, ValueError) as e:
            text, is_error = "Error: %s" % e, True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    raise RpcError(-32601, "method not found: %s" % method)


def serve():
    send = lambda obj: (sys.stdout.write(json.dumps(obj) + "\n"), sys.stdout.flush())
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if not isinstance(msg, dict) or "id" not in msg:
            continue   # notifications need no answer
        try:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)})
        except RpcError as e:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": e.code, "message": str(e)}})
        except Exception as e:  # keep serving; report on stderr, never stdout
            print("[%s] internal error: %r" % (NAME, e), file=sys.stderr, flush=True)
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}})


def check():
    if CONFIG_ERROR:
        print("✗ configuration: %s" % CONFIG_ERROR)
        return 2
    failed = False
    for d in DEVICES.values():
        print("• %s  %s@%s:%d%s" % (d["id"], d["user"], d["host"], d["port"], "  (read-only)" if d["read_only"] else ""))
        try:
            words = parse_help(run_on_ont("?", d))
            print("  ✓ logged in. CLI commands: %s" % (", ".join(words) or "(help not recognised; reads still work)"))
        except OntError as e:
            print("  ✗ %s" % e)
            failed = True
    return 1 if failed else 0


def main(argv=None):
    global DEVICES, DEFAULT_ID, CONFIG_ERROR
    if os.environ.get("DIGISOL_MCP_ASKPASS") == "1":   # ssh is asking us for the password
        sys.stdout.write(os.environ.get("DIGISOL_MCP_SECRET", ""))
        return 0
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["--version"], ["-V"]):
        print(NAME, VERSION)
        return 0
    try:
        DEVICES, DEFAULT_ID = load_config()
    except OntError as e:   # still start, so the error shows up in the AI client
        CONFIG_ERROR = str(e)
    if argv[:1] == ["--check"]:
        return check()
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
