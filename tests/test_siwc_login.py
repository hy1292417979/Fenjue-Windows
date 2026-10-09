"""Local fixtures only: no test sends a request to OpenAI."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from fenjue.siwc_login import (
    AUTHORIZE_URL, ISSUER, JWKS_URL, RESOURCE, TOKEN_URL,
    LoginAttempt, LoginError, LoginServer, SecureStore,
)


SUBJECT = "fixture-account-subject"
SUBJECT_HASH = hashlib.sha256(SUBJECT.encode()).hexdigest()
CLIENT_ID = "oaiapp_fixture_registration"


class LoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.ec_key = ec.generate_private_key(ec.SECP256R1())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SecureStore(Path(self.temp.name) / "siwc")
        self.requests = []
        self.token_changes = {}
        self.claim_changes = {}
        self.omitted_claims = set()
        self.algorithm = "RS256"
        self.remote_status = 200
        self.redirect = False
        self.client = httpx.Client(transport=httpx.MockTransport(self.remote), follow_redirects=False)
        self.addCleanup(self.client.close)
        self.attempt = LoginAttempt(self.store, SUBJECT_HASH, client=self.client)
        self.attempt.bind_callback("http://127.0.0.1:1455/auth/callback")

    def key(self):
        return self.ec_key if self.algorithm == "ES256" else self.rsa_key

    def remote(self, request):
        self.requests.append(request)
        self.assertIn(str(request.url), (TOKEN_URL, JWKS_URL))
        if str(request.url) == JWKS_URL:
            algorithm = jwt.algorithms.ECAlgorithm if self.algorithm == "ES256" else jwt.algorithms.RSAAlgorithm
            jwk = json.loads(algorithm.to_jwk(self.key().public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "fixture-key", "use": "sig", "alg": self.algorithm}]})
        self.assertEqual(request.method, "POST")
        if self.redirect:
            return httpx.Response(302, headers={"Location": "https://example.invalid/collect"})
        if self.remote_status != 200:
            return httpx.Response(self.remote_status, json={"error": "private-upstream-message"})
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": SUBJECT, "iat": now,
                  "exp": now + 300, "nonce": self.attempt.nonce, **self.claim_changes}
        for claim in self.omitted_claims:
            claims.pop(claim, None)
        token = jwt.encode(claims, self.key(), algorithm=self.algorithm, headers={"kid": "fixture-key"})
        return httpx.Response(200, json={"access_token": "fixture-access-token",
            "refresh_token": "fixture-refresh-token", "id_token": token,
            "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
            "token_type": "Bearer", "expires_in": 3600, **self.token_changes})

    def callback(self, **changes):
        return urlencode({"code": "fixture-code", "state": self.attempt.state,
                          "client_id": CLIENT_ID, **changes})

    def assert_failure(self, code, query=None):
        with self.assertRaises(LoginError) as caught:
            self.attempt.complete_callback(query or self.callback())
        self.assertEqual(caught.exception.code, code)
        self.assertFalse((self.store.directory / "local.json").exists())

    def test_authorization_pkce_host_and_public_launch_file(self):
        params = parse_qs(urlsplit(self.attempt.authorization_url()).query)
        self.assertEqual(urlsplit(self.attempt.authorization_url())._replace(query="").geturl(), AUTHORIZE_URL)
        self.assertEqual(params["client_id"], ["dynamic_agent_client"])
        self.assertEqual(params["agent_name_hint"], ["Fenjue"])
        self.assertEqual(params["resource"], [RESOURCE])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.attempt.verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(params["code_challenge"], [challenge])
        self.assertEqual(params["nonce"], [self.attempt.nonce])
        self.assertNotEqual(self.attempt.state, self.attempt.nonce)
        other = LoginAttempt(self.store, SUBJECT_HASH, client=self.client)
        self.assertEqual(self.attempt.host_id, other.host_id)
        self.assertNotEqual(self.attempt.state, other.state)
        with LoginServer(self.attempt) as server:
            public = json.loads((self.store.directory / "public.json").read_text())
            self.assertEqual(public["start_url"], server.start_url)
            self.assertEqual(urlsplit(public["start_url"]).hostname, "127.0.0.1")
            self.assertNotIn(self.attempt.state, json.dumps(public))
            self.assertNotIn("auth.openai.com", json.dumps(public))
        self.assertEqual(self.requests, [])

    def test_success_verifies_and_atomically_saves_then_reuses_registration(self):
        self.attempt.complete_callback(self.callback())
        form = parse_qs(self.requests[0].content.decode())
        self.assertEqual(form["client_id"], [CLIENT_ID])
        self.assertEqual(form["resource"], [RESOURCE])
        self.assertEqual(form["code_verifier"], [self.attempt.verifier])
        self.assertEqual(form["redirect_uri"], [self.attempt.redirect_uri])
        saved = json.loads((self.store.directory / "local.json").read_text())
        self.assertEqual(saved["subject"], SUBJECT)
        self.assertEqual(saved["client_id"], CLIENT_ID)
        self.assertIn("chatgpt.tokens.use.direct", saved["scopes"])
        self.assertEqual(saved["refresh_token"], "fixture-refresh-token")
        self.assertFalse(list(self.store.directory.glob("*.tmp")))
        returning = LoginAttempt(self.store, SUBJECT_HASH, client=self.client)
        returning.bind_callback("http://127.0.0.1:1456/auth/callback")
        params = parse_qs(urlsplit(returning.authorization_url()).query)
        self.assertEqual(params["client_id"], [CLIENT_ID])
        self.assertNotIn("agent_name_hint", params)
        self.assertEqual(params["id_token_hint"], [saved["id_token"]])

    def test_es256_is_supported(self):
        self.algorithm = "ES256"
        self.attempt.complete_callback(self.callback())
        self.assertEqual(self.attempt.status, "saved")

    def test_bad_state_prevents_exchange(self):
        self.assert_failure("state_mismatch", self.callback(state="wrong"))
        self.assertEqual(self.requests, [])

    def test_bad_state_does_not_consume_the_pending_login(self):
        self.assert_failure("state_mismatch", self.callback(state="wrong"))
        self.assertFalse(self.attempt.consumed)
        self.assertEqual(self.attempt.status, "awaiting_user")
        self.attempt.complete_callback(self.callback())
        self.assertEqual(self.attempt.status, "saved")

    def test_duplicate_query_parameters_prevent_exchange(self):
        self.assert_failure("invalid_callback", self.callback() + "&state=second")
        self.assertEqual(self.requests, [])

    def test_registration_needs_issued_client_id(self):
        self.assert_failure("missing_client_id", urlencode({"code": "fixture-code", "state": self.attempt.state}))
        self.assertEqual(self.requests, [])

    def test_dynamic_client_id_is_not_an_issued_registration(self):
        self.assert_failure("invalid_client_id", self.callback(client_id="dynamic_agent_client"))
        self.assertEqual(self.requests, [])

    def test_access_denied_validates_state_and_does_not_exchange(self):
        self.assert_failure("access_denied", urlencode({"error": "access_denied", "state": self.attempt.state}))
        self.assertEqual(self.requests, [])

    def test_wrong_nonce_blocks_storage(self):
        self.claim_changes["nonce"] = "different-nonce"
        self.assert_failure("nonce_mismatch")

    def test_different_identity_blocks_storage(self):
        self.claim_changes["sub"] = "different-account"
        self.assert_failure("identity_mismatch")

    def test_bad_issuer_audience_expiry_and_signature_are_rejected(self):
        for claims in ({"iss": "https://example.invalid"}, {"aud": "oaiapp_other"}, {"exp": 1}):
            with self.subTest(claims=claims):
                self.attempt = LoginAttempt(self.store, SUBJECT_HASH, client=self.client)
                self.attempt.bind_callback("http://127.0.0.1:1455/auth/callback")
                self.claim_changes = claims
                self.assert_failure("invalid_id_token")
        self.attempt = LoginAttempt(self.store, SUBJECT_HASH, client=self.client)
        self.attempt.bind_callback("http://127.0.0.1:1455/auth/callback")
        self.claim_changes = {}
        self.token_changes["id_token"] = jwt.encode({"sub": SUBJECT}, "fixture-secret", algorithm="HS256")
        self.assert_failure("invalid_id_token")

    def test_granted_scope_controls_saving(self):
        self.token_changes["scope"] = "openid email profile"
        self.assert_failure("direct_scope_missing")

    def test_missing_or_future_issued_at_is_rejected(self):
        for issued_at in (None, int(time.time()) + 300):
            with self.subTest(issued_at=issued_at):
                self.attempt = LoginAttempt(self.store, SUBJECT_HASH, client=self.client)
                self.attempt.bind_callback("http://127.0.0.1:1455/auth/callback")
                self.omitted_claims = {"iat"} if issued_at is None else set()
                self.claim_changes = {} if issued_at is None else {"iat": issued_at}
                self.assert_failure("invalid_id_token")

    def test_valid_algorithm_with_wrong_signing_key_is_rejected(self):
        unrelated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.token_changes["id_token"] = jwt.encode(
            {"iss": ISSUER, "aud": CLIENT_ID, "sub": SUBJECT,
             "iat": int(time.time()), "exp": int(time.time()) + 300, "nonce": self.attempt.nonce},
            unrelated_key, algorithm="RS256", headers={"kid": "fixture-key"})
        self.assert_failure("invalid_id_token")

    def test_returning_registration_accepts_omitted_client_id(self):
        self.attempt.complete_callback(self.callback())
        self.attempt = LoginAttempt(self.store, SUBJECT_HASH, client=self.client)
        self.attempt.bind_callback("http://127.0.0.1:1456/auth/callback")
        self.attempt.complete_callback(urlencode({"code": "next-code", "state": self.attempt.state}))
        self.assertEqual(self.attempt.status, "saved")
        self.assertEqual(parse_qs(self.requests[-2].content.decode())["client_id"], [CLIENT_ID])

    def test_failed_returning_sign_in_keeps_existing_credentials(self):
        self.attempt.complete_callback(self.callback())
        before = (self.store.directory / "local.json").read_bytes()
        self.attempt = LoginAttempt(self.store, SUBJECT_HASH, client=self.client)
        self.attempt.bind_callback("http://127.0.0.1:1456/auth/callback")
        self.claim_changes["sub"] = "different-account"
        with self.assertRaises(LoginError) as caught:
            self.attempt.complete_callback(self.callback())
        self.assertEqual(caught.exception.code, "identity_mismatch")
        self.assertEqual((self.store.directory / "local.json").read_bytes(), before)

    def test_returning_client_id_mismatch_prevents_exchange(self):
        self.attempt.complete_callback(self.callback())
        self.requests.clear()
        self.attempt = LoginAttempt(self.store, SUBJECT_HASH, client=self.client)
        self.attempt.bind_callback("http://127.0.0.1:1456/auth/callback")
        with self.assertRaises(LoginError) as caught:
            self.attempt.complete_callback(self.callback(client_id="oaiapp_other"))
        self.assertEqual(caught.exception.code, "client_id_mismatch")
        self.assertEqual(self.requests, [])

    def test_timeout_is_bounded_and_callback_host_must_be_literal_loopback(self):
        with self.assertRaises(LoginError):
            LoginAttempt(self.store, SUBJECT_HASH, client=self.client, timeout=601)
        for uri in ("http://localhost:1455/auth/callback", "http://127.0.0.1:1455/callback",
                    "https://127.0.0.1:1455/auth/callback", "http://127.0.0.1:99999/auth/callback"):
            with self.subTest(uri=uri), self.assertRaises(LoginError):
                self.attempt.bind_callback(uri)

    def test_redirect_is_not_followed_or_retried(self):
        self.redirect = True
        self.assert_failure("token_exchange_failed")
        self.assertEqual(len(self.requests), 1)

    def test_callback_is_consumed_only_once(self):
        self.attempt.complete_callback(self.callback())
        with self.assertRaises(LoginError) as caught:
            self.attempt.complete_callback(self.callback())
        self.assertEqual(caught.exception.code, "callback_consumed")
        self.assertEqual(len(self.requests), 2)

    def test_expired_attempt_makes_no_network_request(self):
        self.attempt.deadline = time.monotonic() - 1
        self.assert_failure("attempt_expired")
        self.assertEqual(self.requests, [])

    def test_remote_io_respects_total_attempt_deadline(self):
        def slow_fixture(request):
            time.sleep(0.4)
            return httpx.Response(200, json={})
        client = httpx.Client(transport=httpx.MockTransport(slow_fixture))
        self.addCleanup(client.close)
        self.attempt.client = client
        self.attempt.deadline = time.monotonic() + 0.08
        started = time.monotonic()
        self.assert_failure("attempt_expired")
        self.assertLess(time.monotonic() - started, 0.25)

    def test_trickled_loopback_headers_do_not_extend_deadline(self):
        self.attempt.deadline = time.monotonic() + 0.3
        with LoginServer(self.attempt) as server:
            thread = threading.Thread(target=server.serve_until_done)
            thread.start()
            connection = socket.create_connection(("127.0.0.1", server.httpd.server_port))
            connection.sendall(b"GET / HTTP/1.1\r\nX-Fixture: ")
            started = time.monotonic()
            try:
                while time.monotonic() - started < 0.6:
                    try:
                        connection.sendall(b"x")
                    except OSError:
                        break
                    time.sleep(0.03)
                thread.join(timeout=0.3)
                alive_past_deadline = thread.is_alive()
            finally:
                connection.close()
                thread.join(timeout=4)
            self.assertFalse(alive_past_deadline)
            self.assertEqual(self.attempt.status, "attempt_expired")

    def test_connection_accepted_during_expiry_is_closed_before_registration(self):
        with LoginServer(self.attempt) as server:
            connection = Mock()

            def expire_before_registration(timeout):
                server.httpd.deadline = time.monotonic() - 1
                server._expire()

            connection.settimeout.side_effect = expire_before_registration
            with patch("fenjue.siwc_login.HTTPServer.get_request",
                       return_value=(connection, ("127.0.0.1", 12345))):
                with self.assertRaises(OSError):
                    server.httpd.get_request()
            connection.close.assert_called_once()
            self.assertEqual(server.httpd._connections, set())

    def test_loopback_callback_redirects_to_clean_completion_page(self):
        with LoginServer(self.attempt) as server:
            thread = threading.Thread(target=server.serve_until_done)
            thread.start()
            with httpx.Client(trust_env=False, follow_redirects=False) as browser:
                first = browser.get(server.start_url)
                self.assertEqual(first.status_code, 200)
                self.assertIn("Continue with ChatGPT", first.text)
                self.assertEqual(first.headers["referrer-policy"], "same-origin")
                rejected = browser.post(server.origin + server.begin_path, headers={"Origin": "null"})
                self.assertEqual(rejected.status_code, 400)
                self.assertEqual(rejected.text, "invalid_origin")
                begin = browser.post(server.origin + server.begin_path, headers={"Origin": server.origin})
                self.assertEqual(begin.status_code, 303)
                self.assertEqual(begin.headers["referrer-policy"], "no-referrer")
                self.assertTrue(begin.headers["location"].startswith(AUTHORIZE_URL + "?"))
                invalid = browser.get(self.attempt.redirect_uri + "?state=wrong")
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(invalid.text, "state_mismatch")
                response = browser.get(self.attempt.redirect_uri + "?" + self.callback())
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/complete")
                self.assertEqual(response.headers["referrer-policy"], "no-referrer")
                page = browser.get(server.origin + "/complete")
                self.assertIn("saved", page.text)
                for value in ("fixture-code", "fixture-access-token", "fixture-refresh-token"):
                    self.assertNotIn(value, response.text + page.text)
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_status_write_failure_still_finishes_listener(self):
        original_write = self.store.write

        def fail_public_write(name, value):
            if name == "public.json":
                raise LoginError("storage_write_failed")
            original_write(name, value)

        self.attempt.deadline = time.monotonic() + 10
        with LoginServer(self.attempt) as server:
            with patch.object(self.store, "write", side_effect=fail_public_write):
                thread = threading.Thread(target=server.serve_until_done, daemon=True)
                thread.start()
                with httpx.Client(trust_env=False, follow_redirects=False) as browser:
                    response = browser.get(self.attempt.redirect_uri + "?" + self.callback())
                self.assertEqual(response.status_code, 303)
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
                self.assertEqual(self.attempt.status, "status_write_failed")
                self.assertTrue((self.store.directory / "local.json").exists())

    @unittest.skipIf(os.name == "nt", "POSIX mode assertion; Windows uses protected ACLs")
    def test_owner_only_modes(self):
        self.attempt.complete_callback(self.callback())
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store.directory / "local.json").stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "nt", "Windows DACL assertion")
    def test_windows_directory_and_credential_acl_allow_only_owner_and_system(self):
        self.attempt.complete_callback(self.callback())
        script = r"""
$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($p in @($env:FENJUE_FIXTURE_DIR, ($env:FENJUE_FIXTURE_DIR + '\local.json'))) {
  $acl = (Get-Item -LiteralPath $p).GetAccessControl()
  if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid) { throw 'owner' }
  $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
  $seen = @()
  foreach ($rule in $rules) {
    if ($rule.AccessControlType -ne 'Allow' -or $rule.IdentityReference.Value -notin @($sid, 'S-1-5-18')) { throw 'principal' }
    $seen += $rule.IdentityReference.Value
  }
  if ($sid -notin $seen -or 'S-1-5-18' -notin $seen) { throw 'missing' }
}
"""
        result = subprocess.run([shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "FENJUE_FIXTURE_DIR": str(self.store.directory)},
            capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, "Fixture ACL did not restrict principals")


if __name__ == "__main__":
    unittest.main()
