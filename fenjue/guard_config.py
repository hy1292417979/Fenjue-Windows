"""Scoped Codex URL enable/restore. Never persist or print config credentials."""
import argparse
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from .cockpit_accounts import CockpitAccounts

GUARD_URL = 'http://127.0.0.1:17878/v1'
PROVIDER = 'codex_local_access'


def inspect(raw):
    text = raw.decode('utf-8-sig')
    parsed = tomllib.loads(text)
    if parsed.get('model_provider') != PROVIDER:
        raise ValueError('Active Codex provider is not Cockpit; refusing to modify another provider.')
    provider = parsed.get('model_providers', {}).get(PROVIDER, {})
    if provider.get('wire_api') != 'responses' or provider.get('supports_websockets', False):
        raise ValueError('The guard requires Responses over HTTP (WebSockets disabled).')
    return text, provider


def replace_url(raw, expected, target):
    text, provider = inspect(raw)
    if provider.get('base_url') != expected:
        raise ValueError('Codex provider URL changed concurrently; no update made.')
    section = re.search(r'(?m)^\[model_providers\.codex_local_access\][^\r\n]*(?:\r?\n|$)', text)
    if not section:
        raise ValueError('Unsupported provider section syntax; manual update required.')
    end = re.search(r'(?m)^\[', text[section.end():])
    end = section.end() + end.start() if end else len(text)
    body = text[section.end():end]
    pattern = r'(?m)^(base_url[ \t]*=[ \t]*)("[^"\r\n]*"|\x27[^\x27\r\n]*\x27)([ \t]*(?:#[^\r\n]*)?\r?)$'
    updated, count = re.subn(pattern, lambda m: m[1] + json.dumps(target) + m[3], body)
    if count != 1:
        raise ValueError('Expected one simple base_url setting; no update made.')
    result = text[:section.end()] + updated + text[end:]
    # Verify that exactly the permitted field changes semantically.
    before, after = tomllib.loads(text), tomllib.loads(result)
    before['model_providers'][PROVIDER]['base_url'] = target
    if before != after:
        raise ValueError('Unexpected configuration change.')
    return (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'') + result.encode('utf-8')


def atomic_replace(path, old, new):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.fenjue-', delete=False) as temp:
        temp.write(new)
        staged = Path(temp.name)
    try:
        if path.read_bytes() != old:
            raise ValueError('Codex configuration changed concurrently; please retry.')
        os.replace(staged, path)
    finally:
        if staged.exists():
            staged.unlink()


def configure(action, path, directory):
    path, directory = Path(path), Path(directory)
    raw = path.read_bytes()
    if action in ('status', 'restore'):
        parsed = tomllib.loads(raw.decode('utf-8-sig'))
        if parsed.get('model_provider') != PROVIDER:
            return {'guard_enabled': False, 'changed': False, 'codex_provider_is_cockpit': False,
                    'note': 'Active Codex provider is not Cockpit; configuration left unchanged.'}
    _, provider = inspect(raw)
    current = provider.get('base_url')
    record = directory / 'codex-url-rollback.local.json'
    if action == 'status':
        return {'guard_enabled': current == GUARD_URL, 'base_url': current,
                'codex_provider_is_cockpit': True}
    if action in ('enable', 'record'):
        acceptance = json.loads((directory / 'acceptance.local.json').read_text())
        if acceptance.get('passed') is not True:
            raise ValueError('Real tool/continuation/compaction acceptance must pass before enabling.')
        source = CockpitAccounts(directory / 'settings.local.json')
        snapshot = source.snapshot()
        if acceptance.get('binding') != snapshot.binding:
            raise ValueError('Acceptance belongs to another account or guard configuration; re-validation required.')
        if provider.get('experimental_bearer_token') != snapshot.local_key:
            raise ValueError('Current Codex key differs from Cockpit; no credentials changed.')
        if current == GUARD_URL:
            if not record.exists():
                raise ValueError('Guard is selected but rollback record is missing.')
            return {'guard_enabled': True, 'changed': False}
        valid_url = current in (snapshot.local_url + '/v1', snapshot.local_url.replace('127.0.0.1', 'localhost') + '/v1')
        if not valid_url:
            raise ValueError('Current URL is not the live Cockpit endpoint; refusing to overwrite it.')
        metadata = {'schema': 1, 'config_path': str(path.resolve()), 'provider': PROVIDER,
                    'original_url': current, 'guard_url': GUARD_URL}
        record.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        if action == 'record':
            return {'rollback_recorded': True, 'changed': False}
        atomic_replace(path, raw, replace_url(raw, current, GUARD_URL))
        return {'guard_enabled': True, 'changed': True}
    if action == 'restore':
        if current != GUARD_URL:
            return {'guard_enabled': False, 'changed': False, 'note': 'Current URL is user/Cockpit owned; left unchanged.'}
        metadata = json.loads(record.read_text())
        if metadata.get('config_path') != str(path.resolve()) or metadata.get('provider') != PROVIDER:
            raise ValueError('Rollback record does not match this configuration.')
        target = metadata['original_url']
        # Cockpit can change ports. Restore its current endpoint if available,
        # otherwise the exact recorded endpoint; never restore any credentials.
        from .cockpit import CockpitSource
        try:
            target = CockpitSource().connection()[0] + '/v1'
        except Exception:
            pass
        atomic_replace(path, raw, replace_url(raw, GUARD_URL, target))
        return {'guard_enabled': False, 'changed': True, 'base_url': target}
    raise ValueError('Unknown action.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('enable', 'record', 'restore', 'status'))
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml')
    parser.add_argument('--state', type=Path, default=Path('runtime/guard'))
    args = parser.parse_args()
    try:
        print(json.dumps(configure(args.action, args.config, args.state)))
    except Exception as error:
        print('Configuration unchanged or incomplete: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
