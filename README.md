# Hexabuddy plugin for Claude Code and Codex

See your coding sessions on your phone: status, approvals and agent questions in the Hexabuddy app,
plus **Translate → Buddy** for any selected word on your Mac.

## Install

| Client | Command |
|---|---|
| Claude Code | `/plugin marketplace add chuanmuxiang/hexabuddy-plugin` then `/plugin install hexabuddy-workspace@hexabuddy-plugin` |
| Codex | `codex plugin marketplace add chuanmuxiang/hexabuddy-plugin` then install **Hexabuddy** from `/plugins` |

Requirements: macOS, the Hexabuddy app signed in on your phone, Python 3 (Xcode Command Line Tools) and `jq`.
Translate → Buddy needs nothing more: no model key, LLM settings file or Pillow; your Hexabuddy server translates
(it counts against your account's daily translation allowance).

## Use

| Step | Claude Code | Codex |
|---|---|---|
| Connect (one phone scan) | `/hexabuddy-workspace:connect` | ask: "connect Hexabuddy" |
| Check every piece | `/hexabuddy-workspace:status` | ask: "Hexabuddy status" |
| Disconnect and remove everything | `/hexabuddy-workspace:disconnect` | ask: "disconnect Hexabuddy" |

Connect opens a QR page: in the app, **Devices → Scan QR code** and scan it. macOS asks once to
trust the Hexabuddy server certificate, for the server's address only. Connect sets up both Claude Code and Codex when both are installed.

On the phone, each Mac has two switches: **Monitor sessions and approvals** and **Save vocabulary cards**, and a
**Disconnect this Mac** button that works even when the Mac is offline.

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
