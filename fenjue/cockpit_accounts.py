"""Read Cockpit-owned OAuth credentials without copying or refreshing them.

This replaces discovery.local.LocalAccountSource's Sub2API DB dependency.
The pinned account is selected once, not changed when Cockpit reorders its pool.
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .cockpit import CockpitSource, DEFAULT_CONFIG

DIRECT = 'https://chatgpt.com/backend-api/codex'


class BindingError(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise BindingError('Local Cockpit configuration is unavailable; open Cockpit and enable local access.') from None


@dataclass(repr=False)
class Snapshot:
    account_ref: str
    controls_version: str
    token: str = field(repr=False)
    account_id: str = field(repr=False)
    proxy: str | None = field(repr=False)
    local_url: str
    local_key: str = field(repr=False)
    cockpit_exclusive: bool

    @property
    def binding(self):
        return {'account_ref': self.account_ref, 'controls_version': self.controls_version}

    def headers(self, route, incoming):
        forwarded = ('session_id', 'conversation_id', 'x-codex-turn-state', 'x-client-request-id',
                     'openai-beta', 'originator', 'user-agent', 'x-codex-beta-features')
        headers = {k: incoming[k] for k in forwarded if k in incoming}
        headers.update({'Content-Type': 'application/json', 'Accept': 'text/event-stream, application/json',
                        'Accept-Encoding': 'identity'})
        if route == 'codex_http':
            headers.update({'Authorization': 'Bearer ' + self.token, 'ChatGPT-Account-Id': self.account_id})
            headers.setdefault('OpenAI-Beta', 'responses=experimental')
        elif route == 'cockpit' and self.cockpit_exclusive:
            headers['Authorization'] = 'Bearer ' + self.local_key
        else:
            raise BindingError('Cockpit route is not exclusively bound to the selected OAuth account.')
        return headers


class CockpitAccounts:
    def __init__(self, settings_path, config_path=DEFAULT_CONFIG):
        self.settings_path = Path(settings_path)
        self.config_path = Path(config_path)

    def files(self):
        config = read_json(self.config_path)
        manifest = read_json(self.config_path.with_name('manifest.json'))
        url, key = CockpitSource(self.config_path).connection()
        keys = [k for k in manifest.get('apiKeys', []) if k.get('key') == key and k.get('enabled') is True]
        if len(keys) != 1:
            raise BindingError('An enabled Cockpit API-key scope is required.')
        scope = keys[0]
        accounts = [a for a in manifest.get('accounts', [])
                    if a.get('id') in scope.get('accountIds', []) and a.get('authKind') == 'oauth']
        return config, manifest, scope, accounts, url, key

    def initialize(self, account=None):
        if self.settings_path.exists():
            return self.snapshot().binding
        config, manifest, scope, accounts, url, key = self.files()
        if account:
            selected = [a for a in accounts if a['id'] == account]
        else:
            preferred = {r.get('accountId') for r in manifest.get('customRoutingRules', []) if r.get('isPreferred')}
            selected = [a for a in accounts if a['id'] in preferred]
            if not selected and len(accounts) == 1:
                selected = accounts
        if len(selected) != 1:
            raise BindingError('Set exactly one preferred GPT OAuth account in Cockpit, or explicitly select --account.')
        chosen = selected[0]
        value = {'schema': 1, 'account': chosen['id'], 'auth_file': chosen['authId'],
                 'account_ref': fingerprint(chosen['chatgptAccountId']), 'default_effort': 'max',
                 'route': 'codex_http', 'timeout_seconds': 300}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings_path.open('x', encoding='utf-8') as target:
            json.dump(value, target, indent=2)
        return self.snapshot().binding

    def snapshot(self):
        setting = read_json(self.settings_path)
        config, manifest, scope, accounts, url, key = self.files()
        selected = [a for a in accounts if a.get('id') == setting.get('account')]
        if len(selected) != 1 or selected[0].get('authId') != setting.get('auth_file'):
            raise BindingError('Pinned GPT account was removed or replaced; no account fallback is allowed.')
        selected = selected[0]
        name = setting['auth_file']
        if Path(name).name != name or '/' in name or '\\' in name:
            raise BindingError('Invalid local credential filename.')
        directory = Path(config['auth-dir']).resolve()
        auth_path = (directory / name).resolve()
        if auth_path.parent != directory:
            raise BindingError('Credential path escaped Cockpit directory.')
        auth = read_json(auth_path)
        token, account_id = auth.get('access_token'), auth.get('account_id')
        if (not isinstance(token, str) or not token or not isinstance(account_id, str)
                or any(ord(c) < 33 or ord(c) > 126 for c in token + account_id)
                or account_id != selected.get('chatgptAccountId')
                or fingerprint(account_id) != setting.get('account_ref')):
            raise BindingError('Pinned OAuth identity does not match Cockpit credentials.')
        expiry = auth.get('expired')
        if isinstance(expiry, (int, float)) and expiry <= time.time() + 30:
            raise BindingError('OAuth token expired; let Cockpit refresh it. No other account was selected.')
        proxy = auth.get('proxy_url') or config.get('proxy-url') or None
        if proxy and urlsplit(proxy).scheme not in ('http', 'https'):
            raise BindingError('This adapter supports HTTP(S) proxies only.')
        routing = scope.get('modelRouting') or {}
        if (not isinstance(routing, dict) or routing.get('routes')
                or routing.get('defaultRoute', 'oauth') != 'oauth'
                or routing.get('failurePolicy', 'strict') != 'strict'):
            raise BindingError('Explicit model routing is not supported by the pinned native adapter.')
        if scope.get('allowedModels') or scope.get('excludedModels'):
            raise BindingError('Model-restricted access keys require an explicit policy adapter; restrictions will not be bypassed.')
        if (manifest.get('modelAliases') or scope.get('modelPrefix')
                or scope.get('providerGateway')):
            raise BindingError('Model remapping must be disabled for the selected Cockpit access key.')
        controls = fingerprint([setting, scope.get('allowedModels'), scope.get('excludedModels'),
                                routing, selected.get('authKind'), account_id])
        return Snapshot(setting['account_ref'], controls, token, account_id, proxy, url, key,
                        set(scope.get('accountIds', [])) == {setting['account']})

    def settings(self):
        return read_json(self.settings_path)

    def accepted_local_key(self):
        return CockpitSource(self.config_path).connection()[1]
