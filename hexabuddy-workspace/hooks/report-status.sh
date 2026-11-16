#!/usr/bin/env bash
# Fire-and-forget status reporter for every session event in hooks.json (and the
# Codex hooks connect adds). Reads the hook JSON on stdin, POSTs an event to the
# server, and returns immediately (never blocks the agent).

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$DIR/common.sh" || exit 0

read_payload

session_id="$(pj '.session_id')"
event="$(pj '.hook_event_name')"
tool_name="$(pj '.tool_name')"
cwd="$(pj '.cwd')"
model="$(pj '.model // .data.model')"
# Which agent produced this event (set by the Codex wrapper; unset for Claude Code).
agent="${HEXABUDDY_AGENT:-}"

# Use an actual path, never reverse Claude's lossy transcript-directory encoding:
# separators, underscores and hyphens cannot be distinguished after encoding.
transcript="$(pj '.transcript_path')"
if [[ "$agent" != codex && -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
  cwd="$CLAUDE_PROJECT_DIR"
fi
# Codex sends no .cwd but runs the hook IN the session cwd, so $PWD is the project.
[[ -z "$cwd" ]] && cwd="$PWD"

[[ -z "$session_id" ]] && exit 0

# Git worktrees and subdirectories share the primary checkout's project identity.
# Keep cwd intact for session details; never infer identity from the folder name.
project_root=""
common_dir="$(git -C "$cwd" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
if [[ "$common_dir" == */.git ]]; then
  project_root="${common_dir%/.git}"
else
  project_root="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)"
fi

# Conversation log: carry the session's input + output to the app.
#   UserPromptSubmit → the prompt the user typed (straight from the payload)
#   Stop/SubagentStop → Claude's latest assistant text, read from the transcript
detail=""
tokens="null"
case "$event" in
  UserPromptSubmit)
    detail="$(pj '.prompt')"
    ;;
  Stop|SubagentStop)
    # Prefer the last assistant text straight from the payload when present
    # (agent-neutral). Fall back to parsing the CLAUDE transcript JSONL (Claude's
    # schema) for the text + token totals — guarded to ~/.claude so we never try to
    # parse Codex's differently-shaped rollout file.
    detail="$(pj '.last_assistant_message')"
    if [[ "$transcript" == *"/.claude/"* && -f "$transcript" ]]; then
      [[ -z "$detail" ]] && detail="$(jq -rs '[.[] | select(.type=="assistant") | (.message.content // []) | map(select(.type=="text") | .text) | join("\n") | select(. != "")] | last // empty' "$transcript" 2>/dev/null)"
      # Session token totals, summed across all assistant messages.
      tokens="$(jq -s '[.[] | select(.type=="assistant") | .message.usage // empty] | {input:(map(.input_tokens//0)|add // 0), output:(map(.output_tokens//0)|add // 0), cache:(map((.cache_read_input_tokens//0)+(.cache_creation_input_tokens//0))|add // 0)}' "$transcript" 2>/dev/null)"
      [[ -z "$tokens" ]] && tokens="null"
    fi
    ;;
esac
# Keep events small — cap the carried text.
[[ -n "$detail" ]] && detail="${detail:0:4000}"

body="$(jq -nc --arg event "$event" --arg tool "$tool_name" --arg cwd "$cwd" --arg projectRoot "$project_root" --arg model "$model" --arg detail "$detail" --arg transcript "$transcript" --arg agent "$agent" --argjson tokens "$tokens" '{event:$event} + (if $projectRoot != "" then {projectRoot:$projectRoot} else {} end) + (if $tool != "" then {tool_name:$tool} else {} end) + (if $cwd != "" then {cwd:$cwd} else {} end) + (if $model != "" then {model:$model} else {} end) + (if $detail != "" then {detail:$detail} else {} end) + (if $transcript != "" then {transcript_path:$transcript} else {} end) + (if $agent != "" then {agent:$agent} else {} end) + (if $tokens != null then {tokens:$tokens} else {} end)')"

server_post_bg "/sessions/$session_id/event" "$body"
exit 0
