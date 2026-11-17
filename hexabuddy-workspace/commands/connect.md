---
description: Connect this Mac to Hexabuddy with one phone scan
allowed-tools: Bash(python3:*)
---
Run this with the Bash tool (timeout 600000 ms) and show its output:

`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hexabuddy.py" connect`

A pairing page with a QR code opens: scan it in the Hexabuddy app (Devices → Scan QR code). macOS may ask once to trust the Hexabuddy server certificate. Never ask for or print a key.
