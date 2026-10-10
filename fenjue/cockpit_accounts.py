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


def _stable_strings(value):
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({item.strip() for item in value if isinstance(item, str) and item.strip()})


def _model_matches(model, rules):
    """Match Cockpit's case-insensitive ``*`` model rules."""
    model = model.strip().lower()
    if not model:
        return False
    for rule in _stable_strings(rules):
        rule = rule.lower()
        if rule == '*':
            return True
        parts = rule.split('*')
        offset = 0
        if not rule.startswith('*') and not model.startswith(parts[0]):
            continue
        for part in parts:
            if not part:
                continue
            found = model.find(part, offset)
            if found < 0:
                break
            offset = found + len(part)
        else:
            if rule.endswith('*') or model.endswith(parts[-1]):
                return True
    return False


def _proxy(value):
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise BindingError('This adapter supports HTTP(S) proxies only.')
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
        raise BindingError('This adapter supports HTTP(S) proxies only.')
    return value


def _model_policy(manifest, scope):
    fields = {'allowedModels': scope.get('allowedModels'),
              'excludedModels': scope.get('excludedModels'),
              'globalExcludedModels': manifest.get('excludedModels')}
    for value in fields.values():
        if value is not None and (not isinstance(value, list)
                                  or any(not isinstance(v, str) for v in value)):
            raise BindingError('Invalid Cockpit model policy.')
    return {name: sorted({v.strip().lower() for v in value or [] if v.strip()})
            for name, value in fields.items()}


def _controls(manifest, scope, selected, routing):
    routing = dict(routing)
    for name in ('nativeModels', 'routableModels'):
        if name in routing:
            routing[name] = sorted({v.lower() for v in _stable_strings(routing[name])})
    return fingerprint({'schema': 2, 'scope': _stable_strings(scope.get('accountIds')),
                        'policy': _model_policy(manifest, scope), 'routing': routing,
                        'entry_id': selected['id'], 'auth_id': selected.get('authId', '').lower(),
                        'auth_kind': selected.get('authKind', '').lower(),
                        'account_id': selected.get('chatgptAccountId')})


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
    model_policy: dict = field(default_factory=dict, repr=False)

    @property
    def binding(self):
        return {'account_ref': self.account_ref, 'controls_version': self.controls_version}

    def headers(self, route, incoming):
        # These names mirror the native Codex HTTP executor.  Normalize lookup
        # case before forwarding so a caller's OpenAI-Beta value is never
        # duplicated by the default below.
        forwarded = {
            'conversation_id': 'Conversation_id',
            'x-codex-turn-state': 'X-Codex-Turn-State',
            'x-client-request-id': 'X-Client-Request-Id',
            'openai-beta': 'OpenAI-Beta', 'originator': 'Originator',
            'user-agent': 'User-Agent', 'x-codex-beta-features': 'X-Codex-Beta-Features',
            'version': 'Version', 'x-codex-turn-metadata': 'X-Codex-Turn-Metadata',
            'x-codex-window-id': 'X-Codex-Window-Id', 'thread-id': 'Thread-Id',
            'x-openai-internal-codex-responses-lite': 'X-OpenAI-Internal-Codex-Responses-Lite',
        }
        incoming_lower = {str(k).lower(): v for k, v in incoming.items()}
        headers = {canonical: incoming_lower[key] for key, canonical in forwarded.items()
                   if key in incoming_lower}
        sessions = [incoming_lower[key] for key in ('session_id', 'session-id') if key in incoming_lower]
        if len(set(sessions)) > 1:
            raise BindingError('Conflicting Codex session headers were supplied.')
        if sessions:
            # The HTTP Codex executor uses the legacy underscore spelling;
            # accept the hyphenated spelling from newer clients but emit one
            # unambiguous value upstream.
            headers['Session_id'] = sessions[0]
        headers.update({'Content-Type': 'application/json', 'Accept': 'text/event-stream, application/json',
                        'Accept-Encoding': 'identity'})
        if route == 'codex_http':
            headers.update({'Authorization': 'Bearer ' + self.token, 'ChatGPT-Account-Id': self.account_id})
            if 'OpenAI-Beta' not in headers:
                headers['OpenAI-Beta'] = 'responses=experimental'
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
        if not isinstance(config, dict) or not isinstance(manifest, dict):
            raise BindingError('Cockpit configuration has an invalid shape.')
        url, key = CockpitSource.from_config(config)
        api_keys = manifest.get('apiKeys', [])
        if not isinstance(api_keys, list):
            raise BindingError('Cockpit API-key manifest has an invalid shape.')
        keys = [k for k in api_keys if isinstance(k, dict) and k.get('key') == key and k.get('enabled') is True]
        if len(keys) != 1:
            raise BindingError('An enabled Cockpit API-key scope is required.')
        scope = keys[0]
        account_ids = scope.get('accountIds', [])
        if not isinstance(account_ids, list) or any(not isinstance(item, str) for item in account_ids):
            raise BindingError('Cockpit account scope has an invalid shape.')
        raw_accounts = manifest.get('accounts', [])
        if not isinstance(raw_accounts, list):
            raise BindingError('Cockpit account manifest has an invalid shape.')
        accounts = [a for a in raw_accounts
                    if isinstance(a, dict) and isinstance(a.get('id'), str)
                    and isinstance(a.get('authId'), str) and isinstance(a.get('chatgptAccountId'), str)
                    and a.get('id') in account_ids
                    and str(a.get('authKind', '')).lower() == 'oauth'
                    and str(a.get('provider', 'codex') or 'codex').lower() == 'codex']
        return config, manifest, scope, accounts, url, key

    def initialize(self, account=None):
        config, manifest, scope, accounts, url, key = self.files()
        if not accounts:
            raise BindingError('No GPT OAuth account is available in the selected Cockpit API-key scope.')
        self._policy(manifest, scope)
        if self.settings_path.exists():
            current = self.settings()
            if current.get('mode') == 'dynamic':
                return {'account_ref': 'dynamic', 'controls_version': fingerprint(current)[:64]}
            if len(accounts) > 1 and account is None:
                # An old single-account installation remains usable after
                # Cockpit expands the pool.  The selected outbound account is
                # always validated per request; do not require the old pinned
                # token to be valid merely to start dynamic outbound routing.
                value = {'schema': 2, 'mode': 'dynamic', 'default_effort': current.get('default_effort', 'max'),
                         'route': 'cockpit_dynamic', 'timeout_seconds': current.get('timeout_seconds', 300)}
                staged = self.settings_path.with_name('.' + self.settings_path.name + '.fenjue-new')
                staged.write_text(json.dumps(value, indent=2), encoding='utf-8')
                os.replace(staged, self.settings_path)
                return {'account_ref': 'dynamic', 'controls_version': fingerprint(value)[:64]}
            return self.snapshot().binding
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
        if (manifest.get('modelAliases') or scope.get('modelPrefix')
                or scope.get('providerGateway')):
            raise BindingError('Model remapping must be disabled for the selected Cockpit access key.')
        _model_policy(manifest, scope)
        return routing

    def validate_model(self, model, snapshot=None):
        """Validate a request model against Cockpit's native scope rules.

        This deliberately does not resolve aliases or route a model.  Callers
        should invoke it after preparing the original request and before
        submission; a rejected model must remain an explicit error.
        """
        if not isinstance(model, str) or not model.strip():
            raise BindingError('An explicit model is required.')
        if snapshot is not None and snapshot.model_policy:
            policy = snapshot.model_policy
        else:
            _, manifest, scope, _, _, _ = self.files()
            self._policy(manifest, scope)
            policy = _model_policy(manifest, scope)
        model = model.strip()
        if _model_matches(model, policy['globalExcludedModels']):
            raise BindingError('The requested model is excluded by Cockpit policy.')
        allowed = policy['allowedModels']
        if allowed and not _model_matches(model, allowed):
            raise BindingError('The requested model is outside this Cockpit API-key scope.')
        if _model_matches(model, policy['excludedModels']):
            raise BindingError('The requested model is excluded by this Cockpit API-key scope.')
        return model

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
        if str(selected.get('authKind', '')).lower() != 'oauth' or selected.get('chatgptAccountId') != account_id:
            raise BindingError('Selected Cockpit credential is not a GPT OAuth account.')
        auth_path = self._credential_path(config, auth_id)
        # Check the local credential's identity. Cockpit owns token validity;
        # its in-memory refresh may arrive before this file is updated.
        auth = read_json(auth_path)
        if auth.get('account_id') != account_id:
            raise BindingError('Cockpit selected OAuth identity does not match its credential file.')
        routing = self._policy(manifest, scope)
        controls = _controls(manifest, scope, selected, routing)
        return Snapshot(fingerprint(account_id), controls, token, account_id,
                        _proxy(auth.get('proxy_url') or config.get('proxy-url')), url, key,
                        set(scope.get('accountIds', [])) == {selected['id']}, _model_policy(manifest, scope))

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
        proxy = _proxy(auth.get('proxy_url') or config.get('proxy-url'))
        routing = self._policy(manifest, scope)
        controls = _controls(manifest, scope, selected, routing)
        return Snapshot(setting['account_ref'], controls, token, account_id, proxy, url, key,
                        set(scope.get('accountIds', [])) == {setting['account']}, _model_policy(manifest, scope))

    def settings(self):
        return read_json(self.settings_path)

    def accepted_local_key(self):
        return CockpitSource(self.config_path).connection()[1]
