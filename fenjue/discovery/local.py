"""Explicit local installation only; credentials are never serialized by this module."""
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx

from .transport import scrub

LOCAL = 'http://127.0.0.1:18080'
DIRECT = 'https://chatgpt.com/backend-api/codex/responses'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def controls_fingerprint(account, group, creds):
    # Usage telemetry changes on successful calls and is not routing configuration.
    mutable = {'codex_usage_updated_at', 'codex_usage_observed_at_unix_nano',
               'codex_credits_snapshot', 'codex_consumer_credits_snapshot',
               'codex_reset_credit_snapshot', 'codex_reset_credit_updated_at',
               'codex_auto_reset_credit_state', 'scheduled_account_test_last_started_at',
               'codex_primary_over_secondary_percent'}
    extra = {k: v for k, v in (account.get('extra') or {}).items() if k not in mutable and not
             re.fullmatch(r'codex_(5h|7d|primary|secondary)_(used_percent|reset_at|reset_after_seconds|window_minutes)', k)}
    stable_creds = {k: v for k, v in creds.items() if k not in ('access_token','refresh_token','id_token')}
    return fingerprint([account | {'extra': extra}, group, stable_creds])


def verify_binding(info, config):
    if (info.get('account_id') != config['account_id'] or info.get('group_id') != config['group_id']
            or info.get('members') != [config['account_id']]
            or info.get('platform') != 'openai' or info.get('type') != 'oauth'
            or info.get('status') != 'active' or info.get('group_status') != 'active'
            or info.get('fallback_group_id') or info.get('fallback_group_id_on_invalid_request')
            or info.get('model_routing_enabled')):
        raise ValueError('Account/group binding is not an exclusive active OAuth route without fallback')


class LocalAccountSource:
    def __init__(self, config):
        self.config = config
        self.install = Path(config['local_install'])
        self.settings = json.loads((self.install / 'private-settings.json').read_text(encoding='utf-8-sig'))
        self.env = self.settings['env']
        self.client = httpx.Client(timeout=15, trust_env=False, follow_redirects=False)
        self.token = None
        self.key = None
        self.key_id = None
        self.last_usage_id = 0
        self.cleanup_result = None
        self.on_cleanup = lambda record: None
        self._credentials = {}
        self.secrets = [v for k, v in self.env.items() if 'PASSWORD' in k or 'SECRET' in k]
        self.session = str(uuid.uuid4())

    def close(self):
        self.client.close()

    def sql(self, query):
        env = os.environ.copy()
        env['PGPASSWORD'] = self.env['DATABASE_PASSWORD']
        proc = subprocess.run([str(self.install / 'runtime/pgsql/bin/psql.exe'), '-h', '127.0.0.1',
                               '-p', '15432', '-U', self.env['DATABASE_USER'], '-d', self.env['DATABASE_DBNAME'],
                               '-At', '-v', 'ON_ERROR_STOP=1', '-c', query],
                              capture_output=True, env=env, timeout=15)
        if proc.returncode:
            raise ValueError('Local database query failed (details withheld from logs)')
        return json.loads(proc.stdout.decode('utf-8'))

    def inventory(self):
        aid, gid = self.config['account_id'], self.config['group_id']
        row = self.sql(f"""SELECT json_build_object(
          'account_id',a.id,'group_id',g.id,'platform',a.platform,'type',a.type,
          'status',a.status,'group_status',g.status,'credentials',a.credentials,
          'account_controls',json_build_object('extra',a.extra,'proxy_id',a.proxy_id,'parent',a.parent_account_id),
          'group_controls',to_jsonb(g)-'name'-'description'-'created_at'-'updated_at',
          'fallback_group_id',g.fallback_group_id,
          'fallback_group_id_on_invalid_request',g.fallback_group_id_on_invalid_request,
          'model_routing_enabled',g.model_routing_enabled,
          'members',(SELECT coalesce(json_agg(x.account_id ORDER BY x.account_id),'[]')
             FROM account_groups x JOIN accounts b ON b.id=x.account_id
             WHERE x.group_id=g.id AND b.deleted_at IS NULL))
          FROM accounts a CROSS JOIN groups g WHERE a.id={int(aid)} AND g.id={int(gid)}
          AND a.deleted_at IS NULL AND g.deleted_at IS NULL""")
        if not row:
            raise ValueError('Selected account/group missing')
        verify_binding(row, self.config)
        creds = row.pop('credentials')
        if not creds.get('access_token') or not creds.get('chatgpt_account_id'):
            raise ValueError('Selected account has no Codex OAuth credentials')
        self._credentials = creds
        self.secrets.extend(v for v in creds.values() if isinstance(v, str))
        row['account_ref'] = fingerprint(creds['chatgpt_account_id'])
        row['credential_version'] = fingerprint(creds['access_token'])
        # Compare controls without dumping arbitrary database metadata into the report.
        row['controls_version'] = controls_fingerprint(row.pop('account_controls'), row.pop('group_controls'), creds)
        row['binding_schema'] = 2
        row['identity_basis'] = 'same local stored OAuth credentials; no independent identity attestation'
        return row

    def credentials(self):
        if not self._credentials:
            self.inventory()
        return self._credentials

    def api(self, method, path, body=None):
        headers = {'Authorization': 'Bearer ' + self.token} if self.token else {}
        response = self.client.request(method, LOCAL + path, headers=headers, json=body)
        if response.status_code >= 300:
            raise ValueError('Local admin API HTTP failure')
        data = response.json()
        if data.get('code') != 0:
            raise ValueError('Local admin API returned failure')
        return data.get('data')

    @contextmanager
    def temporary_key(self):
        login = self.api('POST', '/api/v1/auth/login', {'email': self.env['ADMIN_EMAIL'],
                                                      'password': self.env['ADMIN_PASSWORD']})
        self.token = login['access_token']
        self.secrets.append(self.token)
        created = self.api('POST', '/api/v1/keys', {'name': 'fenjue-' + str(uuid.uuid4()),
                           'group_id': self.config['group_id'], 'quota': 1, 'expires_in_days': 1,
                           'ip_whitelist': ['127.0.0.1']})
        self.key_id, self.key = int(created['id']), created['key']
        self.secrets.append(self.key)
        try:
            self.cleanup_result = {'key_id': self.key_id, 'verified': False, 'status': 'pending'}
            self.on_cleanup(self.cleanup_result)
            yield
        finally:
            self.api('DELETE', '/api/v1/keys/' + str(self.key_id))
            self.key = None
            deleted = self.sql(f'SELECT to_json(NOT EXISTS (SELECT 1 FROM api_keys WHERE id={int(self.key_id)} AND deleted_at IS NULL))')
            self.cleanup_result = {'key_id': self.key_id, 'verified': deleted is True,
                                   'status': 'deleted' if deleted is True else 'check_required'}
            self.on_cleanup(self.cleanup_result)
            if deleted is not True:
                raise ValueError('Temporary key cleanup needs inspection')

    def request(self, route):
        headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                   'User-Agent': 'Fenjue-Route-Discovery/0.1', 'session_id': self.session}
        if route == 'sub2api':
            if not self.key:
                raise ValueError('Temporary local key is not active')
            return LOCAL + '/v1/responses', headers | {'Authorization': 'Bearer ' + self.key}
        if route != 'codex_http':
            raise ValueError('Unsupported route')
        creds = self.credentials()
        return DIRECT, headers | {'Authorization': 'Bearer ' + creds['access_token'],
                                  'ChatGPT-Account-Id': creds['chatgpt_account_id'],
                                  'OpenAI-Beta': 'responses=experimental'}

    def audit(self, route):
        if route == 'codex_http':
            return {'verified': True, 'basis': 'explicit selected local credential sent to Codex endpoint'}
        rows = []
        for attempt in range(6):
            rows = self.sql(f"""SELECT coalesce(json_agg(row_to_json(t)),'[]') FROM
              (SELECT id,account_id,group_id,requested_model,upstream_model,upstream_response_model,
                upstream_model_mismatch,requested_reasoning_effort,reasoning_effort,input_tokens,output_tokens
               FROM usage_logs WHERE api_key_id={int(self.key_id)} AND id>{int(self.last_usage_id)} ORDER BY id) t""")
            if rows:
                break
            time.sleep(.5)
        if rows:
            self.last_usage_id = max(r['id'] for r in rows)
        valid = len(rows) == 1 and all(
            r['account_id'] == self.config['account_id'] and r['group_id'] == self.config['group_id']
            and r['requested_model'] == self.config['model']
            and r['upstream_model'] == self.config['model']
            and r['upstream_response_model'] == self.config['model']
            and r['upstream_model_mismatch'] is False
            and r['requested_reasoning_effort'] == self.config['effort']
            and r['reasoning_effort'] == self.config['effort'] for r in rows)
        return scrub({'verified': valid, 'basis': 'dedicated temporary key, sequential correlated local usage log',
                      'usage_rows': rows}, self.secrets)
