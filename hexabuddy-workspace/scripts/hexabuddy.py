#!/usr/bin/env python3
"""Hexabuddy on this Mac: connect · disconnect · status. No daemon, no local MCP bridge.
Run by the plugin's commands (/hexabuddy-workspace:connect …) or directly: python3 scripts/hexabuddy.py connect"""
import argparse
import getpass
import hashlib
import html
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # the Xcode Command Line Tools ship Python 3.9
    tomllib = None

PLUGIN = Path(__file__).resolve().parents[1]  # the plugin root: hooks/, translate-to-buddy/, connection.json
_config_spec = importlib.util.spec_from_file_location('buddy_config', str(PLUGIN / 'hooks/config.py'))
_config = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_config)
_credentials = _config.credentials

AGENTS = ('claude', 'codex')
MCP_NAME = 'hexabuddy-buddy'
KEY_FORMAT = re.compile(r'hb_desktop_[a-f0-9]{64}')
DESKTOP_ID = re.compile(r'[a-f0-9]{32}')
CODEX_TOOL_TIMEOUT = 600  # s; well above the server's 25 s hold per ask_human / get_human_answer call
CODEX_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SubagentStop')  # status only: permission prompts stay in the terminal


class SetupError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError('Server redirected the request. Configure its final URL before sending credentials.')


# ── paths and state ──
# The plugin owns one folder (config.home()): state.json · server.pem · hooks/ · translate-to-buddy/ · logs.
# Outside it, only the Claude Code and Codex configs are edited.

def state_path():
    return _config.home() / 'state.json'


def codex_home():
    return Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))


def claude_settings_path():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))) / 'settings.json'


def home_ca():
    # The server CA as connect copied it; NODE_EXTRA_CA_CERTS and the hooks use this copy.
    return _config.home() / 'server.pem'


def quick_action_path():
    return Path.home() / 'Library/Services/Translate to Buddy.workflow'


def login_keychain():
    return Path.home() / 'Library/Keychains/login.keychain-db'


def found_agents():
    # Every supported coding client installed on this Mac.
    return [agent for agent in AGENTS if shutil.which(agent)]


def read_settings(path):
    # The JSON object in path, or {} when it does not exist.
    if not path.exists():
        return {}
    settings = json.loads(path.read_text())
    if not isinstance(settings, dict):
        raise SetupError(f'{path} must contain a JSON object.')
    return settings


def backup(path):
    # Owner-only timestamped copy next to the file.
    copy = path.with_name(path.name + '.bak.' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    shutil.copyfile(path, copy)
    copy.chmod(0o600)


def make_home():
    # Owner-only, also when an older run created it with other permissions.
    home = _config.home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    return home


def save_settings(path, settings, *, keep_backup=True):
    # Atomic owner-only write; the previous content of a client config is backed up when it changes.
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and read_settings(path) == settings:
        path.chmod(0o600)
        return
    if path.exists() and keep_backup:
        backup(path)
    fd, tmp = tempfile.mkstemp(prefix='.buddy-settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(settings, file, indent=2)
            file.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def server_url(value):
    # Normalised server address without credentials, query or fragment.
    value = value.strip().rstrip('/')
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise SetupError('Server must be an http(s) URL without credentials, query, or fragment.')
    return value


def secure_key_url(value):
    # Desktop keys travel only over HTTPS or loopback.
    if not _config.secure(server_url(value)):
        raise SetupError('Desktop API keys require HTTPS. Use a trusted HTTPS address or a local SSH tunnel.')


def server_and_ca(args, state):
    # --server, else the release profile (connection.json), else the saved state; the CA follows the server.
    profile = _config.bundled(PLUGIN)
    server = server_url(args.server or profile.get('server') or state.get('server') or '')
    if args.ca_cert:
        ca_cert = args.ca_cert
    elif server == profile.get('server') and profile.get('caCertificate'):
        ca_cert = str(PLUGIN / profile['caCertificate'])
    elif server == state.get('server') and home_ca().is_file():
        ca_cert = str(home_ca())
    else:
        ca_cert = None
    if ca_cert:
        ca_cert = str(Path(ca_cert).expanduser().resolve())
        if not Path(ca_cert).is_file():
            raise SetupError('Configured CA certificate file is missing.')
    return server, ca_cert


# ── local commands ──

def execute(command, env=None):
    # (exit code, stdout) of a captured command.
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=60)
    return result.returncode, result.stdout


def run(command, env=None, *, check=True):
    # stdout of a captured command; its output may hold credentials, so failures print only the command.
    code, output = execute(command, env)
    if code and check:
        raise SetupError(f'{command[0]} {command[1]} failed (exit {code}). Retry after checking the agent CLI.')
    return output if code == 0 else ''


def interactive_command(command):
    # `/hexabuddy-workspace:connect` runs this script through the agent's shell tool, which has no terminal, and
    # `claude mcp login` refuses to sign in without one ("stdin isn't a terminal"). Give such commands a
    # pseudo-terminal with macOS `script`; the sign-in itself needs no typing (this Mac's key approves it).
    if sys.stdin.isatty() or not shutil.which('script'):
        return command
    return ['script', '-q', '/dev/null', *command]


def run_interactive(command, env=None):
    # A command the person sees: native sign-in pages and macOS trust prompts.
    if subprocess.run(interactive_command(command), env=env, timeout=900).returncode:
        raise SetupError(f'{command[0]} {command[1]} did not finish. Run connect again.')


# ── server ──

def connection_opener(ca_cert=None):
    if ca_cert:
        return build_opener(NoRedirect, HTTPSHandler(context=ssl.create_default_context(cafile=ca_cert)))
    return build_opener(NoRedirect)


def post_json(server, path, body, ca_cert=None, api_key=None):
    # One JSON POST; HTTP and network errors reach the caller.
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    request = Request(server + path, data=json.dumps(body).encode(), headers=headers, method='POST')
    with connection_opener(ca_cert).open(request, timeout=15) as response:
        return json.load(response)


def key_state(server, api_key, ca_cert=None):
    # 'accepted' · 'rejected' · 'unreachable' for GET /sessions with this Mac's key.
    request = Request(server + '/sessions', headers={'Authorization': 'Bearer ' + api_key})
    try:
        with connection_opener(ca_cert).open(request, timeout=10) as response:
            data = json.load(response)
    except HTTPError as error:
        return 'rejected' if error.code in (401, 403) else 'unreachable'
    except (URLError, TimeoutError, OSError, ValueError, SetupError):
        return 'unreachable'
    return 'accepted' if isinstance(data, dict) and isinstance(data.get('sessions'), list) else 'unreachable'


def check_remote_mcp(server, ca_cert=None):
    # The server answers over a trusted connection and publishes MCP metadata for this address.
    try:
        with connection_opener(ca_cert).open(server + '/.well-known/oauth-protected-resource/mcp', timeout=10) as response:
            metadata = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        raise SetupError('Cannot reach the server over a trusted connection. Check its address, certificate and network.') from None
    if metadata.get('resource') != server + '/mcp' or metadata.get('authorization_servers') != [server]:
        raise SetupError('Server MCP metadata does not match this address. Check its public_url.')


def revoke_self(server, api_key, ca_cert=None):
    # POST /desktop/revoke-self → (ok, message); never raises.
    try:
        result = post_json(server, '/desktop/revoke-self', {}, ca_cert, api_key)
    except HTTPError as error:
        reason = 'key already revoked or expired' if error.code == 401 else f'HTTP {error.code}'
        return False, f'server rejected the request ({reason})'
    except (URLError, TimeoutError, OSError, ValueError, SetupError):
        return False, 'server unreachable over a trusted connection'
    scopes = result.get('scopes') if isinstance(result, dict) and isinstance(result.get('scopes'), list) else []
    kinds = sorted({'questions' if scope == 'mcp' else 'hooks' for scope in scopes})
    message = f'revoked {len(scopes)} key(s) of this Mac ({", ".join(kinds) or "none"})'
    if 'mcp' not in scopes:
        message += '; a Questions key set up before keys were linked stays listed on the phone: revoke it there'
    return True, message


# ── pairing and the desktop key ──

@contextmanager
def pairing_window(pair, name):
    # A local page showing the QR (PNG data URL); the redeeming device secret never enters it.
    image = pair.get('qr_png', '')
    if not isinstance(image, str) or not re.fullmatch(r'data:image/png;base64,[A-Za-z0-9+/]+={0,2}', image) or len(image) > 100_000:
        raise SetupError('Server cannot display QR pairing. Update the server first.')
    directory = make_home()
    fd, filename = tempfile.mkstemp(prefix='pair-', suffix='.html', dir=directory)
    path = Path(filename)

    def page(content):
        return ('<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">'
                '<meta http-equiv="refresh" content="3"><title>Connect Hexabuddy</title>'
                '<style>body{font:18px system-ui;color:#17202a;background:#fff;max-width:36rem;margin:7vh auto;padding:24px;text-align:center}img{width:320px;max-width:100%}p{line-height:1.5}</style>'
                + content + '</html>')
    with os.fdopen(fd, 'w') as file:
        file.write(page(f'<h1>Pair {html.escape(name)}</h1><p>On your phone: Hexabuddy app → Devices → Scan QR code.</p>'
                        f'<img alt="Pairing QR code" src="{image}"><p>Scan to connect automatically.</p>'
                        f'<details><summary>Can’t scan?</summary><p>Enter {html.escape(pair["user_code"])} on your phone.</p></details>'
                        '<p>This code expires in 10 minutes.</p>'))
    try:
        if not webbrowser.open(path.as_uri()):
            raise SetupError(f'Open the pairing page: {path}')
        yield
    except BaseException:
        path.write_text(page('<h1>Pairing stopped</h1><p>Open Connect Hexabuddy to try again.</p>'))
        raise
    else:
        path.write_text(page('<h1>Phone approved</h1><p>Your Mac is finishing setup.</p>'))


def pair_desktop(server, name, ca_cert=None, desktop_id=None):
    # QR plus one phone approval → (desktop key, desktop id).
    def post(path, body):
        try:
            return post_json(server, path, body, ca_cert)
        except HTTPError as error:
            messages = {403: 'Connection denied on the phone.', 410: 'Pairing expired or was already used. Run connect again.',
                        429: 'Pairing is rate limited. Wait and retry.', 503: 'Server pairing is not configured. Update the server and its public_url.'}
            raise SetupError(messages.get(error.code, f'Pairing failed (HTTP {error.code}).')) from None
        except (URLError, TimeoutError, OSError):
            raise SetupError('Could not reach pairing over a trusted connection. Check the server certificate and address.') from None
    request = {'name': name}
    if desktop_id:
        request['desktop_id'] = desktop_id
    pair = post('/api/desktop/pair', request)
    if not isinstance(pair.get('device_code'), str) or not re.fullmatch(r'[a-f0-9]{64}', pair['device_code']):
        raise SetupError('Server returned an invalid pairing response.')
    user_code = pair.get('user_code', '')
    if not re.fullmatch(r'[A-F0-9]{5}-[A-F0-9]{5}', user_code):
        raise SetupError('Server returned an invalid pairing code.')
    print('Scan the QR code in the pairing window to connect automatically.', flush=True)
    print(f'Scan the QR code with the Hexabuddy app: Devices → Scan QR code (pairing code {user_code}). If it expires, run connect again.', flush=True)
    deadline = time.monotonic() + min(600, max(1, pair.get('expires_in', 600)))
    with pairing_window(pair, name):
        while time.monotonic() < deadline:
            time.sleep(3)
            answer = post('/api/desktop/pair/poll', {'device_code': pair['device_code']})
            if answer.get('status') == 'approved':
                if not KEY_FORMAT.fullmatch(answer.get('key', '')):
                    raise SetupError('Server returned an invalid desktop key.')
                desktop = answer.get('desktopId')
                return answer['key'], desktop if isinstance(desktop, str) and DESKTOP_ID.fullmatch(desktop) else None
            if answer.get('status') != 'pending':
                raise SetupError('Unexpected pairing state. Start a new connection.')
    raise SetupError('Pairing timed out. Open Connect Hexabuddy again.')


def stored_key(server, state):
    # This Mac's desktop key from the Keychain, or '' when this Mac is not connected to server.
    if not server or state.get('server') != server:
        return ''
    try:
        key = _credentials.keychain(server)
    except RuntimeError:
        return ''
    return key if KEY_FORMAT.fullmatch(key or '') else ''


def obtain_key(args, server, ca_cert, state):
    # Reuse an accepted Keychain key; else pair by QR (or a pasted phone key) and store it in the Keychain.
    desktop_id = state.get('desktopId') if DESKTOP_ID.fullmatch(str(state.get('desktopId', ''))) else None
    api_key = stored_key(server, state)
    if api_key and key_state(server, api_key, ca_cert) == 'accepted':
        return api_key, desktop_id
    if args.api_key:
        api_key = getpass.getpass('Desktop API key (hidden): ').strip()
    else:
        api_key, desktop_id = pair_desktop(server, args.name, ca_cert, desktop_id)
    if not KEY_FORMAT.fullmatch(api_key) or key_state(server, api_key, ca_cert) != 'accepted':
        raise SetupError('The server did not accept the desktop key.')
    try:
        _credentials.keychain(server, api_key)
    except RuntimeError as error:
        # BUG-033: the phone approved a pairing this Mac cannot keep; revoke it so the phone lists nothing new.
        revoked, _ = revoke_self(server, api_key, ca_cert)
        raise SetupError(f'{error}. ' + ('The new pairing was revoked on the server; run connect again.' if revoked
                         else 'Remove the new Mac on the phone: Devices → this Mac → Disconnect this Mac.')) from None
    return api_key, desktop_id


def forget_key(server):
    try:
        _credentials.keychain(server, delete=True)
        print('✓ Keychain desktop key deleted.')
    except RuntimeError:
        print(f'✗ Keychain desktop key not deleted: remove "{_credentials.SERVICE}" in Keychain Access.')


def forget_local(server):
    # Delete the Keychain desktop key and the Hexabuddy folder (state, CA copy, hooks, logs).
    if server:
        forget_key(server)
    home = _config.home()
    if home.resolve() in (Path.home().resolve(), Path(home.anchor)):
        raise SetupError('HEXABUDDY_HOME must name a folder of its own.')
    if home.exists():
        shutil.rmtree(home)
        print(f'✓ {home} removed.')


# ── server CA trust (same for both clients) ──

def certificate_sha1(ca_cert):
    # SHA-1 of the DER certificate, as `security delete-certificate -Z` expects.
    return hashlib.sha1(ssl.PEM_cert_to_DER_cert(Path(ca_cert).read_text())).hexdigest().upper()


def install_ca(ca_cert):
    # Copy the server CA into the Hexabuddy folder; that copy is the one every client uses.
    if not ca_cert:
        home_ca().unlink(missing_ok=True)
        return None
    make_home()
    if Path(ca_cert).resolve() != home_ca().resolve():
        shutil.copyfile(ca_cert, home_ca())
    home_ca().chmod(0o644)
    return str(home_ca())


def server_certificate(server):
    # PEM of the certificate the server presents; fetch only, `security verify-cert` does the verification.
    url = urlsplit(server)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((url.hostname, url.port or 443), timeout=10) as raw, \
         context.wrap_socket(raw, server_hostname=url.hostname) as tls:
        return ssl.DER_cert_to_PEM_cert(tls.getpeercert(binary_form=True))


def server_trusted(server):
    # Whether macOS already trusts the server's certificate for SSL to its host (the check a client makes).
    host = urlsplit(server).hostname
    try:
        leaf = server_certificate(server)
    except (OSError, ValueError):
        return False
    with tempfile.NamedTemporaryFile('w', suffix='.pem') as file:
        file.write(leaf)
        file.flush()
        code, _ = execute(['security', 'verify-cert', '-c', file.name, '-p', 'ssl', '-s', host])
    return code == 0


def trust_server_ca(ca_cert, state):
    # Login Keychain trusts the CA for SSL to the server host only (both clients); Claude Code also gets
    # NODE_EXTRA_CA_CERTS. Recorded in state trust.
    if not ca_cert:
        return
    trust = dict(state.get('trust') or {})
    host = urlsplit(state['server']).hostname
    if trust.get('keychain') and not trust.get('host'):
        # An earlier connect trusted the CA for SSL to every host: replace it with the host-scoped setting.
        run(['security', 'delete-certificate', '-t', '-Z', trust.pop('keychain'), str(login_keychain())], check=False)
    if urlsplit(state['server']).scheme == 'https' and not server_trusted(state['server']):
        print(f'macOS asks once to trust the Hexabuddy server certificate for {host}.', flush=True)
        run_interactive(['security', 'add-trusted-cert', '-r', 'trustRoot', '-p', 'ssl', '-s', host,
                         '-k', str(login_keychain()), ca_cert])
        trust.update(keychain=certificate_sha1(ca_cert), host=host)
    path = claude_settings_path()
    claude = read_settings(path)
    env = dict(claude.get('env') or {})
    current = env.get('NODE_EXTRA_CA_CERTS')
    if current is None:
        env['NODE_EXTRA_CA_CERTS'] = ca_cert
        save_settings(path, dict(claude, env=env))
        trust['claudeEnv'] = ca_cert
    elif current != ca_cert:
        print(f'! Claude Code already uses NODE_EXTRA_CA_CERTS={current}; append the Hexabuddy CA to that file.')
    state['trust'] = trust


def remove_ca_trust(state):
    # Undo only the trust that connect added: the CA certificate and its trust settings (host-scoped or older global).
    trust = state.get('trust') or {}
    if trust.get('keychain'):
        run(['security', 'delete-certificate', '-t', '-Z', trust['keychain'], str(login_keychain())], check=False)
        print(f"✓ Server certificate trust{' for ' + trust['host'] if trust.get('host') else ''} removed from the login Keychain.")
    path = claude_settings_path()
    claude = read_settings(path)
    env = dict(claude.get('env') or {})
    if trust.get('claudeEnv') and env.get('NODE_EXTRA_CA_CERTS') == trust['claudeEnv']:
        del env['NODE_EXTRA_CA_CERTS']
        claude['env'] = env
        if not env:
            del claude['env']
        save_settings(path, claude)
        print('✓ NODE_EXTRA_CA_CERTS removed from Claude Code settings.')


# ── Claude Code ──

def claude_plugins():
    return json.loads(run(['claude', 'plugin', 'list', '--json']))


def install_claude():
    # Enable the hexabuddy-workspace plugin (hooks + commands); install it from this marketplace when it is missing.
    plugin = next((item for item in claude_plugins() if str(item.get('id', '')) == 'hexabuddy-workspace@hexabuddy-plugin'), None)
    if plugin is None:
        marketplace = next((root for root in (PLUGIN.parent, PLUGIN.parents[1]) if (root / '.claude-plugin/marketplace.json').exists()), None)
        if marketplace is None:
            raise SetupError('Install the plugin first: /plugin install hexabuddy-workspace@hexabuddy-plugin in Claude Code.')
        run(['claude', 'plugin', 'marketplace', 'add', str(marketplace)], check=False)
        run(['claude', 'plugin', 'install', 'hexabuddy-workspace@hexabuddy-plugin'])
    elif not plugin.get('enabled'):
        run(['claude', 'plugin', 'enable', plugin['id']])
    print('✓ Claude Code plugin enabled.')


def remove_claude():
    # Plugin, marketplace, MCP sign-in and MCP entry.
    for plugin in claude_plugins():
        if str(plugin.get('id', '')).startswith('hexabuddy-workspace@'):
            run(['claude', 'plugin', 'uninstall', plugin['id']], check=False)
    run(['claude', 'plugin', 'marketplace', 'remove', 'hexabuddy-plugin'], check=False)
    run(['claude', 'mcp', 'logout', MCP_NAME], check=False)
    run(['claude', 'mcp', 'remove', '--scope', 'user', MCP_NAME], check=False)
    print('✓ Claude Code: plugin and hexabuddy-buddy removed.')


# ── Codex ──

def is_our_hook(hook):
    # Our Codex hooks run `env HEXABUDDY_AGENT=codex <dir>/hooks/report-status.sh`, wherever <dir> was; approve.sh is
    # still recognised so connect and disconnect remove it from Macs set up before phone approvals were removed.
    try:
        command = shlex.split(hook.get('command', ''))
    except ValueError:
        return False
    if len(command) != 3 or command[:2] != ['env', 'HEXABUDDY_AGENT=codex']:
        return False
    script = Path(command[2])
    return script.name in ('report-status.sh', 'approve.sh') and script.parent.name == 'hooks'


def codex_hook_ids(hooks_path):
    # Codex trust-record ids ("<hooks.json>:<snake_event>:<group>:<hook>") of our hooks in hooks.json.
    ids = []
    for event, groups in read_settings(hooks_path).get('hooks', {}).items():
        snake = re.sub(r'(?<!^)(?=[A-Z])', '_', event).lower()
        for g, group in enumerate(groups):
            for h, hook in enumerate(group.get('hooks', [])):
                if is_our_hook(hook):
                    ids.append(f'{hooks_path}:{snake}:{g}:{h}')
    return ids


def configure_codex_hooks(*, remove=False):
    # Rewrite hooks.json with our hooks removed (re-added unless remove); other hooks stay. Returns the removed ids.
    path = codex_home() / 'hooks.json'
    settings = read_settings(path)
    removed = codex_hook_ids(path)
    hooks = {}
    for event, groups in settings.get('hooks', {}).items():
        kept = [dict(group, hooks=[h for h in group.get('hooks', []) if not is_our_hook(h)]) for group in groups]
        kept = [group for group in kept if group['hooks']]
        if kept:
            hooks[event] = kept
    if not remove:
        for event in CODEX_EVENTS:
            hooks.setdefault(event, []).append(codex_hook_group(event))
    if not remove or path.exists():
        save_settings(path, dict(settings, hooks=hooks))
    if not remove:
        remove_codex_trust_records(set())   # stale records of the plugin's own hooks file (BUG-037)
    return removed


def codex_hook_group(event):
    # Our hook group for one Codex event.
    script = _config.home() / 'hooks' / 'report-status.sh'
    group = {'hooks': [{'type': 'command', 'command': f'env HEXABUDDY_AGENT=codex {shlex.quote(str(script))}'}]}
    if event in ('PreToolUse', 'PostToolUse'):
        group['matcher'] = '.*'
    return group


TRUST_HEADER = re.compile(r'\[\s*hooks\.state\.(?:"((?:[^"\\]|\\.)*)"|\'([^\']*)\')\s*\]')


def trust_record_id(line):
    # The id in a `[hooks.state."<id>"]` header line, else None.
    if not line.strip().startswith('[hooks.state.'):
        return None
    if tomllib is None:
        match = TRUST_HEADER.fullmatch(line.strip())
        if not match:
            return None
        return match.group(2) if match.group(1) is None else re.sub(r'\\(["\\])', r'\1', match.group(1))
    try:
        state = tomllib.loads(line.strip()).get('hooks', {}).get('state', {})
    except tomllib.TOMLDecodeError:
        return None
    return next(iter(state)) if len(state) == 1 else None


def codex_trust_records():
    # Ids of every Codex hook trust record in config.toml.
    path = codex_home() / 'config.toml'
    if not path.exists():
        return set()
    if tomllib is None:
        return {record for record in map(trust_record_id, path.read_text().splitlines()) if record}
    try:
        return set(tomllib.loads(path.read_text()).get('hooks', {}).get('state', {}))
    except tomllib.TOMLDecodeError:
        return set()


def set_codex_tool_timeout():
    # Ask and wait (server.md §4.8): one ask_human call holds up to 25 s; Codex's default tool timeout is 60 s. Set
    # tool_timeout_sec in the hexabuddy-buddy table (backup first, must parse); nothing else in config.toml changes.
    path = codex_home() / 'config.toml'
    if not path.exists():
        return
    lines, out, inside, done = path.read_text().splitlines(keepends=True), [], False, False
    for line in lines:
        if line.lstrip().startswith('['):
            if inside and not done:
                at = len(out)
                while at and not out[at - 1].strip():   # directly under the table's keys, before its blank lines
                    at -= 1
                out.insert(at, f'tool_timeout_sec = {CODEX_TOOL_TIMEOUT}\n'); done = True
            inside = re.match(r'\s*\[mcp_servers\.' + re.escape(MCP_NAME) + r'\]', line) is not None
        elif inside and re.match(r'\s*tool_timeout_sec\s*=', line):
            line, done = f'tool_timeout_sec = {CODEX_TOOL_TIMEOUT}\n', True
        out.append(line)
    if inside and not done:
        if out and not out[-1].endswith('\n'):
            out[-1] += '\n'
        out.append(f'tool_timeout_sec = {CODEX_TOOL_TIMEOUT}\n')
    text = ''.join(out)
    if text == ''.join(lines):
        return
    if tomllib:
        tomllib.loads(text)  # never write a config Codex cannot parse
    backup(path)
    path.write_text(text)


def add_codex_entry_quietly(server, desktop_id):
    # Write the hexabuddy-buddy table into Codex's config.toml directly: `codex mcp add` starts an OAuth sign-in and
    # waits on it. Same keys `codex mcp add --url --oauth-resource --oauth-client-id` writes; backup first, must parse.
    path, url = codex_home() / 'config.toml', server + '/mcp'
    text = path.read_text() if path.exists() else ''
    kept, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith('['):
            skipping = re.match(r'\s*\[mcp_servers\.' + re.escape(MCP_NAME) + r'(\.|\])', line) is not None
        if not skipping:
            kept.append(line)
    body = ''.join(kept)
    if body and not body.endswith('\n'):
        body += '\n'
    body += (f'\n[mcp_servers.{MCP_NAME}]\nurl = {json.dumps(url)}\noauth_resource = {json.dumps(url)}\n\n'
             f'tool_timeout_sec = {CODEX_TOOL_TIMEOUT}\n\n[mcp_servers.{MCP_NAME}.oauth]\nclient_id = {json.dumps(mcp_client_id("codex", desktop_id))}\n')
    if body == text:
        return
    if tomllib:
        tomllib.loads(body)  # never write a config Codex cannot parse
    if path.exists():
        backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


PLUGIN_HOOK_RECORD = 'hexabuddy-workspace@'   # records of hooks Codex once loaded from the plugin itself (BUG-037)


def remove_codex_trust_records(ids):
    # Drop the trust-record tables of our removed hooks from config.toml (backup first, result must parse), and any
    # record of the plugin's own hooks file, which Codex no longer loads (BUG-037).
    path = codex_home() / 'config.toml'
    if not path.exists() or (not ids and PLUGIN_HOOK_RECORD not in path.read_text()):
        return 0
    kept, skipping, dropped = [], False, 0
    for line in path.read_text().splitlines(keepends=True):
        if line.lstrip().startswith('['):
            record = trust_record_id(line)
            skipping = record in ids or (record or '').startswith(PLUGIN_HOOK_RECORD)
            dropped += skipping
        if not skipping:
            kept.append(line)
    if dropped:
        text = ''.join(kept)
        if tomllib:
            tomllib.loads(text)  # never write a config Codex cannot parse
        backup(path)
        path.write_text(text)
    return dropped


def codex_app_running():
    # The Codex app ships inside ChatGPT.app; its processes name the framework or its user-data folder.
    # Crash-reporter helpers (browser_crashpad_handler) keep running after ChatGPT quits; they never touch the cache, so
    # they do not count (BUG-039: uninstall refused forever after quitting).
    markers = ('Codex Framework.framework', 'Application Support/Codex', '/Codex.app/Contents/MacOS/')
    listing = run(['ps', '-axo', 'command'], check=False)
    return any(any(marker in line for marker in markers) and 'crashpad_handler' not in line for line in listing.splitlines())


def without_mcp_entries(value):
    # Copy of a JSON value without hexabuddy-buddy entries (items whose server/name is it, or keys equal to it).
    if isinstance(value, list):
        return [without_mcp_entries(item) for item in value
                if not (isinstance(item, dict) and MCP_NAME in (item.get('server'), item.get('name')))]
    if isinstance(value, dict):
        return {key: without_mcp_entries(item) for key, item in value.items() if key != MCP_NAME}
    return value


def codex_cache_blocked():
    # The Codex app cache names hexabuddy-buddy and the app is running (it would rewrite the file): uninstall must wait.
    path = codex_home() / '.codex-global-state.json'
    return path.exists() and MCP_NAME in path.read_text() and codex_app_running()


BACKUP_NAME = re.compile(r'.+\.bak\.\d{8}-\d{6}-\d{6}$')   # backup()'s own names
PLUGIN_MARKERS = (MCP_NAME, 'HEXABUDDY_AGENT', 'hexabuddy-workspace')


def remove_leftovers():
    # "Nothing left" (design/desktop.md): the config backups this plugin made that still name it. Other backups and files
    # are never touched.
    removed = 0
    for folder in (codex_home(), claude_settings_path().parent, Path.home()):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.is_file() and BACKUP_NAME.fullmatch(path.name):
                try:
                    text = path.read_text(errors='ignore')
                except OSError:
                    continue
                if any(marker in text for marker in PLUGIN_MARKERS):
                    path.unlink(); removed += 1
    # Claude Code's download cache stays: sessions still running load their hooks from it, and deleting it made every
    # prompt in them fail ("Plugin directory does not exist", BUG-040) until restarted; Claude Code cleans it itself.
    print(f'✓ Leftovers removed: {removed} config backup(s).')


def clear_codex_app_cache():
    # Remove hexabuddy-buddy from the Codex app's cached state. False when the app runs (it would rewrite the file).
    path = codex_home() / '.codex-global-state.json'
    if not path.exists() or MCP_NAME not in path.read_text():
        return True
    if codex_app_running():
        return False
    state = json.loads(path.read_text())
    backup(path)
    path.write_text(json.dumps(without_mcp_entries(state)))
    print('✓ Codex app cache: hexabuddy-buddy entries cleared.')
    return True


def remove_codex():
    # Plugin, marketplace, hooks, their trust records, MCP sign-in and MCP entry.
    removed = configure_codex_hooks(remove=True)
    records = remove_codex_trust_records(set(removed))
    run(['codex', 'mcp', 'logout', MCP_NAME], check=False)
    run(['codex', 'mcp', 'remove', MCP_NAME], check=False)
    # The plugin itself too: disconnect is the one command that removes everything (no separate uninstall).
    run(['codex', 'plugin', 'remove', 'hexabuddy-workspace@hexabuddy-plugin'], check=False)
    run(['codex', 'plugin', 'marketplace', 'remove', 'hexabuddy-plugin'], check=False)
    print(f'✓ Codex: plugin, {len(removed)} hooks, {records} trust records and hexabuddy-buddy removed.')


# ── MCP: the same name, URL, client-id shape and CA trust in both clients ──

def mcp_client_id(agent, desktop_id=None):
    # The server joins an MCP key to this Mac's keys when the fixed client id names its desktop id.
    if desktop_id and DESKTOP_ID.fullmatch(desktop_id):
        return f'hexabuddy-{agent}.{desktop_id}'
    return f'hexabuddy-{agent}'


def client_env(agent, ca_cert=None):
    # Environment for a client subprocess that trusts the server CA for that run.
    env = dict(os.environ)
    if ca_cert:
        env['CODEX_CA_CERTIFICATE' if agent == 'codex' else 'NODE_EXTRA_CA_CERTS'] = ca_cert
    return env


def add_mcp_entry(agent, server, desktop_id, ca_cert):
    # Register hexabuddy-buddy at <server>/mcp, user scope; keep an entry that already matches.
    url = server + '/mcp'
    if agent == 'codex':
        existing = json.loads(run(['codex', 'mcp', 'get', MCP_NAME, '--json'], check=False) or '{}')
        transport = existing.get('transport', {})
        if transport.get('type') == 'streamable_http' and transport.get('url') == url and existing.get('enabled') is not False:
            set_codex_tool_timeout()
            return
        run(['codex', 'mcp', 'remove', MCP_NAME], check=False)
        run_interactive(['codex', 'mcp', 'add', MCP_NAME, '--url', url, '--oauth-resource', url,
                         '--oauth-client-id', mcp_client_id('codex', desktop_id)], client_env('codex', ca_cert))
        set_codex_tool_timeout()
        return
    code, output = execute(['claude', 'mcp', 'get', MCP_NAME])
    if code == 0 and re.search(r'^\s*URL: ' + re.escape(url) + r'\s*$', output, re.M):
        return
    if code == 0:
        run(['claude', 'mcp', 'remove', '--scope', 'user', MCP_NAME])
    run(['claude', 'mcp', 'add', '--scope', 'user', '--transport', 'http',
         '--client-id', mcp_client_id('claude', desktop_id), MCP_NAME, url])


def mcp_state(agent):
    # ('connected' | 'sign-in' | 'failed' | 'missing', detail). Claude is health-checked; Codex reports its sign-in.
    if agent == 'claude':
        code, output = execute(['claude', 'mcp', 'get', MCP_NAME], client_env('claude'))
        if code:
            return 'missing', 'not configured'
        status = next((line.split(':', 1)[1].strip() for line in output.splitlines() if line.strip().startswith('Status:')), '')
        if 'Connected' in status:
            return 'connected', status
        return ('sign-in' if 'auth' in status.lower() else 'failed'), status or 'no status'
    try:
        entries = json.loads(run(['codex', 'mcp', 'list', '--json'], check=False) or '[]')
    except ValueError:
        return 'failed', 'codex mcp list returned invalid JSON'
    entry = next((item for item in entries if isinstance(item, dict) and item.get('name') == MCP_NAME), None)
    if entry is None:
        return 'missing', 'not configured'
    signed_in = str(entry.get('auth_status', '')).replace('_', '').lower() in ('oauth', 'bearertoken')
    return ('connected' if signed_in else 'sign-in'), f'auth: {entry.get("auth_status")}'


@contextmanager
def mac_approval(server, api_key, ca_cert=None):
    # While a client signs in, this Mac's key approves that sign-in (POST /desktop/approve-mcp) every 2 s.
    stop = threading.Event()

    def approve():
        while not stop.wait(2):
            try:
                if post_json(server, '/desktop/approve-mcp', {}, ca_cert, api_key).get('approved'):
                    print('✓ Sign-in approved by this Mac’s paired key.', flush=True)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, SetupError):
                continue
    worker = threading.Thread(target=approve, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=20)


def connect_mcp(agent, server, api_key, desktop_id, ca_cert):
    # Add the entry and finish its OAuth sign-in now; this Mac approves it, so no second phone scan.
    with mac_approval(server, api_key, ca_cert):
        add_mcp_entry(agent, server, desktop_id, ca_cert)
        if mcp_state(agent)[0] != 'connected':
            print(f'Signing {agent} in to {MCP_NAME}: a browser page opens and completes by itself.', flush=True)
            run_interactive([agent, 'mcp', 'login', MCP_NAME], client_env(agent, ca_cert))


# ── Translate → Buddy Quick Action ──

def install_copies():
    # Copy the hooks and the Quick Action script into the Hexabuddy folder, replacing an older copy:
    # Codex hooks and the Quick Action point there, so a plugin update (new cache folder) never breaks them.
    target = make_home()
    for part in ('hooks', 'translate-to-buddy'):
        shutil.rmtree(target / part, ignore_errors=True)
        shutil.copytree(PLUGIN / part, target / part, ignore=shutil.ignore_patterns('__pycache__', '.DS_Store'))


def install_quick_action():
    run(['bash', str(_config.home() / 'translate-to-buddy/install.sh')])
    print('✓ Translate → Buddy Quick Action installed.')


def remove_quick_action():
    if quick_action_path().exists():
        run(['bash', str(PLUGIN / 'translate-to-buddy/uninstall.sh')], check=False)
        print('✓ Translate → Buddy Quick Action removed.')


def mcp_references():
    # Files on this Mac that still mention hexabuddy-buddy.
    files = [Path.home() / '.claude.json', claude_settings_path(), codex_home() / 'config.toml', codex_home() / 'hooks.json',
             codex_home() / '.codex-global-state.json', codex_home() / '.credentials.json']
    return [str(path) for path in files if path.exists() and MCP_NAME in path.read_text(errors='ignore')]


# ── the three commands ──

def connect(args):
    # One QR + one phone approval; installs every piece for every agent found, then reports status.
    agents = found_agents()
    if agents and not shutil.which('jq'):
        raise SetupError('jq is required by the hooks. Install it, then connect again.')
    state = _config.state()
    server, ca_cert = server_and_ca(args, state)
    secure_key_url(server)
    check_remote_mcp(server, ca_cert)
    api_key, desktop_id = obtain_key(args, server, ca_cert, state)
    if state.get('server') not in (None, server):
        forget_key(state['server'])  # the Keychain holds one desktop key: this server's
    state['server'] = server
    if desktop_id:
        state['desktopId'] = desktop_id
    ca_cert = install_ca(ca_cert)
    trust_server_ca(ca_cert, state)
    save_settings(state_path(), state, keep_backup=False)
    print(f'✓ Paired; state saved to {state_path()}.')
    install_copies()
    install_quick_action()   # first: no agent's sign-in can block it
    caller = calling_agent()
    for agent in sorted(agents, key=lambda a: a != caller):   # the agent running connect goes first
        if agent == 'claude':
            install_claude()
        else:
            configure_codex_hooks()
        if caller and agent != caller:
            # Never wait on another agent's browser sign-in. Codex gets its hexabuddy-buddy entry (no sign-in), so it can
            # Authenticate it from /mcp later; that page asks for its own QR scan.
            if agent == 'codex':
                add_codex_entry_quietly(server, desktop_id)
            print(f'• {agent}: hooks and {MCP_NAME} added; sign it in from {agent}: /mcp → {MCP_NAME} → Authenticate, '
                  'then scan the QR code with Devices → Scan QR code.', flush=True)
            continue
        try:
            connect_mcp(agent, server, api_key, desktop_id, ca_cert)
        except (SetupError, subprocess.CalledProcessError, OSError) as error:
            print(f'! {agent}: {MCP_NAME} sign-in did not finish ({error}). Run connect again from {agent}.', flush=True)
    return status(args)


def calling_agent():
    # The agent whose plugin command runs this script: Claude Code marks its shell with CLAUDECODE. Unknown (a plain
    # terminal) → None: every agent found is connected and signed in, as before.
    if os.environ.get('CLAUDECODE') or os.environ.get('CLAUDE_CODE_ENTRYPOINT'):
        return 'claude'
    return None


def disconnect(args):
    # Revoke this Mac on the server, then remove every piece: agent entries, CA trust, Quick Action, Keychain key, folder.
    if codex_cache_blocked():
        # BUG-034: stop before removing anything, so the command still exists for the rerun.
        print('! The Codex app (inside ChatGPT) is running and keeps hexabuddy-buddy cached. Quit ChatGPT, then run uninstall.')
        return 1
    state = _config.state()
    server = state.get('server', '')
    api_key = stored_key(server, state)
    ca_cert = str(home_ca()) if home_ca().is_file() else None
    revoked, note = revoke_self(server, api_key, ca_cert) if api_key else (False, 'no desktop key on this Mac')
    print(('✓' if revoked else '✗') + ' Server: ' + note)
    for agent in found_agents():
        if agent == 'claude':
            remove_claude()
        else:
            remove_codex()
    cache_cleared = clear_codex_app_cache()
    remove_ca_trust(state)
    remove_quick_action()
    forget_local(server or _config.bundled(PLUGIN).get('server', ''))
    remove_leftovers()
    if not cache_cleared:
        print('! The Codex app started during uninstall and keeps hexabuddy-buddy cached; it goes when Codex rescans its MCP servers.')
    if revoked:
        print('This Mac is disconnected.')
    else:
        print('This Mac is cleared locally. If the phone still lists it: Devices → this Mac → Disconnect this Mac.')
    return 0 if cache_cleared else 1


def sync(args):
    # Local switch (privacy): off → every hook stops on this Mac before any network call (hooks/config.py), so no session status,
    # prompt or reply leaves the Mac. Questions (MCP) and Translate → Buddy are not affected. Saved in state.json.
    if args.state not in ('on', 'off'):
        print(f'Sync to phone: {"off" if _config.state().get("sync") is False else "on"}')
        return 0
    state = _config.state()
    if args.state == 'off':
        state['sync'] = False
    else:
        state.pop('sync', None)
    save_settings(state_path(), state, keep_backup=False)
    print('✓ Sync to phone: ' + ('off — nothing leaves this Mac; questions and Translate → Buddy still work.'
                                        if args.state == 'off' else 'on — session status goes to your phone.'))
    return 0


def status(args):
    # Read-only report of every piece; exit 1 when any piece is missing.
    state = _config.state()
    server = state.get('server', '')
    results = []

    def report(ok, text):
        results.append(ok)
        print(('✓ ' if ok else '✗ ') + text)
    report(bool(server), f'Server: {server or "not set"}')
    report_key(server, state, report)
    print('  Sync to phone: ' + ('off — nothing leaves this Mac' if state.get('sync') is False else 'on'))
    for agent in found_agents():
        report_agent(agent, report)
    installed = quick_action_path().exists()
    report(installed, 'Translate → Buddy Quick Action ' + ('installed' if installed else 'not installed'))
    leftovers = mcp_references()
    print('  hexabuddy-buddy appears in: ' + ', '.join(leftovers) if leftovers else '  No file mentions hexabuddy-buddy.')
    return 0 if all(results) else 1


def report_key(server, state, report):
    # The desktop key: this Mac connected, the key in the Keychain, accepted by the server.
    if not server:
        report(False, 'Desktop key: none. This Mac is not connected; run /hexabuddy-workspace:connect.')
        return
    api_key = stored_key(server, state)
    if not api_key:
        report(False, 'Desktop key: missing from the Keychain. Run connect again.')
        return
    ca_cert = str(home_ca()) if home_ca().is_file() else None
    answer = key_state(server, api_key, ca_cert)
    messages = {'accepted': 'Desktop key accepted by the server',
                'rejected': 'Desktop key revoked or expired. Run connect again.',
                'unreachable': 'Server unreachable. Check the address, certificate and network.'}
    report(answer == 'accepted', messages[answer])


def report_agent(agent, report):
    # One client: hooks present (and trusted, for Codex) and its hexabuddy-buddy connection.
    if agent == 'claude':
        enabled = any(p.get('id') == 'hexabuddy-workspace@hexabuddy-plugin' and p.get('enabled') for p in claude_plugins())
        report(enabled, 'Claude Code hooks ' + ('enabled' if enabled else 'not installed'))
    else:
        ids = codex_hook_ids(codex_home() / 'hooks.json')
        trusted = len(set(ids) & codex_trust_records())
        report(bool(ids), f'Codex hooks: {len(ids)} configured')
        report(bool(ids) and trusted == len(ids), f'Codex hooks trusted: {trusted}/{len(ids)} (review them with /hooks in Codex)')
    state, detail = mcp_state(agent)
    report(state == 'connected', f'{agent} {MCP_NAME}: {state} ({detail})')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['connect', 'uninstall', 'disconnect', 'status', 'sync'])  # disconnect = old name of uninstall
    parser.add_argument('state', nargs='?', choices=['on', 'off'], help='sync: on or off (no value: show it)')
    parser.add_argument('--server', help='connect: server URL (default: connection.json)')
    parser.add_argument('--ca-cert', help='connect: PEM CA certificate for that server')
    parser.add_argument('--name', default=socket.gethostname()[:80], help='connect: name shown on the phone')
    parser.add_argument('--api-key', action='store_true', help='connect: paste a key created on the phone instead of scanning')
    args = parser.parse_args(argv)
    return {'connect': connect, 'uninstall': disconnect, 'disconnect': disconnect, 'status': status, 'sync': sync}[args.command](args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        # Only our own diagnostics; third-party output may hold secrets.
        if isinstance(error, SetupError):
            print(f'Setup: {error}', file=sys.stderr)
        else:
            print('Setup failed: check file permissions, configuration syntax and agent CLIs.', file=sys.stderr)
        sys.exit(1)
