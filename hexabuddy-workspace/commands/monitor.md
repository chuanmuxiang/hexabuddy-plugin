---
description: Turn session monitoring on or off on this Mac (off = the hooks send nothing)
argument-hint: on | off
allowed-tools: Bash(python3:*)
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hexabuddy.py" monitor $ARGUMENTS` with the Bash tool and show its output.
Off: no session status, prompt or reply leaves this Mac; agent questions and Translate → Buddy still work.
