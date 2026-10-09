"""Use separately validated SIWC credentials at the public Responses endpoint."""
import hashlib
import hmac
import json
import time

from .transport import RouteTransport
from ..siwc_login import DIRECT_SCOPE, ISSUER

API = 'https://api.openai.com/v1'


class SiwcSource:
    def __init__(self, saved, expected_subject_hash, timeout=240):
        subject = saved.get('subject', '')
        actual = hashlib.sha256(subject.encode()).hexdigest() if isinstance(subject, str) else ''
        if (saved.get('issuer') != ISSUER or not subject
                or not hmac.compare_digest(actual, expected_subject_hash)
                or saved.get('subject_hash') != actual):
            raise ValueError('SIWC selected-account identity mismatch')
        scopes = saved.get('scopes', [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        if DIRECT_SCOPE not in scopes:
            raise ValueError('SIWC direct scope is missing')
        if not isinstance(saved.get('access_token'), str) or not saved['access_token']:
            raise ValueError('SIWC access token is missing')
        if not isinstance(saved.get('expires_at'), (int, float)) or saved['expires_at'] <= time.time():
            raise ValueError('SIWC token expired; sign in again')
        self._token = saved['access_token']
        self.config = {'timeout_seconds': timeout}

    def request(self, route):
        if route != 'siwc':
            raise ValueError('SIWC route required')
        return API + '/responses', {
            'Authorization': 'Bearer ' + self._token,
            'Content-Type': 'application/json', 'Accept': 'text/event-stream',
        }

    async def catalog(self, client):
        async with client.stream('GET', API + '/models',
                                 headers={'Authorization': 'Bearer ' + self._token},
                                 follow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError('SIWC model catalog HTTP ' + str(response.status_code))
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 1_000_000:
                    raise ValueError('SIWC model catalog too large')
        models = json.loads(raw).get('models')
        if not isinstance(models, list):
            raise ValueError('SIWC model catalog contract mismatch')
        return [m['slug'] for m in models if isinstance(m, dict)
                and isinstance(m.get('slug'), str) and m.get('visibility') == 'list']

    async def call(self, client, payload, available):
        if payload['model'] not in available:
            raise ValueError('Requested model is absent from selected-account SIWC catalog')
        # Frozen text-only acceptance contract. No preview-unsupported fields.
        allowed = {'model', 'input', 'instructions', 'reasoning', 'store', 'stream'}
        if set(payload) - allowed or not isinstance(payload.get('input'), list):
            raise ValueError('Unsupported SIWC text trial payload')
        if payload.get('store') is not False or payload.get('stream') is not True:
            raise ValueError('SIWC requires store:false and stream:true')
        return await RouteTransport(self, client).call('siwc', payload)
