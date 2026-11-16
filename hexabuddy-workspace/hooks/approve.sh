#!/usr/bin/env bash
# Remote-approval hook for the official **PermissionRequest** event. This event
# fires ONLY when Claude actually shows a permission dialog — tools the CLI would
# auto-allow never reach here, so there's no allow-list to mirror ourselves.
#
# Behaviour:
#   - If no app is watching the session -> emit nothing, exit 0 -> Claude shows its
#     own native dialog. The CLI is completely unaffected when the app is off.
#   - If an app IS watching -> race the phone (server) against a local keypress on
#     the Mac terminal (1=Yes 2=Always 3=No); first answer wins. No answer in the
#     window -> fall through to the native dialog.
#
# Output contract (PermissionRequest):
#   allow/deny -> {"hookSpecificOutput":{"hookEventName":"PermissionRequest",
#                  "decision":{"behavior":"allow"|"deny"}}}   exit 0
#   fall through to native dialog -> no stdout, exit 0

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$DIR/common.sh" || exit 0

# Diagnostic log (stdout must stay clean for the hook protocol, so log to a file).
# Kept in the Hexabuddy folder (removed by disconnect). Disable with HEXABUDDY_APPROVE_LOG="".
HEXABUDDY_APPROVE_LOG="${HEXABUDDY_APPROVE_LOG-$HEXABUDDY_HOME/approve.log}"
log() { [[ -n "$HEXABUDDY_APPROVE_LOG" ]] && printf '%s approve.sh: %s\n' "$(date '+%H:%M:%S')" "$*" 2>/dev/null >> "$HEXABUDDY_APPROVE_LOG"; }

read_payload
log "FIRED — payload bytes=${#PAYLOAD} server=$HEXABUDDY_SERVER"

session_id="$(pj '.session_id')"
tool_name="$(pj '.tool_name')"
tool_input="$(jq -c '.tool_input // {}' <<<"$PAYLOAD" 2>/dev/null)"
# Claude sets CLAUDE_PROJECT_DIR; Codex sends neither that nor .cwd but runs the
# hook in the project cwd, so fall back to $PWD. (Only used for the Claude-only
# "Always" allow-rule write, which Codex never reaches — canRemember stays false.)
proj="${CLAUDE_PROJECT_DIR:-$(pj '.cwd')}"
[[ -z "$proj" ]] && proj="$PWD"
log "session_id='$session_id' tool_name='$tool_name'"

emit() { # behavior
  jq -nc --arg b "$1" \
    '{hookSpecificOutput:{hookEventName:"PermissionRequest",decision:{behavior:$b}}}'
}

# Deny with a feedback message — used to forward an AskUserQuestion menu choice
# back to Claude (it reads the message and continues as if the user picked it).
emit_deny_msg() { # message
  jq -nc --arg m "$1" \
    '{hookSpecificOutput:{hookEventName:"PermissionRequest",decision:{behavior:"deny",message:$m}}}'
}

[[ -z "$session_id" ]] && { log "no session_id -> exit (native dialog)"; exit 0; }

# Stay dormant unless an app is actively watching this session. Server down or
# nobody watching both read as "not watching" -> native dialog, CLI untouched.
watching="$(server_get "/sessions/$session_id/watching" | jq -r '.watching // false' 2>/dev/null)"
log "watching='$watching' (server_code=${SERVER_CODE:-?})"
[[ "$watching" == "true" ]] || { log "not watching -> exit (native dialog). Open the session's detail view in the app to make it watch."; exit 0; }

# "Always": persist the rule Claude itself suggested (falls back to this exact
# call) so the next identical request is auto-allowed and never reaches us.
remember_rule() {
  local rule f tmp c
  rule="$(jq -r '(.permission_suggestions[]? | select(.behavior=="allow") | .rule) // empty' \
          <<<"$PAYLOAD" 2>/dev/null | head -1)"
  if [[ -z "$rule" ]]; then
    c="$(jq -r '.command // .file_path // empty' <<<"$tool_input" 2>/dev/null)"
    [[ -n "$c" ]] && rule="$tool_name($c)" || rule="$tool_name"
  fi
  f="$proj/.claude/settings.local.json"
  mkdir -p "$(dirname "$f")"; [[ -f "$f" ]] || echo '{}' > "$f"
  tmp="$(mktemp)"
  jq --arg r "$rule" '.permissions.allow = (((.permissions.allow // []) + [$r]) | unique)' \
     "$f" > "$tmp" 2>/dev/null && mv "$tmp" "$f" || rm -f "$tmp"
}

# Does the CLI offer "Yes, and don't ask again" (an allow-rule suggestion)? The
# app uses this to show 2 buttons (Yes/No) vs 3 (Yes/Always/No), matching the CLI.
can_remember=false
jq -e '(.permission_suggestions[]? | select(.behavior=="allow"))' <<<"$PAYLOAD" >/dev/null 2>&1 && can_remember=true
log "can_remember=$can_remember"

# AskUserQuestion carries the menu in tool_input.questions — forward it so the
# app can render the real options instead of a meaningless Yes/No.
questions="$(jq -c '.tool_input.questions // null' <<<"$PAYLOAD" 2>/dev/null)"; [[ -z "$questions" ]] && questions=null
req_body="$(jq -nc --arg t "$tool_name" --argjson ti "$tool_input" --argjson cr "$can_remember" --argjson q "$questions" \
  '{tool_name:$t, tool_input:$ti, can_remember:$cr, questions:$q}')"
resp="$(server_post "/sessions/$session_id/approval-request" "$req_body")"
request_id="$(jq -r '.request_id // empty' <<<"$resp" 2>/dev/null)"
log "posted approval-request -> request_id='$request_id'; waiting (no timeout) for phone or keypress"
[[ -z "$request_id" ]] && { log "no request_id from server (server down?) -> exit (native dialog)"; exit 0; }

# Race the phone (server long-poll) against a local keypress. A single mkdir lock
# makes the first answer win atomically; the loser's write is dropped. The hook is
# short-lived: it returns to the native prompt after at most 80 seconds.
race_dir="$(mktemp -d)"; result="$race_dir/result"
claim() { mkdir "$race_dir/lock" 2>/dev/null && printf '%s %s\n' "$1" "$2" > "$result"; }
pw=$POLL_WAIT
_approval_deadline=$((SECONDS + 80))

( while [[ ! -e "$race_dir/lock" && $SECONDS -lt $_approval_deadline ]]; do
    body="$(server_get "/sessions/$session_id/approval/$request_id?wait=$pw")"
    case "$(jq -r '.decision // "timeout"' <<<"$body" 2>/dev/null)" in
      allow)          claim phone allow ;;
      allow_remember) claim phone allow_remember ;;
      deny)           claim phone deny ;;
      answer)         jq -r '.answer // empty' <<<"$body" > "$race_dir/answer" 2>/dev/null; claim phone answer ;;
      timeout)        : ;;            # long-poll lapsed, no answer yet -> keep waiting
      *)              claim phone none ;;  # superseded/expired by a newer event -> stop
    esac
  done ) & phone_pid=$!

mac_pid=""
# The 1/2/3 keypress maps to Yes/Always/No — meaningless for an AskUserQuestion
# menu, so skip the local prompt there (answer on the phone, or let it fall
# through to the native TUI menu).
if [[ "$tool_name" != "AskUserQuestion" ]] && ( : < /dev/tty ) 2>/dev/null; then
  ( printf '\n[hexabuddy] %s — 1=Yes  2=Always (don'\''t ask again)  3=No   (or answer on phone)\n' \
      "$tool_name" > /dev/tty
    while [[ ! -e "$race_dir/lock" && $SECONDS -lt $_approval_deadline ]]; do
      if IFS= read -rsn1 -t 1 k < /dev/tty; then
        case "$k" in
          1) claim mac allow ;;
          2) claim mac allow_remember ;;
          3) claim mac deny ;;
        esac
      fi
    done ) & mac_pid=$!
fi

while [[ ! -s "$result" && $SECONDS -lt $_approval_deadline ]]; do
  kill -0 "$phone_pid" 2>/dev/null && { sleep 0.2; continue; }
  [[ -n "$mac_pid" ]] && kill -0 "$mac_pid" 2>/dev/null && { sleep 0.2; continue; }
  break
done
kill "$phone_pid" ${mac_pid:+$mac_pid} 2>/dev/null; wait 2>/dev/null

src="" decision=""
read -r src decision 2>/dev/null < "$result"
answer_text=""
[[ "$decision" == "answer" ]] && answer_text="$(cat "$race_dir/answer" 2>/dev/null)"
log "resolved decision='${decision:-none}' src='${src:-none}'${answer_text:+ answer='$answer_text'}"
rm -rf "$race_dir"

[[ -n "$mac_pid" ]] && printf '[hexabuddy] resolved: %s (%s)\n' "${decision:-timeout}" "${src:-}" > /dev/tty 2>/dev/null

# A decision made on the Mac (or no decision) must also clear the phone's approval card.
# Phone decisions are already recorded by the server.
cancel_on_phone() {
  server_post "/sessions/$session_id/approval-cancel" "$(jq -nc --arg id "$request_id" '{request_id:$id}')" >/dev/null 2>&1
}
[[ "$src" == "mac" ]] && cancel_on_phone
case "$decision" in
  allow)          emit allow; exit 0 ;;
  allow_remember) remember_rule; emit allow; exit 0 ;;
  deny)           emit deny; exit 0 ;;
  answer)  # menu choice from the phone -> deny the tool, feed the choice to Claude
      emit_deny_msg "The user answered via Hexabuddy: ${answer_text}"; exit 0 ;;
  *)  # no answer -> clear the phone card, fall back to the native dialog/TUI menu
      cancel_on_phone
      log "no answer -> cleared the phone card; native dialog"
      exit 0 ;;
esac
