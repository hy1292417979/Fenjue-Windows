"""Read-only model catalog comparison. No inference or raw response persistence."""
import json
import subprocess
from pathlib import Path
import httpx
from fenjue.discovery.config import read
from fenjue.discovery.local import LocalAccountSource

NODE = r'''
let input='';for await (const c of process.stdin) input+=c;
const config=JSON.parse(input);
try {
 const r=await fetch(config.url,{headers:config.headers,redirect:'manual',signal:AbortSignal.timeout(15000)});
 const raw=await r.text(); let j;try{j=JSON.parse(raw)}catch{}
 const models=j?.models||j?.data||[];
 console.log(JSON.stringify({status:r.status,content_type:r.headers.get('content-type'),
  body_kind:j?'json':raw.trimStart().startsWith('<')?'html':'other',
  root_keys:j&&typeof j==='object'?Object.keys(j).slice(0,12):[],
  model_count:Array.isArray(models)?models.length:null,
  requested_model_listed:Array.isArray(models)&&models.some(x=>[x.slug,x.id].includes('gpt-6-astra'))}));
}catch{console.log(JSON.stringify({error:'network_error'}));}
'''

source = LocalAccountSource(read('screen.proposal.json'))
try:
    source.inventory()
    _, headers = source.request('codex_http')
    headers['Accept'] = 'application/json'
    url = 'https://chatgpt.com/backend-api/codex/models?client_version=0.160.1'
    results = {'inference_calls': 0}
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
        try:
            r = client.get(url, headers=headers)
            try:
                body = r.json()
            except ValueError:
                body = None
            models = (body.get('models') or body.get('data') or []) if isinstance(body, dict) else []
            results['httpx'] = {'status':r.status_code,'content_type':r.headers.get('content-type'),
                'body_kind':'json' if body is not None else 'html' if r.text.lstrip().startswith('<') else 'other',
                'root_keys':list(body)[:12] if isinstance(body,dict) else [],
                'model_count':len(models) if isinstance(models,list) else None,
                'requested_model_listed':isinstance(models,list) and any(x.get('slug',x.get('id'))=='gpt-6-astra' for x in models if isinstance(x,dict))}
        except httpx.HTTPError:
            results['httpx'] = {'error':'network_error'}
    p = subprocess.run(['node','--input-type=module','-e',NODE], input=json.dumps({'url':url,'headers':headers}),
                       text=True, capture_output=True, timeout=20)
    results['node'] = json.loads(p.stdout) if p.returncode==0 else {'error':'process_error'}
    target=Path('studies/astra-screen-proposal-20261007/preflight.json')
    target.write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results))
finally:
    source.close()
