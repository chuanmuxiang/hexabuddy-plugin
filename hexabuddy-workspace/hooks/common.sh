#!/usr/bin/env bash
# Shared config + helpers for Hexabuddy hooks.
# Sourced by report-status.sh.
#
# config.py resolves the connection (environment → state.json in the Hexabuddy folder →
# the bundled connection.json) and the desktop key (login Keychain). Not connected → the
# hooks do nothing.

set -uo pipefail
umask 077  # logs and temporary files: owner-only

_config="$(python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.py" shell)" || return 1
{ IFS= read -r HEXABUDDY_HOME; IFS= read -r HEXABUDDY_SERVER; IFS= read -r _ca_cert; IFS= read -r HEXABUDDY_API_KEY; } <<<"$_config"
unset _config
[[ -n "$HEXABUDDY_API_KEY" ]] || return 1

# Per-connection CA file; never disable HTTPS verification.
_transport=()
if [[ -n "$_ca_cert" ]]; then
  [[ -f "$_ca_cert" ]] || { echo '[hexabuddy] Configured CA file is missing.' >&2; return 1; }
  _transport=(--cacert "$_ca_cert")
fi

POLL_WAIT=25  # per-request long-poll seconds

# read the entire hook payload (JSON on stdin) once into $PAYLOAD
read_payload() { PAYLOAD="$(cat)"; }

# jq-extract a field from the payload, with a fallback
pj() { jq -r "$1 // empty" <<<"$PAYLOAD" 2>/dev/null; }

# curl helpers ---------------------------------------------------------------
server_curl() {
  # ${arr[@]+...} guards empty-array expansion: macOS ships bash 3.2 where
  # "set -u" + an empty "${_transport[@]}" is a fatal unbound-variable error
  # that silently killed every fire-and-forget event POST.
  # The secret travels through a pipe, not curl's process-list arguments.
  printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' "$HEXABUDDY_API_KEY" | curl ${_transport[@]+"${_transport[@]}"} --config - "$@"
}

server_post() { # path  json-body
  server_curl -s --max-time 10 -X POST "${HEXABUDDY_SERVER}$1" -d "$2"
}
server_post_bg() { # fire-and-forget
  (
    code="$(server_curl -s --max-time 10 -o /dev/null -w '%{http_code}' -X POST "${HEXABUDDY_SERVER}$1" -d "$2")"
    if [[ "$code" == 423 ]]; then
      :  # Monitoring is switched off for this Mac on the phone.
    elif [[ "$code" == 401 || "$code" == 403 ]]; then
      echo '[hexabuddy] Agent Sessions access rejected. Run /hexabuddy-workspace:status; reconnect with /hexabuddy-workspace:connect.' >&2
    elif [[ "$code" != 200 ]]; then
      echo '[hexabuddy] Agent session update was not delivered. Check the server connection.' >&2
    fi
  ) &
}
server_get() { # path  -> body on stdout, http code in $SERVER_CODE
  local out
  out="$(server_curl -s -w '\n%{http_code}' --max-time "$((POLL_WAIT + 8))" "${HEXABUDDY_SERVER}$1")"
  SERVER_CODE="${out##*$'\n'}"
  if [[ "$SERVER_CODE" == 401 || "$SERVER_CODE" == 403 ]]; then
    echo '[hexabuddy] Desktop access rejected. Reconnect or replace the desktop key.' >&2
    printf '{"decision":"expired","error":"desktop_access_rejected"}'
    return 0
  fi
  printf '%s' "${out%$'\n'*}"
}

