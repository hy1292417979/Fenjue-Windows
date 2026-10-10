"""Read Cockpit-owned OAuth credentials without copying or refreshing them.

This replaces discovery.local.LocalAccountSource's Sub2API DB dependency.
Outbound requests may use the account Cockpit selected for that request; the
selected identity is still carried into the session binding so a conversation
cannot silently move to another account.
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
            current = self.settings()
            if current.get('mode') == 'dynamic':
                return {'account_ref': 'dynamic', 'controls_version': fingerprint(current)[:64]}
            return self.snapshot().binding
        config, manifest, scope, accounts, url, key = self.files()
        if account:
            selected = [a for a in accounts if a['id'] == account]
        elif len(accounts) == 1:
            # A single OAuth account is equivalent to dynamic routing, but the
            # pinned form remains useful for the legacy direct guard endpoint.
            selected = accounts
        else:
            # Cockpit's preferred-account metadata is only a scheduling hint;
            # it must never turn a multi-account API-key scope into a fixed
            # credential binding.
            selected = []
        if len(selected) == 1:
            chosen = selected[0]
            value = {'schema': 1, 'mode': 'pinned', 'account': chosen['id'], 'auth_file': chosen['authId'],
                     'account_ref': fingerprint(chosen['chatgptAccountId']), 'default_effort': 'max',
                     'route': 'codex_http', 'timeout_seconds': 300}
        elif account:
            raise BindingError('The requested GPT OAuth account is not in the selected Cockpit API-key scope.')
        else:
            # A multi-account Cockpit key is a valid configuration.  Keep a
            # small settings file for defaults, while outbound requests use
            # the identity selected by Cockpit on each request.
            value = {'schema': 2, 'mode': 'dynamic', 'default_effort': 'max',
                     'route': 'cockpit_dynamic', 'timeout_seconds': 300}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings_path.open('x', encoding='utf-8') as target:
            json.dump(value, target, indent=2)
        if value['mode'] == 'dynamic':
            return {'account_ref': 'dynamic', 'controls_version': fingerprint(value)[:64]}
        return self.snapshot().binding

    def _policy(self, manifest, scope):
        routing = scope.get('modelRouting') or {}
        if (not isinstance(routing, dict) or routing.get('routes')
                or routing.get('defaultRoute', 'oauth') != 'oauth'
                or routing.get('failurePolicy', 'strict') != 'strict'):
            raise BindingError('Explicit model routing is not supported by the native adapter.')
        if scope.get('allowedModels') or scope.get('excludedModels'):
            raise BindingError('Model-restricted access keys require an explicit policy adapter; restrictions will not be bypassed.')
        if (manifest.get('modelAliases') or scope.get('modelPrefix')
                or scope.get('providerGateway')):
            raise BindingError('Model remapping must be disabled for the selected Cockpit access key.')
        return routing

    def _credential_path(self, config, auth_id):
        if not isinstance(auth_id, str) or Path(auth_id).name != auth_id or '/' in auth_id or '\\' in auth_id:
            raise BindingError('Invalid local credential filename.')
        directory = Path(config['auth-dir']).resolve()
        auth_path = (directory / auth_id).resolve()
        if auth_path.parent != directory:
            raise BindingError('Credential path escaped Cockpit directory.')
        return auth_path

    def selected_snapshot(self, token, account_id, auth_id):
        """Build a snapshot from Cockpit's per-request selected OAuth identity."""
        if (not isinstance(token, str) or not token or not isinstance(account_id, str)
                or not isinstance(auth_id, str)
                or any(ord(c) < 33 or ord(c) > 126 for c in token + account_id)):
            raise BindingError('Cockpit selected an invalid OAuth credential.')
        config, manifest, scope, accounts, url, key = self.files()
        selected = [a for a in accounts
                    if a.get('authId', '').lower() == auth_id.lower()]
        if len(selected) != 1:
            raise BindingError('Selected Cockpit account is outside this API-key scope.')
        selected = selected[0]
        if selected.get('authKind') != 'oauth' or selected.get('chatgptAccountId') != account_id:
            raise BindingError('Selected Cockpit credential is not a GPT OAuth account.')
        auth_path = self._credential_path(config, auth_id)
        # Check the local credential's identity. Cockpit owns token validity;
        # its in-memory refresh may arrive before this file is updated.
        auth = read_json(auth_path)
        if auth.get('account_id') != account_id:
            raise BindingError('Cockpit selected OAuth identity does not match its credential file.')
        routing = self._policy(manifest, scope)
        controls = fingerprint([scope.get('accountIds', []), scope.get('allowedModels'),
                                scope.get('excludedModels'), routing, selected.get('authKind'), account_id])
        return Snapshot(fingerprint(account_id), controls, token, account_id,
                        auth.get('proxy_url') or config.get('proxy-url') or None, url, key,
                        set(scope.get('accountIds', [])) == {selected['id']})

    def snapshot(self):
        setting = read_json(self.settings_path)
        if setting.get('mode') == 'dynamic' or not setting.get('account'):
            raise BindingError('No pinned account is configured; use Cockpit-selected outbound routing.')
        config, manifest, scope, accounts, url, key = self.files()
        selected = [a for a in accounts if a.get('id') == setting.get('account')]
        if len(selected) != 1 or selected[0].get('authId') != setting.get('auth_file'):
            raise BindingError('Pinned GPT account was removed or replaced; no account fallback is allowed.')
        selected = selected[0]
        name = setting['auth_file']
        auth_path = self._credential_path(config, name)
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
        routing = self._policy(manifest, scope)
        controls = fingerprint([setting, scope.get('allowedModels'), scope.get('excludedModels'),
                                routing, selected.get('authKind'), account_id])
        return Snapshot(setting['account_ref'], controls, token, account_id, proxy, url, key,
                        set(scope.get('accountIds', [])) == {setting['account']})

    def settings(self):
        return read_json(self.settings_path)

    def accepted_local_key(self):
        return CockpitSource(self.config_path).connection()[1]
