"""Operate Sub2API's native upstream protocol without adding a client gateway."""
import argparse
import json
import time
from pathlib import Path

from .discovery.config import read
from .discovery.local import LocalAccountSource

KEYS = ('openai_oauth_responses_websockets_v2_enabled',
        'openai_oauth_responses_websockets_v2_mode', 'openai_ws_force_http')
WS_SETTINGS = dict(zip(KEYS, (True, 'ctx_pool', False)))


def switch(source, route, snapshot):
    if route != 'codex_ws':
        raise ValueError('Only the validated native WebSocket candidate is integrated.')
    aid = source.config['account_id']
    account = source.api('GET', f'/api/v1/admin/accounts/{aid}')
    extra = account.get('extra') or {}
    value = {'account_id': aid, 'created': time.time(),
             'previous': {k: {'present':k in extra,'value':extra.get(k)} for k in KEYS},
             'applied': WS_SETTINGS}
    with Path(snapshot).open('x', encoding='utf-8') as f:
        json.dump(value, f, indent=2)
    # Native bulk-update performs an atomic JSONB key merge, even for one account.
    source.api('POST', '/api/v1/admin/accounts/bulk-update',
               {'account_ids':[aid], 'extra':WS_SETTINGS})
    current = source.api('GET', f'/api/v1/admin/accounts/{aid}').get('extra') or {}
    if any(current.get(k) != v for k, v in WS_SETTINGS.items()):
        raise RuntimeError('Protocol setting readback did not match; inspect saved rollback.')
    return {'account_id': aid, 'route': route, 'settings_verified': True,
            'actual_transport_verified': False, 'capability_gain_verified': False}


def restore(source, snapshot):
    value = json.loads(Path(snapshot).read_text(encoding='utf-8'))
    aid = source.config['account_id']
    if value['account_id'] != aid:
        raise ValueError('Rollback account mismatch')
    current = source.api('GET', f'/api/v1/admin/accounts/{aid}').get('extra') or {}
    if any(current.get(k) != v for k, v in value['applied'].items()):
        raise ValueError('Protocol settings changed since this tool applied them; preserve operator edits.')
    if set(value['previous']) != set(KEYS) or value['applied'] != WS_SETTINGS:
        raise ValueError('Rollback contains unexpected protocol settings')
    # For these three native typed settings, JSON null and absence both select
    # the native default/fallback. Preserve unrelated fields atomically.
    updates = {k:old['value'] if old['present'] else None for k,old in value['previous'].items()}
    source.api('POST', '/api/v1/admin/accounts/bulk-update',
               {'account_ids':[aid], 'extra':updates})
    check = source.api('GET', f'/api/v1/admin/accounts/{aid}').get('extra') or {}
    if any(check.get(k) != v for k,v in updates.items()):
        raise RuntimeError('Rollback readback mismatch')
    return {'account_id': aid, 'restored': True, 'absent_keys_restored_as_null':True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['status', 'enable-ws', 'restore'])
    parser.add_argument('--config', default='screen.proposal.json')
    parser.add_argument('--snapshot', default='runtime/sub2-ws-rollback.json')
    args = parser.parse_args()
    source = LocalAccountSource(read(args.config))
    try:
        info = source.inventory()
        source.token = source.api('POST', '/api/v1/auth/login',
                                 {'email':source.env['ADMIN_EMAIL'], 'password':source.env['ADMIN_PASSWORD']})['access_token']
        if args.command == 'enable-ws':
            evidence = json.loads(Path('runtime/alternate-trial/ledger.json').read_text(encoding='utf-8'))
            passed = [x for x in evidence['calls'] if x['route']=='codex_ws' and x.get('status')=='completed'
                      and (x.get('grade') or {}).get('passed') is True and x.get('model')==source.config['model']
                      and x.get('effort')=='max' and 0<=time.time()-x.get('finished',0)<3600]
            if evidence.get('account_ref')!=info['account_ref'] or len(passed)<2:
                raise ValueError('Require two recent completed same-account max WebSocket checks before enabling.')
            result = switch(source, 'codex_ws', args.snapshot)
        elif args.command == 'restore':
            result = restore(source, args.snapshot)
        else:
            account = source.api('GET', f"/api/v1/admin/accounts/{source.config['account_id']}")
            extra = account.get('extra') or {}
            result = {'account_id':source.config['account_id'], 'group_id':source.config['group_id'],
                      'protocol_settings':{k:extra.get(k) for k in KEYS},
                      'customer_endpoint':'http://127.0.0.1:18080/v1',
                      'capability_gain_verified':False}
        print(json.dumps(result, ensure_ascii=False))
    finally:
        source.close()


if __name__=='__main__':
    main()
