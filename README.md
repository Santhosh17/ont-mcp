<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.svg">
  <img src="assets/banner-light.svg" alt="ONT MCP: your fibre box, now fluent in AI" width="100%">
</picture>

**An [MCP](https://modelcontextprotocol.io) server that lets Claude, Cursor, VS Code and other AI assistants read and safely configure fibre ONTs, or run 100% offline with a local model.**

**Tested on:** Digisol DG-GR6011 (EPON) · [see all tested hardware](#-tested-hardware)

[![tests](https://github.com/Santhosh17/ont-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/Santhosh17/ont-mcp/actions/workflows/test.yml)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)
![Zero dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)
![One file](https://img.shields.io/badge/server-one%20file-blueviolet)
![macOS · Linux · Windows](https://img.shields.io/badge/runs%20on-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey)
[![MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[Quick start](#-quick-start-3-minutes) · [Tested hardware](#-tested-hardware) · [What you can ask](#-what-you-can-ask) · [Local models](#-run-it-100-locally) · [Safety](#-safety-built-in-not-bolted-on) · [Configuration](#%EF%B8%8F-configuration) · [Troubleshooting](#-troubleshooting)

</div>

---

```text
You:     Why is my internet slow tonight? Check the fibre box.

Claude:  ▸ ont_show status
         ▸ ont_show wan

         Your ONT has been up for 18 hours and the WAN link is connected.
         PPPoE session is up and the default gateway answers, so the fibre
         line itself looks healthy. The slowdown is more likely on your Wi-Fi
         or router. Want me to check the DHCP table for unknown devices?
```

No web page with a captcha. No vendor app. No CLI manuals. **You ask, your assistant reads the ONT, you get an answer.**

## ✨ Why you'll like it

| | |
|---|---|
| 🧠 **Learns your ONT by itself** | Nothing is hardcoded. It asks your ONT's own CLI what it supports, so different models and firmware versions just work. |
| 🛡️ **You approve every change** | Reads are free. Every change pops up a dialog on *your* screen, and you have to type a word to allow it. The AI cannot click it for you. |
| 📦 **Zero dependencies** | One Python file, standard library only. No `npm install`, no Docker, nothing to break. |
| 🔐 **Your secrets stay yours** | Your ONT's IP and password live in *your* config: Keychain, a password file or env vars. Never in this repo. |
| 🏡 **Runs 100% offline** | Pair it with a local model in LM Studio or Ollama, and nothing leaves your home network. |
| 🏠 **Several ONTs** | Home, office, parents' house: one server, one config, pick by name. |
| 🐢 **Gentle on the hardware** | ONTs have tiny memory. Sessions are paced and short, so the box stays healthy. |

## 📟 Tested hardware

| Make | Model | Type | Firmware | Result |
|---|---|---|---|---|
| **Digisol** | **DG-GR6011** | EPON ONT (Realtek SDK) | V1.0.00-240813 | ✅ Tested on real hardware: SSH login, CLI discovery, `show` commands, Claude and local Qwen3.8 27B |

**Should also work:** other ONTs whose SSH command line answers `?` with a command list, which is common on Realtek-based GPON/EPON ONTs. The server learns each box's commands itself, so nothing is model-specific.

**Got another make or model working?** Open an issue with the make, model, firmware and the output of `ont-mcp --check`. It takes 2 minutes and gets your box into this table. ❤️

## 🚀 Quick start (3 minutes)

### 1 · Turn on SSH on your ONT

Open your ONT's web page (the address is on the sticker under the box, or it is your router's gateway address; it varies by ISP, e.g. `192.168.1.1`, `192.168.0.1` or `192.168.100.1`), find **Remote Access** (sometimes **Service Control**) and allow **SSH on the LAN side**. Then check it works from a terminal:

```bash
ssh admin@YOUR-ONT-IP
```

Replace `YOUR-ONT-IP` here and in every config below with your ONT's real address. Not sure what it is? Run `netstat -rn | grep default` (macOS/Linux) or `ipconfig` (Windows) and look for the default gateway, or check the sticker on the box.

If you get a `>` prompt, you're good. Type `exit`.

> Got `no matching host key type`? Your ONT uses older SSH algorithms. Use the command in [Troubleshooting](#-troubleshooting).

### 2 · Store the ONT password safely

Pick **one**:

<details open>
<summary><b>🍎 macOS: Keychain (recommended)</b></summary>

```bash
security add-generic-password -s ont-mcp -a admin -w
```
It asks for the password and stores it encrypted. Then use `"ONT_KEYCHAIN": "ont-mcp"` below.
</details>

<details>
<summary><b>🐧 Linux: Secret Service (GNOME Keyring / KWallet)</b></summary>

```bash
secret-tool store --label="ONT MCP" service ont-mcp user admin
```
Then use `"ONT_KEYCHAIN": "ont-mcp"` below.
</details>

<details>
<summary><b>📄 Any OS: a password file</b></summary>

```bash
mkdir -p ~/.config/ont-mcp
printf '%s' 'YOUR-ONT-PASSWORD' > ~/.config/ont-mcp/ont.password
chmod 600 ~/.config/ont-mcp/ont.password
```
Then use `"ONT_PASSWORD_FILE": "~/.config/ont-mcp/ont.password"` below.
</details>

### 3 · Add it to your AI app

You need Python 3.9+ (already on macOS and Linux). [`uv`](https://docs.astral.sh/uv/) is the easiest way to run it, with no clone and no install:

<details open>
<summary><b>Claude Desktop</b></summary>

Settings → Developer → Edit Config, then add:

```json
{
  "mcpServers": {
    "ont": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Santhosh17/ont-mcp", "ont-mcp"],
      "env": {
        "ONT_HOST": "YOUR-ONT-IP",
        "ONT_KEYCHAIN": "ont-mcp"
      }
    }
  }
}
```
Restart Claude Desktop.
</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add ont --scope user \
  -e ONT_HOST=YOUR-ONT-IP -e ONT_KEYCHAIN=ont-mcp \
  -- uvx --from git+https://github.com/Santhosh17/ont-mcp ont-mcp
```
</details>

<details>
<summary><b>Cursor</b> (<code>~/.cursor/mcp.json</code>) and <b>Windsurf</b></summary>

Same JSON as Claude Desktop, under `"mcpServers"`.
</details>

<details>
<summary><b>VS Code</b> (<code>.vscode/mcp.json</code>)</summary>

```json
{
  "servers": {
    "ont": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Santhosh17/ont-mcp", "ont-mcp"],
      "env": { "ONT_HOST": "YOUR-ONT-IP", "ONT_KEYCHAIN": "ont-mcp" }
    }
  }
}
```
</details>

<details>
<summary><b>LM Studio</b> (local models, <code>~/.lmstudio/mcp.json</code>)</summary>

Program → Install → Edit mcp.json:

```json
{
  "mcpServers": {
    "ont": {
      "command": "python3",
      "args": ["/full/path/to/ont-mcp/ont_mcp.py"],
      "env": { "ONT_HOST": "YOUR-ONT-IP", "ONT_KEYCHAIN": "ont-mcp" }
    }
  }
}
```
Desktop apps often don't see `uvx` on their PATH, so this uses a clone and `python3`. See [Run it 100% locally](#-run-it-100-locally).
</details>

<details>
<summary><b>No uv? Use plain Python</b></summary>

```bash
git clone https://github.com/Santhosh17/ont-mcp
```
Then use `"command": "python3"` and `"args": ["/full/path/to/ont-mcp/ont_mcp.py"]`.
(On Windows: `"command": "python"`.)
</details>

### ✅ Check it

```bash
ONT_HOST=YOUR-ONT-IP ONT_KEYCHAIN=ont-mcp uvx --from git+https://github.com/Santhosh17/ont-mcp ont-mcp --check
```

```text
• ont  admin@YOUR-ONT-IP:22
  ✓ logged in. CLI commands: backup, config, debug, exit, help, reboot, restore, show
```

That's it. Ask your assistant *"What's the status of my ONT?"* 🎉

## 💬 What you can ask

| Ask | What happens |
|---|---|
| *"Is my fibre connection up? What's the WAN IP?"* | reads `status` and `wan` |
| *"Which devices are on my network right now?"* | reads `arp` and `dhcp` |
| *"Is Telnet open on my ONT? It shouldn't be."* | reads `remoteaccess`, then offers to close it (you approve) |
| *"Forward port 8080 to my home server at 192.168.1.50"* | explores `config portforward ?`, proposes the command, **asks you** |
| *"What firmware am I running?"* | reads `status` |
| *"Explain my ONT's routing table like I'm five"* | reads `route` and explains it |
| *"Reboot the ONT"* | dialog: you must type **REBOOT** |

## 🏡 Run it 100% locally

Prefer your ONT's data never leaves the house? MCP works with any model, so pair this server with a local one and **no question, answer or ONT output goes to the cloud.**

- **Apps:** [LM Studio](https://lmstudio.ai) (config in [Quick start](#3--add-it-to-your-ai-app)), or any MCP app that runs local models: Jan, Goose, Cline, Continue. Open WebUI needs an HTTP bridge such as `mcpo`.
- **Models:** pick one that's good at tool calling, ideally **14B or larger** (Qwen 3, Mistral Small, gpt-oss-20b). Tested with **Qwen3.8 27B in LM Studio**: it found the right tools by itself and checked settings before proposing changes. Models under ~7B often call the wrong tool.
- **Safety doesn't depend on the model.** Command checks and the approval dialog run in the server, so even a confused model can't change anything without you. Trying a small model? Add `"ONT_READ_ONLY": "1"` for extra peace of mind.

## 🛡️ Safety: built in, not bolted on

Your ONT is the only thing between your home and the internet. This server treats it that way.

```mermaid
flowchart LR
    A[AI asks to run a command] --> B{What kind?}
    B -->|show … / help ?| C[✅ Runs right away]
    B -->|not a real command<br/>on this ONT| D[⛔ Refused, with the list<br/>of real commands]
    B -->|a change| E[🪟 Dialog on YOUR screen]
    E -->|you type YES / the keyword| F[✅ Runs]
    E -->|cancel, wrong word,<br/>timeout or no screen| G[⛔ Not run]
```

- **Human approval for every change.** The dialog shows the ONT, the exact command, the risk and the AI's reason. The AI cannot see or answer it.
- **Stronger lock on dangerous commands.** For `reboot`, `reset`, `restore`, firmware, WAN/LAN/PPP/VLAN, accounts and remote access, you must type the word itself (`REBOOT`, `WAN`, …), not just `YES`.
- **Read-only mode.** Set `"read_only": true` (or `ONT_READ_ONLY=1`) and changes are off completely. Great for a parent's ONT.
- **Only real commands.** Anything your ONT's CLI doesn't list is refused before it reaches the box.
- **One command per call.** Line breaks and control characters are rejected, so nothing can sneak in a second command.
- **No dialog, no change.** On a headless server, changes are simply refused.

Approval dialogs use AppleScript on macOS, PowerShell on Windows, and `zenity` or `kdialog` on Linux.

## ⚙️ Configuration

Your ONT details are **never** in this repo. Give them to the server in one of two ways.

### One ONT: environment variables

Put these in the `env` block of your AI app's config:

| Variable | Default | What it is |
|---|---|---|
| `ONT_HOST` | **required** | ONT IP address, the address of your ONT on your LAN, see [step 1](#1--turn-on-ssh-on-your-ont) |
| `ONT_USER` | `admin` | SSH username |
| `ONT_PORT` | `22` | SSH port |
| `ONT_KEYCHAIN` | | Keychain (macOS) / Secret Service (Linux) entry name |
| `ONT_PASSWORD_FILE` | | Path to a file holding only the password |
| `ONT_PASSWORD` | | The password itself (easiest, least safe) |
| `ONT_LABEL` | `Fibre ONT` | Friendly name shown in approval dialogs |
| `ONT_READ_ONLY` | off | `1` turns off all changes |
| `ONT_PROMPT` | `> or #` | Regex for the CLI prompt, only if yours is unusual |

### Several ONTs: a JSON file

Copy [`ont.example.json`](ont.example.json) to `~/.config/ont-mcp/ont.json` (found automatically) or anywhere you like and point `ONT_CONFIG` at it:

```json
{
  "default": "home",
  "devices": {
    "home":   { "host": "192.168.1.1",  "keychain": "ont-home", "label": "Home ONT" },
    "office": { "host": "192.168.10.1", "password_file": "~/.config/ont-mcp/office.password",
                "read_only": true }
  }
}
```

Device fields: `host`, `user`, `port`, `label`, `keychain`, `password_file`, `password`, `read_only`, `prompt`. A typo such as `pasword` is reported clearly instead of failing silently.

The tools then take a `device` argument: *"check the office ONT"*.

## 🧰 Tools

| Tool | Does | Approval |
|---|---|---|
| `ont_show` | `show <item>`: status, wan, lan, arp, dhcp, route, … | never |
| `ont_help` | asks the CLI what exists under a path (`config wan ?`) | never |
| `ont_run` | any change: `config …`, save, reboot, … | **always** |

## 🩺 Troubleshooting

| You see | Fix |
|---|---|
| `wrong username or password` | Check the password in your keychain/file. The SSH login is usually the same as the web login. |
| `SSH is off on the ONT` | Enable SSH under Remote Access in the ONT web page. |
| `the ONT did not answer` | Wrong IP, or this computer isn't on the ONT's network (for example, it's behind another router). |
| `no matching host key type` when testing by hand | `ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa admin@YOUR-ONT-IP`. The server already does this. |
| `the ONT's SSH key changed` | Expected after a factory reset or firmware update: `ssh-keygen -R YOUR-ONT-IP` |
| `no approval dialog could be shown` | Linux: install `zenity` or `kdialog`. Headless machines can only read, by design. |
| `no ONT configured` | Set `ONT_HOST` in your AI app's `env` block, or create `~/.config/ont-mcp/ont.json`. |
| The first answer takes ~10 s | Normal: on first use it learns your ONT's commands, and sessions are paced to protect the box. |

Still stuck? Run `ont-mcp --check` and paste the output into an issue.

## ❓ FAQ

<details>
<summary><b>Why SSH and not the web page?</b></summary>

The web login has a captcha, and the pages load the ONT's small memory heavily. The SSH CLI is fast, scriptable and gentle.
</details>

<details>
<summary><b>Can the AI change settings without me knowing?</b></summary>

No. Every change waits for a dialog on your screen that only a person can answer. With `read_only`, changes are off completely.
</details>

<details>
<summary><b>Where is my password sent?</b></summary>

Only to your ONT, over SSH, on your local network. The server hands it to `ssh` through a private, one-shot helper. It's never logged, never in tool output, never sent to the AI.
</details>

<details>
<summary><b>Does it work with optical power (Rx/Tx dBm) readings?</b></summary>

Only if your ONT's CLI exposes them. Many firmwares show them only in the web page. Ask your assistant to run `ont_help` and see what your box offers.
</details>

<details>
<summary><b>Is this made by Digisol or another ONT maker?</b></summary>

No. It's an independent open-source project, not affiliated with or endorsed by any ONT manufacturer. Digisol and other product names are trademarks of their owners, used here only to say which hardware has been tested.
</details>

## 🤝 Contributing

Device reports, bug fixes and docs are all welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
python3 -m unittest -v      # offline tests, no ONT needed (includes a fake ONT over a real SSH flow)
```

## ⚠️ Disclaimer

Changing ONT settings can cut your internet, voice or IPTV, and may break your provider's terms. You approve every change; you own the result. Provided under the [MIT License](LICENSE), with no warranty.

<div align="center">

**If this saved you a trip to a captcha-protected web page, a ⭐ helps others find it.**

</div>
