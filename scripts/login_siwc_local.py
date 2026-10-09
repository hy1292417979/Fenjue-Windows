"""Start official SIWC authorization bound to the selected local user's subject.

The previous imported identity is only an expected-identity anchor. The new
SIWC ID token must pass signature/issuer/audience/nonce and subject validation.
"""
import hashlib
import jwt

from fenjue.discovery.config import read
from fenjue.discovery.local import LocalAccountSource
from fenjue.siwc_login import main


def run():
    source=LocalAccountSource(read('screen.proposal.json'))
    try:
        source.inventory()
        creds=source.credentials()
        claims=jwt.decode(creds['id_token'],options={'verify_signature':False})
        access=jwt.decode(creds['access_token'],options={'verify_signature':False})
        subject=claims.get('sub')
        if not isinstance(subject,str) or not subject or subject!=access.get('sub'):
            raise ValueError('Stored selected-account identity is inconsistent')
        expected=hashlib.sha256(subject.encode()).hexdigest()
    finally:
        source.close()
    return main(['--state-dir','runtime/siwc','--expected-subject-hash',expected])


if __name__=='__main__':
    raise SystemExit(run())
