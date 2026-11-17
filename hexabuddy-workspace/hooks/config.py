#!/usr/bin/env python3
"""This Mac's Hexabuddy connection, read the same way by the engine, the hooks, the Quick Action and vocab-export.

Precedence: environment (developers) → state.json in the Hexabuddy folder → the bundled connection.json.
The only secret is the paired desktop key in the login Keychain (credentials.py).

    python3 config.py shell   →  four lines: folder, server, CA file, credential (empty when not connected)
"""
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
KEY_FORMAT = re.compile(r'hb_desktop_[a-f0-9]{64}')
LOOPBACK = ('localhost', '127.0.0.1', '::1')

_spec = importlib.util.spec_from_file_location('buddy_credentials', str(HERE / 'credentials.py'))
credentials = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(credentials)


def home():
    # The one folder the plugin owns: state.json, server.pem, hooks/, translate-to-buddy/, logs.
    return Path(os.environ.get('HEXABUDDY_HOME') or Path.home() / 'Library/Application Support/Hexabuddy').expanduser()


def read_json(path):
    # The JSON object in path, or {} when it does not exist.
    path = Path(path)
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f'{path} must contain a JSON object.')
    return value


def state():
    # What connect saved: {server, desktopId, trust}. No secrets.
    return read_json(home() / 'state.json')


def bundled(root=HERE.parent):
    # The plugin's release profile {server, caCertificate}; absent in the copied hooks (state.json is there instead).
    return read_json(root / 'connection.json')


def connection(root=HERE.parent):
    # {server, ca_cert, connected, desktop_id}: environment → state.json → bundled connection.json.
    saved, profile = state(), bundled(root)
    server = (os.environ.get('HEXABUDDY_SERVER') or saved.get('server') or profile.get('server') or '').rstrip('/')
    ca_cert = os.environ.get('HEXABUDDY_CA_CERT', '')
    if not ca_cert and saved.get('server') == server and (home() / 'server.pem').is_file():
        ca_cert = str(home() / 'server.pem')
    if not ca_cert and profile.get('server') == server and profile.get('caCertificate'):
        ca_cert = str(root / profile['caCertificate'])
    return {'server': server, 'ca_cert': ca_cert, 'connected': bool(server) and saved.get('server') == server,
            'desktop_id': saved.get('desktopId')}


def secure(server):
    # Credentials travel only over HTTPS or loopback, to a URL without userinfo, query or fragment.
    url = urlsplit(server)
    return not (url.username or url.password or url.query or url.fragment) and (
        url.scheme == 'https' or (url.scheme == 'http' and url.hostname in LOOPBACK))


def credential(conn):
    # This Mac's desktop key (HEXABUDDY_API_KEY, else the Keychain once connected), or '' when not connected.
    key = os.environ.get('HEXABUDDY_API_KEY', '')
    if not key and conn['connected']:
        key = credentials.keychain(conn['server'])
        if not key:
            raise RuntimeError('Desktop key missing from the Keychain. Run /hexabuddy-workspace:connect.')
    if not key:
        return ''
    if not KEY_FORMAT.fullmatch(key):
        raise RuntimeError('Invalid desktop key format. Run /hexabuddy-workspace:connect.')
    if not secure(conn['server']):
        raise RuntimeError('Desktop keys require an HTTPS server URL.')
    return key


if __name__ == '__main__':
    try:
        if sys.argv[1:] != ['shell']:
            raise RuntimeError('Usage: config.py shell')
        if state().get('monitor') is False:
            sys.exit(3)  # monitoring off on this Mac (`hexabuddy.py monitor off`): the hook sends nothing, quietly
        conn = connection()
        values = [str(home()), conn['server'], conn['ca_cert'], credential(conn)]
        if any('\n' in value for value in values):
            raise RuntimeError('Invalid configuration.')
        print('\n'.join(values))
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else 'Invalid Hexabuddy configuration.'
        print(f'[hexabuddy] {message}', file=sys.stderr)
        sys.exit(1)
