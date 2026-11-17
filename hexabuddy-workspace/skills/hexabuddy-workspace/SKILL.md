---
name: hexabuddy-workspace
description: Connect this Mac to the Hexabuddy phone app, show its status, turn sync to the phone on or off, or disconnect it. Use when the user asks to connect, check, sync or disconnect Hexabuddy.
---

# Hexabuddy

| Request | Command (from this skill's folder) |
|---|---|
| connect | `python3 ../../scripts/hexabuddy.py connect` (timeout 10 min; a QR page opens, scan it in the app) |
| status | `python3 ../../scripts/hexabuddy.py status` (read-only) |
| disconnect | `python3 ../../scripts/hexabuddy.py disconnect` |
| sync on / off | `python3 ../../scripts/hexabuddy.py sync on` or `… sync off` (off: prompts and replies never leave this Mac) |

Resolve the path against this SKILL.md's directory. Show the output. Never ask for or print a key.
