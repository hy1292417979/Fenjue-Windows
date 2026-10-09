"""Single HTTP BPS request with bounded, redacted failure evidence.

The body and fixture headers are generated offline by the public Go protocol.
This diagnostic never changes endpoint, retries, or falls back to Codex.
"""
import asyncio
import json
import time

from .alternate import BPS
from .transport import completed, unlabelled_sse, scrub


async def diagnose_http(client, contract, headers, secrets, timeout=180):
    started=time.monotonic()
    record={'status':'reserved_unknown_completion'}
    body=contract['body']
    expected={'model':body['model'],'reasoning':{'effort':body['reasoning_effort']}}
    try:
        async with asyncio.timeout(timeout):
            async with client.stream('POST',BPS,headers=headers,json=body,follow_redirects=False) as response:
                record.update(http_status=response.status_code,
                              response_headers={k:v for k,v in response.headers.items() if k in {
                                  'content-type','x-request-id','request-id','cf-ray','server','retry-after','www-authenticate'}})
                raw=bytearray()
                limit=8_000_000 if response.status_code==200 else 65536
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>limit:
                        del raw[limit:]
                        record['body_truncated']=True
                        break
                try:
                    parsed=json.loads(raw)
                except (ValueError,UnicodeError):
                    parsed=None
                if response.status_code!=200:
                    record.update(status='http_error',error_body=parsed if parsed is not None else raw.decode('utf-8','replace'))
                elif record.get('body_truncated'):
                    record['status']='response_size_limit'
                else:
                    if isinstance(parsed,dict) and parsed.get('object')=='response':
                        result=completed(parsed,expected)
                        record.update(result.record())
                    elif raw.lstrip().startswith((b'data:',b'event:')):
                        result=unlabelled_sse(bytes(raw),expected)
                        record.update(result.record())
                    else:
                        record.update(status='unexpected_response',body_kind=type(parsed).__name__)
                    record['http_status']=response.status_code
    except TimeoutError:
        record['status']='timeout'
    except Exception as exc:
        # Exception text can include request material; only retain the class.
        record.update(status='transport_or_protocol_error',error_class=type(exc).__name__)
    record['elapsed_seconds']=round(time.monotonic()-started,3)
    return scrub(record,secrets)
