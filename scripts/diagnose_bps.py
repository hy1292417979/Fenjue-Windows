"""One explicitly named submission from an offline Go-generated BPS contract."""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx
import jwt

from fenjue.discovery.config import read
from fenjue.discovery.local import LocalAccountSource
from fenjue.discovery.bps_diagnostic import diagnose_http


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--slot',required=True,choices=['full-http','full-stream','full-ws','verified-fix'])
    parser.add_argument('--contract',default='runtime/bps-full-trial/contract.json')
    parser.add_argument('--official-account-user-header',action='store_true')
    args=parser.parse_args()
    contract=json.loads(Path(args.contract).read_text(encoding='utf-8-sig'))
    assert contract['url']=='https://bps.openai.com/basispoints/api/responses'
    fixture={k:v[0] for k,v in contract['headers'].items() if isinstance(v,list) and len(v)==1}
    assert len(fixture)==len(contract['headers'])
    assert next(v for k,v in fixture.items() if k.lower()=='authorization')=='Bearer FENJUE_FIXTURE_TOKEN'
    directory=Path('runtime/bps-full-trial')
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(args.slot+'.json')
    source=LocalAccountSource(read('screen.proposal.json'))
    try:
        identity=source.inventory()
        creds=source.credentials()
        claims=jwt.decode(creds['access_token'],options={'verify_signature':False})
        auth=claims.get('https://api.openai.com/auth') or {}
        account=auth.get('chatgpt_account_id') or creds['chatgpt_account_id']
        if account!=creds['chatgpt_account_id']:
            raise ValueError('Token and selected local account ID differ')
        headers={k:('Bearer '+creds['access_token'] if k.lower()=='authorization' else
                    account if k.lower() in ('chatgpt-account-id','x-openai-account-id') else v)
                 for k,v in fixture.items()}
        if args.official_account_user_header:
            if not auth.get('chatgpt_account_user_id'):
                raise ValueError('Official account-user claim is absent; no invented identity is sent')
            headers['X-OpenAI-Account-User-Id']=auth['chatgpt_account_user_id']
            source.secrets.append(auth['chatgpt_account_user_id'])
        reservation={'slot':args.slot,'reserved_client_submissions':1,'stage_maximum_submissions':4,
                     'status':'reserved_unknown_completion','started':time.time(),'account_ref':identity['account_ref'],
                     'contract_sha256':hashlib.sha256(Path(args.contract).read_bytes()).hexdigest(),
                     'unverified_token_metadata':{'expired':claims.get('exp',0)<time.time(),
                        'account_id_matches':True,'plan':auth.get('chatgpt_plan_type'),
                        'aud':claims.get('aud'),'scopes':claims.get('scope',claims.get('scp'))},
                     'protocol_version':contract.get('protocol_version')}
        reservation['official_account_user_header']=args.official_account_user_header
        with path.open('x',encoding='utf-8') as handle:
            json.dump(reservation,handle,indent=2)
        async with httpx.AsyncClient(timeout=180,trust_env=False,follow_redirects=False) as client:
            result=await diagnose_http(client,contract,headers,source.secrets,timeout=180)
        reservation.update(result)
        reservation['finished']=time.time()
        path.write_text(json.dumps(reservation,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
    finally:
        source.close()


if __name__=='__main__':
    asyncio.run(main())
