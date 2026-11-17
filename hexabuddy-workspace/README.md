# Hexabuddy plugin for Claude Code and Codex

See your coding sessions on your phone: status, approvals and agent questions in the Hexabuddy app,
plus **Translate → Buddy** for any selected word on your Mac.

Requirements: macOS, the Hexabuddy app signed in on your phone, Python 3 (Xcode Command Line Tools) and `jq`.
Translate → Buddy needs nothing more: no model key, LLM settings file or Pillow; your Hexabuddy server translates
(it counts against your account's daily translation allowance).

## Usage

The same steps are in the app: **Devices → How to connect a Mac**, each command with a Copy button.

### Step 1 — Install the plugin

Install it in each agent you use.

**Claude Code**

```
/plugin marketplace add chuanmuxiang/hexabuddy-plugin
```

```
/plugin install hexabuddy-workspace@hexabuddy-plugin
```

**or Codex**, in a terminal, then install **Hexabuddy** from `/plugins`:

```
codex plugin marketplace add chuanmuxiang/hexabuddy-plugin
```

### Step 2 — Connect

Connect from each agent by itself; each one shows a QR code to scan in the app with **Devices → Scan QR code**, and
one agent never waits on another.

**Claude Code**

```
/hexabuddy-workspace:connect
```

**or Codex** (ask):

```
connect Hexabuddy
```

> **Note:** in Codex, once Claude Code is connected (its connect already added `hexabuddy-buddy` to Codex), you can
> instead choose **hexabuddy-buddy → Authenticate** in Codex's MCP settings and scan the QR code the page shows.

macOS asks once to trust the Hexabuddy server certificate, for the server's address only. Connect also installs
**Translate → Buddy**: select text in any native app → right-click → **Services → Translate → Buddy**.

### Step 3 — Check every piece

**Claude Code**

```
/hexabuddy-workspace:status
```

**or Codex** (ask):

```
Hexabuddy status
```

### Step 4 — Disconnect before you uninstall

**Claude Code**

```
/hexabuddy-workspace:disconnect
```

**or Codex** (ask):

```
disconnect Hexabuddy
```

Uninstalling the plugin does **not** do this: Claude Code and Codex run nothing when a plugin is uninstalled, so the
key, hooks, CA trust and Quick Action would stay. Disconnect first, then uninstall. Lost the Mac? On the phone,
**Devices → this Mac → Disconnect this Mac** revokes it even when the Mac is offline.

On the phone, each Mac also has two switches: **Monitor sessions and approvals** and **Save vocabulary cards**.

## What is sent

| Data | When | Where |
|---|---|---|
| Session status, prompts and replies, project name | each agent event, while monitoring is on | your Hexabuddy server account |
| Permission requests | only while the phone app watches that session | same |
| Agent questions | when the agent calls `ask_human` | same |
| Selected text | when you choose Translate → Buddy | your Hexabuddy server account: translated there, then shown as a card or spoken; kept in your vocabulary and weekly-report activity |

## What is stored on your Mac

| Item | Holds |
|---|---|
| Login Keychain item `com.hexabuddy.desktop-api-key` (account: server URL) | the desktop key, the only secret |
| `~/Library/Application Support/Hexabuddy/state.json` | server, desktop id, `trust` (what connect added, and the server host the CA is trusted for) |
| `~/Library/Application Support/Hexabuddy/server.pem` | copy of the public server CA |
| `~/Library/Application Support/Hexabuddy/hooks/`, `translate-to-buddy/` | copies Codex and the Quick Action run |
| `~/Library/Application Support/Hexabuddy/` logs | `approve.log` |
| Claude Code / Codex configs | plugin, hooks and their Codex trust records, `hexabuddy-buddy`, `NODE_EXTRA_CA_CERTS` |
| Login Keychain trust, `~/Library/Services` | server CA trusted for SSL to the server host only (if connect added it), Translate → Buddy Quick Action |

Disconnect revokes the key on the server, deletes the Keychain item and the whole folder, and removes the entries it
added to the client configs, the CA trust and the Quick Action. No other file is read or written.

## License

MIT
