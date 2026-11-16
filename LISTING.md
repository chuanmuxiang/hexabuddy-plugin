# Listing draft

| Field | Text |
|---|---|
| Name | Hexabuddy |
| Short description | Your coding sessions on your phone: status, approvals and questions |
| Category | Productivity (Codex) · Developer tools (Claude) |
| Keywords | phone, approvals, sessions, mcp, notifications |

## Long description

Hexabuddy puts Claude Code and Codex on your phone. Connect once with a single QR scan and every session shows its
status in the Hexabuddy app; permission prompts can be approved from the phone while you watch a session, and agents
can ask you questions there through the `hexabuddy-buddy` MCP server. Translate → Buddy sends any selected word on
your Mac to your desk buddy as a flashcard. Per-Mac switches on the phone turn monitoring or vocabulary cards off,
and Disconnect removes everything — on the Mac and on the server.

## Privacy notes

- Sends session status, prompts and replies of monitored sessions to the user's own Hexabuddy server account.
- Approval requests only while the phone app watches that session; the terminal prompt stays available.
- Translate → Buddy sends the selected text to the user's own LLM provider, then the result to their Hexabuddy server.
- No daemon, no background process; hooks run only on agent events.
- Credentials: one desktop key in the macOS login Keychain; MCP sign-in held by the coding client.
- Local state: `~/Library/Application Support/Hexabuddy` (no secrets). Disconnect revokes all of the Mac's keys on the server, deletes the Keychain key and that folder, and removes the entries it added to Claude Code and Codex.
