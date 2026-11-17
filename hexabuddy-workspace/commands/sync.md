---
description: Sync session status to your phone on or off (off = prompts and replies never leave this Mac)
argument-hint: on | off
allowed-tools: Bash(python3:*)
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hexabuddy.py" sync $ARGUMENTS` with the Bash tool and show its output.
Off: no session status, prompt or reply leaves this Mac; agent questions and Translate → Buddy still work.
