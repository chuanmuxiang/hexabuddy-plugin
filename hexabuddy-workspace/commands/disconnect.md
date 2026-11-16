---
description: Disconnect this Mac from Hexabuddy and remove everything
allowed-tools: Bash(python3:*)
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hexabuddy.py" disconnect` with the Bash tool and show its output.
It revokes this Mac's keys on the server, then removes the hooks, `hexabuddy-buddy`, the CA trust, the Quick Action, the Keychain key and the `~/Library/Application Support/Hexabuddy` folder. If it says the Codex app is running, ask the person to quit ChatGPT and run it again.
