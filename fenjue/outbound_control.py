"""Atomic, reversible Cockpit-side control; never store OAuth credentials."""
import argparse
import hashlib
import json
import os
import time
import tomllib
import subprocess
from contextlib import contextmanager
from pathlib import Path

import httpx

from .cockpit_accounts import CockpitAccounts
from .guard_config import atomic_replace

STATE = Path(__file__).resolve().parents[1] / 'runtime/guard'
INSTALL = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'Cockpit Tools'
BUILD = 'fenjue-outbound-1'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def boot_id():
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                             '(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    value = result.stdout.decode('ascii').strip()
    if not value.isdigit() or len(value) < 10:
        raise ValueError('Cannot determine Windows boot identity.')
    return value


def write_control(path, value):
    new = json.dumps(value, indent=2).encode()
    if path.exists():
        atomic_replace(path, path.read_bytes(), new)
    else:
        # Installation creates the disabled file before Cockpit starts.
        with path.open('xb') as out:
            out.write(new)


def probe(source):
    url, key = source.files()[-2:]
    with httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as client:
        response = client.get(url + '/v1/fenjue/status', headers={'Authorization': 'Bearer ' + key})
        response.raise_for_status()
        result = response.json()
    if result.get('build') != BUILD:
        raise ValueError('Cockpit is not running the Fenjue-enabled sidecar.')
    return result


def identity_probe(source):
    # The outbound path is deliberately account-dynamic.  Cockpit chooses an
    # OAuth credential for this probe just as it does for a real request; the
    # guard only verifies that the chosen identity arrived intact.
    local_url, local_key = source.files()[-2:]
    config_path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
    model = tomllib.loads(config_path.read_text(encoding='utf-8-sig')).get('model', '')
    if not model.startswith(('gpt-', 'codex')):
        model = next((m for m in source.files()[1].get('modelIds', []) if isinstance(m, str) and m.startswith('gpt-')), '')
    if not model:
        raise ValueError('No GPT model available for the non-generating identity probe.')
    with httpx.Client(trust_env=False, timeout=15, follow_redirects=False) as client:
        response = client.post(local_url + '/v1/fenjue/probe',
                               headers={'Authorization': 'Bearer ' + local_key}, json={'model': model})
        response.raise_for_status()
        result = response.json()
    if (response.headers.get('x-fenjue-cockpit') != BUILD or not result.get('identity_matched')
            or result.get('generation_submitted') is not False or not result.get('account_ref')):
        raise ValueError('Cockpit-to-Fenjue dynamic identity probe failed.')
    return result | {'checked_at': time.time(), 'model': model}


def configure(action, state=STATE, install=INSTALL, source=None, drain_seconds=330):
    state, install = Path(state), Path(install)
    source = source or CockpitAccounts(state / 'settings.local.json')
    control = install / 'fenjue-guard/control.local.json'
    if action == 'status':
        value = probe(source)
        deployment = json.loads((state / 'deployment.local.json').read_text(encoding='utf-8-sig'))
        value['installed_binary_matches'] = hashlib.sha256((install / 'cockpit-cliproxy.exe').read_bytes()).hexdigest() == deployment['patched_sha256']
        try:
            with httpx.Client(trust_env=False, timeout=3) as client:
                value['guard_ready'] = client.get('http://127.0.0.1:17878/health').json().get('version') == '3.0'
        except Exception:
            value['guard_ready'] = False
        value['takeover_active'] = value.get('enabled') is True and value.get('key_matches') is True and value['installed_binary_matches'] and value['guard_ready']
        try:
            value['last_identity_probe'] = json.loads((state / 'outbound-probe.local.json').read_text())
        except (OSError, ValueError):
            value['last_identity_probe'] = None
        return value
    if action == 'enable':
        deployment = json.loads((state / 'deployment.local.json').read_text(encoding='utf-8-sig'))
        if hashlib.sha256((install / 'cockpit-cliproxy.exe').read_bytes()).hexdigest() != deployment['patched_sha256']:
            raise ValueError('Cockpit binary changed (possibly updated); takeover was not enabled.')
        probe(source)
        local_url, local_key = source.files()[-2:]
        key = json.loads((state / 'gateway-key.local.json').read_text())['api_key']
        with httpx.Client(trust_env=False, timeout=5) as client:
            health = client.get('http://127.0.0.1:17878/health').json()
        if health.get('version') != '3.0' or not health.get('cockpit_outbound_supported'):
            raise ValueError('Outbound guard v3 is not ready; Cockpit left unchanged.')
        value = {'schema': 1, 'mode': 'dynamic', 'enabled': True, 'client_key_sha256': digest(local_key),
                 'boot_id': boot_id(),
                 'client_key_id': source.files()[2].get('id', ''),
                 'auth_id': '', 'account_ref': '', 'guard_key': key}
        previous = json.loads(control.read_text()) if control.exists() else {'schema': 1, 'enabled': False}
        write_control(control, value)
        try:
            result = probe(source)
            if not result.get('enabled') or not result.get('key_matches'):
                raise ValueError('Cockpit did not acknowledge the takeover.')
            evidence = identity_probe(source)
            write_control(state / 'outbound-probe.local.json', evidence)
        except Exception:
            # Only roll back our own value, never a concurrent user's update.
            if json.loads(control.read_text()) == value:
                write_control(control, previous)
            raise
        return {'takeover_active': True, 'codex_endpoint_unchanged': True, 'build': BUILD}
    if action == 'disable':
        # Read errors fail closed: never kill the guard if release was not confirmed.
        value = json.loads(control.read_text())
        value['enabled'] = False
        write_control(control, value)
        deadline = time.monotonic() + drain_seconds
        while True:
            result = probe(source)
            if result.get('enabled') is False and result.get('active') == 0:
                return {'takeover_active': False, 'drained': True, 'codex_endpoint_unchanged': True}
            if time.monotonic() >= deadline:
                raise ValueError('Cockpit still has guarded requests; protection service must remain running.')
            time.sleep(.5)
    raise ValueError('Unknown action')


@contextmanager
def command_lock(state):
    """Serialize independent start/stop commands on Windows, released on crash."""
    import msvcrt
    path = Path(state) / 'control.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as file:
        if file.tell() == 0:
            file.write(b'0'); file.flush()
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            file.seek(0); msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('enable', 'disable', 'status'))
    args = parser.parse_args()
    try:
        with command_lock(STATE):
            print(json.dumps(configure(args.action)))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, ValueError) else type(error).__name__}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
