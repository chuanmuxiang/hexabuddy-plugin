---
name: hexabuddy-workspace
description: Connect this Mac to the Hexabuddy phone app, show its status, turn session monitoring on or off, or disconnect it. Use when the user asks to connect, check, monitor or disconnect Hexabuddy.
---

# Hexabuddy

| Request | Command (from this skill's folder) |
|---|---|
| connect | `python3 ../../scripts/hexabuddy.py connect` (timeout 10 min; a QR page opens, scan it in the app) |
| status | `python3 ../../scripts/hexabuddy.py status` (read-only) |
| disconnect | `python3 ../../scripts/hexabuddy.py disconnect` |
| monitor on / off | `python3 ../../scripts/hexabuddy.py monitor on` or `… monitor off` (off: the hooks send nothing from this Mac) |

Resolve the path against this SKILL.md's directory. Show the output. Never ask for or print a key.
