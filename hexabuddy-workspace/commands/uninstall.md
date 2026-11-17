---
description: Uninstall Hexabuddy from this Mac: revoke it on the server and remove everything, the plugin included
allowed-tools: Bash(python3:*)
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hexabuddy.py" uninstall` with the Bash tool and show its output.
It revokes this Mac's keys on the server, then removes everything: the plugin itself (Claude Code and Codex), the hooks, `hexabuddy-buddy`, the CA trust, the Quick Action, the Keychain key and the `~/Library/Application Support/Hexabuddy` folder. If it says the Codex app is running, ask the person to quit ChatGPT and run it again.
