# Hexabuddy plugin for Claude Code and Codex

See your coding sessions on your phone: status and agent questions in the Hexabuddy app,
plus **Translate → Buddy** for any selected word on your Mac.

| Requirement | |
|---|---|
| Mac | macOS, Python 3 (Xcode Command Line Tools), `jq` |
| Phone | the Hexabuddy app, signed in |
| Translate → Buddy | nothing more: no model key, LLM settings or Pillow; your Hexabuddy server translates (counts against your daily translation allowance) |

## How it works

```text
  ┌─── MAC: AGENT ──────────────┐   ┌─── SERVER ────────────┐   ┌─── APP / BUDDY ─────────┐
  │ ① SESSION STATUS (hooks)    │   │                       │   │                         │
  │ event: start·prompt·tool·   │   │                       │   │                         │
  │ stop                        │   │                       │   │                         │
  │ report-status.sh ───────────┼──▶│ /sessions/<id>/event ─┼──▶│ Phone: Workspace feed   │
  │ (prompt·reply·project)      │   │ → session             │   │ Buddy: session badge    │
  ├─────────────────────────────┤   ├───────────────────────┤   ├─────────────────────────┤
  │ ② ASK AND WAIT (agent MCP)  │   │                       │   │                         │
  │ ask_human("delete?") ───────┼──▶│ /ask holds ≤ 50 s ────┼──▶│ Phone: question card    │
  │ agent blocks,               │   │                       │   │                         │
  │ get_human_answer…           │   │                       │   │                         │
  │ agent continues ◀───────────┼───┤ answer ◀──────────────┼───┤ tap Yes/No → Send       │
  ├─────────────────────────────┤   ├───────────────────────┤   ├─────────────────────────┤
  │ ③ TRANSLATE → BUDDY         │   │                       │   │                         │
  │ select → right-click ───────┼──▶│ translate·card·say ───┼──▶│ Buddy: card / speech    │
  │                             │   │ · vocab               │   │ Phone: Vocab tab        │
  ├─────────────────────────────┤   ├───────────────────────┤   ├─────────────────────────┤
  │ ④ PERMISSION PROMPTS        │   │                       │   │                         │
  │ → terminal only             │   │ ✗ nothing sent        │   │ ✗ no approval cards     │
  │ (auto mode: none)           │   │                       │   │                         │
  └─────────────────────────────┘   └───────────────────────┘   └─────────────────────────┘
```

## What is stored on your Mac

| Item | Holds |
|---|---|
| Login Keychain item `com.hexabuddy.desktop-api-key` (account: server URL) | the desktop key, the only secret |
| `~/Library/Application Support/Hexabuddy/state.json` | server, desktop id, `trust` (what connect added, and the server host the CA is trusted for) |
| `~/Library/Application Support/Hexabuddy/server.pem` | copy of the public server CA |
| `~/Library/Application Support/Hexabuddy/hooks/`, `translate-to-buddy/` | copies Codex and the Quick Action run |
| Claude Code / Codex configs | plugin, hooks and their Codex trust records, `hexabuddy-buddy`, `NODE_EXTRA_CA_CERTS` |
| Login Keychain trust, `~/Library/Services` | server CA trusted for SSL to the server host only (if connect added it), Translate → Buddy Quick Action |

## Install

The commands are also in the app, each with a Copy button: **Devices → How to connect a Mac**.

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

Connect from each agent by itself.

**Claude Code**

```
/hexabuddy-workspace:connect
```

**or Codex** (ask):

```
connect Hexabuddy
```

It shows a QR code: scan it in the app with **Devices → Scan QR code**.

**or Codex, if Claude Code is already connected**: in Codex's MCP settings choose **hexabuddy-buddy → Authenticate**, then
scan the QR code.

**Codex only, after connecting**: open `/hooks` in Codex and trust the 6 Hexabuddy hooks, or Codex sessions won't
show on the phone.

### Step 3 — Uninstall (removes everything)

**Claude Code**

```
/hexabuddy-workspace:uninstall
```

**or Codex** (ask):

```
uninstall Hexabuddy
```

## Tools

### Status (check every piece)

Shows what is connected and what is missing.

**Claude Code**

```
/hexabuddy-workspace:status
```

**or Codex** (ask):

```
Hexabuddy status
```

### Sync to phone (off / on)

For privacy: off keeps your prompts and the agent's replies on your Mac; on shows your sessions on the phone.


**Claude Code**

```
/hexabuddy-workspace:sync off
```

```
/hexabuddy-workspace:sync on
```

**or Codex** (ask):

```
Hexabuddy sync off
```

```
Hexabuddy sync on
```

### Translate → Buddy (selected text to your buddy)

A word becomes a card on your buddy and in the app's Vocab tab; longer text is spoken.

**Any Mac app**: select text → right-click → **Services → Translate → Buddy** (a hotkey can be bound in System Settings →
Keyboard → Keyboard Shortcuts → Services → Text).

## Notice

- **Server certificate:** macOS asks once to trust the Hexabuddy server certificate, for the server's address only.
- **Permission prompts are not sent to the phone**, because the plugin runs no extra process on your Mac (today an
  agent can hand them only to a local process). They stay in the terminal until an agent can send them over MCP.

## License

MIT
