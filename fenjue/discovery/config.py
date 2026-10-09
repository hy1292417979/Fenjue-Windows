import json
from pathlib import Path

FIELDS = {'study_id', 'profile', 'seed', 'max_calls', 'timeout_seconds',
          'model', 'effort', 'local_install', 'account_id', 'group_id'}
CAPS = {'smoke': 2, 'screen': 36, 'confirm': 96}
ROUTES = ('sub2api', 'codex_http')


def validate(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('Configuration fields must match local.example.json; no credential fields')
    for name in ('study_id', 'model', 'local_install'):
        if not isinstance(value[name], str) or not value[name].strip():
            raise ValueError('Missing configuration string')
    if value['profile'] not in CAPS or value['effort'] not in ('low', 'medium', 'high', 'xhigh'):
        raise ValueError('Unsupported profile or effort')
    for name in ('seed', 'max_calls', 'timeout_seconds', 'account_id', 'group_id'):
        if type(value[name]) is not int or value[name] < (0 if name == 'seed' else 1):
            raise ValueError('Invalid integer setting')
    if value['max_calls'] > CAPS[value['profile']] or value['timeout_seconds'] > 600:
        raise ValueError('Budget or timeout exceeds profile limit')
    return value


def read(path):
    return validate(json.loads(Path(path).read_text(encoding='utf-8-sig')))
