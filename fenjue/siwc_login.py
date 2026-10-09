"""Independent Sign in with ChatGPT public-client flow for this local host.

Only the loopback launch URL and fixed status codes are public. OAuth state,
PKCE, authorization URLs and credentials are never written to public.json or
logged. The implementation follows the published SIWC protocol, not an SDK.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import socket
import stat
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit
import uuid

import httpx
import jwt


ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
MAX_DEADLINE_SECONDS = 600
MAX_JSON_BYTES = 1024 * 1024
_CLIENT_ID = re.compile(r"oaiapp_[A-Za-z0-9_-]{1,256}\Z")
_SUBJECT_HASH = re.compile(r"[a-fA-F0-9]{64}\Z")


class LoginError(Exception):
    """An intentionally non-sensitive, fixed public error code."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


def _no_link(path: Path) -> None:
    if path.is_symlink():
        raise LoginError("storage_path_invalid")
    if path.exists():
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise LoginError("storage_path_invalid")


def _protect_windows(path: Path) -> None:
    # Pass the path as an environment value, never interpolate it into code.
    script = r"""
$ErrorActionPreference = 'Stop'
$p = $env:FENJUE_SIWC_ACL_PATH
$item = Get-Item -LiteralPath $p -Force
if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'reparse' }
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
if ($item.PSIsContainer) {
  $acl = [Security.AccessControl.DirectorySecurity]::new()
  $flags = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
} else {
  $acl = [Security.AccessControl.FileSecurity]::new()
  $flags = [Security.AccessControl.InheritanceFlags]::None
}
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
foreach ($principal in @($sid, $system)) {
  $rule = [Security.AccessControl.FileSystemAccessRule]::new($principal, 'FullControl', $flags, 'None', 'Allow')
  $acl.AddAccessRule($rule)
}
$item.SetAccessControl($acl)
$verified = $item.GetAccessControl()
if (-not $verified.AreAccessRulesProtected) { throw 'inheritance' }
if ($verified.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'owner' }
foreach ($rule in $verified.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
  if ($rule.AccessControlType -ne 'Allow' -or $rule.IdentityReference.Value -notin @($sid.Value, 'S-1-5-18')) { throw 'principal' }
}
"""
    executable = shutil.which("powershell.exe")
    if not executable:
        raise LoginError("storage_permissions_failed")
    try:
        result = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "FENJUE_SIWC_ACL_PATH": str(path)},
            capture_output=True, check=False, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise LoginError("storage_permissions_failed") from None
    if result.returncode:
        raise LoginError("storage_permissions_failed")


class SecureStore:
    """A dedicated owner-only directory with atomic credential replacement."""
    def __init__(self, directory: Path):
        self.directory = Path(directory).absolute()
        try:
            _no_link(self.directory)
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self.directory.is_dir():
                raise LoginError("storage_path_invalid")
            if os.name == "nt":
                _protect_windows(self.directory)
            else:
                os.chmod(self.directory, 0o700)
                if self.directory.stat().st_uid != os.getuid():
                    raise LoginError("storage_permissions_failed")
        except OSError:
            raise LoginError("storage_unavailable") from None

    def read(self, name: str) -> dict | None:
        path = self.directory / name
        _no_link(path)
        if not path.exists():
            return None
        try:
            if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
                raise LoginError("storage_record_invalid")
            if os.name == "nt":
                _protect_windows(path)
            else:
                os.chmod(path, 0o600)
                if path.stat().st_uid != os.getuid():
                    raise LoginError("storage_permissions_failed")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise LoginError("storage_record_invalid")
            return value
        except (OSError, ValueError):
            raise LoginError("storage_record_invalid") from None

    def write(self, name: str, value: dict) -> None:
        # Empty temporary files inherit this directory's owner/SYSTEM ACL on
        # Windows; no credential bytes are written before that ACL exists.
        path = self.directory / name
        _no_link(path)
        temporary = self.directory / (name + "." + secrets.token_hex(8) + ".tmp")
        descriptor = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                descriptor = None
                json.dump(value, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except (OSError, ValueError):
            raise LoginError("storage_write_failed") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class LoginAttempt:
    def __init__(self, store: SecureStore, expected_subject_hash: str, *,
                 client: httpx.Client | None = None, timeout: float = MAX_DEADLINE_SECONDS):
        if not _SUBJECT_HASH.fullmatch(expected_subject_hash):
            raise LoginError("invalid_subject_hash")
        if not 0 < timeout <= MAX_DEADLINE_SECONDS:
            raise LoginError("invalid_timeout")
        self.store = store
        self.expected_subject_hash = expected_subject_hash.lower()
        self.state = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        self.deadline = time.monotonic() + timeout
        self.redirect_uri = ""
        self.status = "awaiting_user"
        self.consumed = False
        self._lock = threading.Lock()
        self._owns_client = client is None
        self.client = client or httpx.Client(
            transport=httpx.HTTPTransport(retries=0, trust_env=False), follow_redirects=False,
            trust_env=False, timeout=15.0,
        )
        host = store.read("host.json")
        if host is None:
            host = {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())}
            store.write("host.json", host)
        self.host_id = host.get("ext_agent_host_id")
        if not isinstance(self.host_id, str) or not self.host_id.startswith("urn:uuid:"):
            raise LoginError("storage_record_invalid")
        try:
            uuid.UUID(self.host_id.removeprefix("urn:uuid:"))
        except ValueError:
            raise LoginError("storage_record_invalid") from None
        self.saved = store.read("local.json")
        self.client_id = "dynamic_agent_client"
        if self.saved is not None:
            subject = self.saved.get("subject")
            issued = self.saved.get("client_id")
            if not isinstance(subject, str) or not self._same_subject(subject):
                raise LoginError("identity_mismatch")
            if not isinstance(issued, str) or not _CLIENT_ID.fullmatch(issued):
                raise LoginError("storage_record_invalid")
            if self.saved.get("ext_agent_host_id") != self.host_id:
                raise LoginError("storage_record_invalid")
            self.client_id = issued

    def _same_subject(self, subject: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(subject.encode("utf-8")).hexdigest(), self.expected_subject_hash)

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise LoginError("attempt_expired")
        return remaining

    def bind_callback(self, uri: str) -> None:
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/auth/callback", uri):
            raise LoginError("invalid_callback_uri")
        try:
            if not 1 <= (urlsplit(uri).port or 0) <= 65535:
                raise ValueError
        except ValueError:
            raise LoginError("invalid_callback_uri") from None
        self.redirect_uri = uri

    def authorization_url(self) -> str:
        self.remaining()
        if not self.redirect_uri:
            raise LoginError("listener_not_started")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        params = {"client_id": self.client_id, "ext_agent_host_id": self.host_id,
                  "response_type": "code", "redirect_uri": self.redirect_uri,
                  "scope": SCOPES, "resource": RESOURCE, "state": self.state,
                  "nonce": self.nonce, "code_challenge_method": "S256", "code_challenge": challenge}
        if self.client_id == "dynamic_agent_client":
            params["agent_name_hint"] = "Fenjue"
        elif self.saved:
            hint = self.saved.get("id_token")
            if isinstance(hint, str) and hint:
                params["id_token_hint"] = hint
        return AUTHORIZE_URL + "?" + urlencode(params)

    def _request_json(self, method: str, url: str, data: dict | None, error: str) -> dict:
        # Both destinations are constants. Redirects, proxy environment values,
        # retry adapters and remote-supplied discovery URLs are not used.
        if url not in (TOKEN_URL, JWKS_URL):
            raise LoginError("invalid_remote_endpoint")
        completed = queue.Queue(maxsize=1)

        def read_remote():
            # This worker has no storage authority. Even an uninterruptible
            # transport completing late can only return data to this queue.
            try:
                value = self._read_remote_json(method, url, data, error)
            except LoginError as failure:
                value = failure
            except Exception:
                value = LoginError(error)
            completed.put(value)

        threading.Thread(target=read_remote, daemon=True, name="siwc-oauth-io").start()
        try:
            result = completed.get(timeout=self.remaining())
        except queue.Empty:
            raise LoginError("attempt_expired") from None
        self.remaining()
        if isinstance(result, LoginError):
            raise result
        return result

    def _read_remote_json(self, method: str, url: str, data: dict | None, error: str) -> dict:
        try:
            raw = bytearray()
            with self.client.stream(method, url, data=data, follow_redirects=False,
                                    timeout=min(15.0, self.remaining())) as response:
                if response.status_code != 200:
                    raise LoginError(error)
                for chunk in response.iter_bytes():
                    self.remaining()
                    if len(raw) + len(chunk) > MAX_JSON_BYTES:
                        raise LoginError(error)
                    raw.extend(chunk)
            self.remaining()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise LoginError(error)
            return value
        except (httpx.HTTPError, ValueError):
            raise LoginError(error) from None

    def _verify_identity(self, token: str, issued: str) -> dict:
        try:
            header = jwt.get_unverified_header(token)
            algorithm, kid = header.get("alg"), header.get("kid")
            if algorithm not in ("RS256", "ES256") or not isinstance(kid, str) or not kid:
                raise LoginError("invalid_id_token")
            keys = self._request_json("GET", JWKS_URL, None, "jwks_unavailable").get("keys")
            if not isinstance(keys, list):
                raise LoginError("jwks_unavailable")
            selected = [key for key in keys if isinstance(key, dict) and key.get("kid") == kid
                        and key.get("use", "sig") == "sig" and key.get("alg", algorithm) == algorithm
                        and ("key_ops" not in key or "verify" in key["key_ops"])]
            if len(selected) != 1:
                raise LoginError("invalid_id_token")
            key = jwt.PyJWK.from_dict(selected[0], algorithm=algorithm)
            claims = jwt.decode(token, key.key, algorithms=[algorithm], issuer=ISSUER, audience=issued,
                                options={"require": ["iss", "aud", "sub", "iat", "exp", "nonce"]})
            subject, nonce = claims.get("sub"), claims.get("nonce")
            if not isinstance(subject, str) or not subject or not isinstance(nonce, str):
                raise LoginError("invalid_id_token")
            if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and claims.get("azp") != issued:
                raise LoginError("invalid_id_token")
            if "azp" in claims and claims["azp"] != issued:
                raise LoginError("invalid_id_token")
            if not hmac.compare_digest(nonce.encode("utf-8"), self.nonce.encode("utf-8")):
                raise LoginError("nonce_mismatch")
            if not self._same_subject(subject):
                raise LoginError("identity_mismatch")
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise LoginError("invalid_id_token") from None

    def complete_callback(self, query: str) -> None:
        claimed = False
        try:
            self.remaining()
            if not isinstance(query, str) or len(query) > 16384:
                raise LoginError("invalid_callback")
            try:
                pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                                  encoding="utf-8", errors="strict", max_num_fields=16)
            except (ValueError, UnicodeError):
                raise LoginError("invalid_callback") from None
            params = dict(pairs)
            if len(params) != len(pairs):
                raise LoginError("invalid_callback")
            state_value = params.get("state", "")
            if not hmac.compare_digest(state_value.encode("utf-8"), self.state.encode("utf-8")):
                raise LoginError("state_mismatch")
            # Unrelated local requests must not consume a user's valid login.
            with self._lock:
                if self.consumed:
                    raise LoginError("callback_consumed")
                self.remaining()
                self.consumed = True
                claimed = True
                self.status = "processing"
            if "error" in params:
                if "code" in params:
                    raise LoginError("invalid_callback")
                raise LoginError("access_denied" if params["error"] == "access_denied" else "authorization_failed")
            code = params.get("code")
            if not code:
                raise LoginError("missing_code")
            issued = params.get("client_id")
            if self.client_id == "dynamic_agent_client":
                if not issued:
                    raise LoginError("missing_client_id")
            else:
                if issued is not None and issued != self.client_id:
                    raise LoginError("client_id_mismatch")
                issued = self.client_id
            if not isinstance(issued, str) or not _CLIENT_ID.fullmatch(issued):
                raise LoginError("invalid_client_id")
            if not self.redirect_uri:
                raise LoginError("listener_not_started")
            result = self._request_json("POST", TOKEN_URL, {
                "grant_type": "authorization_code", "client_id": issued, "code": code,
                "code_verifier": self.verifier, "redirect_uri": self.redirect_uri, "resource": RESOURCE,
            }, "token_exchange_failed")
            id_token, access = result.get("id_token"), result.get("access_token")
            if not isinstance(id_token, str) or not id_token or not isinstance(access, str) or not access:
                raise LoginError("invalid_token_response")
            if str(result.get("token_type", "")).lower() != "bearer":
                raise LoginError("invalid_token_response")
            expires = result.get("expires_in")
            if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not 0 < expires <= 31536000:
                raise LoginError("invalid_token_response")
            claims = self._verify_identity(id_token, issued)
            scope = result.get("scope")
            if not isinstance(scope, str) or DIRECT_SCOPE not in scope.split():
                raise LoginError("direct_scope_missing")
            refresh = result.get("refresh_token")
            if refresh is not None and (not isinstance(refresh, str) or not refresh):
                raise LoginError("invalid_token_response")
            self.remaining()
            now = time.time()
            record = {"issuer": ISSUER, "subject": claims["sub"], "subject_hash": self.expected_subject_hash,
                      "client_id": issued, "ext_agent_host_id": self.host_id, "id_token": id_token,
                      "access_token": access, "token_type": "Bearer", "expires_in": expires,
                      "expires_at": now + expires, "scopes": sorted(set(scope.split())), "saved_at": _utc(now)}
            if refresh is not None:
                record["refresh_token"] = refresh
            if isinstance(claims.get("email"), str):
                record["email"] = claims["email"]
            earliest = result.get("earliest_refresh_at")
            if isinstance(earliest, (str, int, float)) and not isinstance(earliest, bool):
                record["earliest_refresh_at"] = earliest
            self.store.write("local.json", record)
            self.status = "saved"
        except LoginError as error:
            if claimed:
                self.status = error.code
            raise
        except Exception:
            if claimed:
                self.status = "internal_error"
            raise LoginError("internal_error") from None

    def close(self) -> None:
        if self._owns_client:
            # Cleanup must not reintroduce an unbounded wait after expiry.
            cleanup = threading.Thread(target=self.client.close, daemon=True, name="siwc-http-cleanup")
            cleanup.start()
            cleanup.join(timeout=0.2)


class _QuietHTTPServer(HTTPServer):
    def __init__(self, *args, **kwargs):
        self._connections = set()
        self._connections_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        # An idle local TCP connection must not hold the callback listener
        # beyond the pending attempt's deadline.
        remaining = getattr(self, "deadline", time.monotonic() + 2) - time.monotonic()
        connection.settimeout(max(0.05, min(2.0, remaining)))
        with self._connections_lock:
            # Expiry may have taken its connection snapshot after accept but
            # before this registration. Recheck under the same lock so every
            # accepted socket is either closed here or included by expiry.
            if time.monotonic() >= getattr(self, "deadline", float("inf")):
                connection.close()
                raise OSError("attempt_expired")
            self._connections.add(connection)
        return connection, address

    def shutdown_request(self, request):
        with self._connections_lock:
            self._connections.discard(request)
        super().shutdown_request(request)

    def close_active_connections(self):
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        # BaseServer's default handler prints tracebacks and request details.
        return


class LoginServer:
    def __init__(self, attempt: LoginAttempt, *, on_status: Callable[[str], None] | None = None):
        self.attempt = attempt
        self.on_status = on_status or (lambda status: None)
        self.launch_secret = secrets.token_urlsafe(32)
        self.begin_path = "/begin/" + self.launch_secret
        self.completed_page_seen = False
        self.finished_at = None
        self._stop = threading.Event()
        self._deadline_timer = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def send_body(self, status, text, *, location=None, referrer_policy="no-referrer"):
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", referrer_policy)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self' https://auth.openai.com; frame-ancestors 'none'; base-uri 'none'")
                self.send_header("Connection", "close")
                if location is not None:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def local_request(self):
                return self.headers.get_all("Host") == [owner.httpd.server_name + ":" + str(owner.httpd.server_port)]

            def do_GET(self):
                if not self.local_request():
                    self.send_body(400, "invalid_host")
                    return
                parsed = urlsplit(self.path)
                if parsed.path == "/auth/callback":
                    try:
                        attempt.complete_callback(parsed.query)
                    except LoginError as failure:
                        if not attempt.consumed:
                            self.send_body(400, failure.code)
                            return
                    owner.finished_at = time.monotonic()
                    try:
                        owner.publish_status()
                    except LoginError:
                        attempt.status = "status_write_failed"
                        owner.on_status(attempt.status)
                    self.send_body(303, "", location="/complete")
                elif self.path == "/start/" + owner.launch_secret and not attempt.consumed:
                    self.send_body(200, '<!doctype html><meta charset="utf-8"><title>Fenjue</title>'
                                   '<h1>Fenjue</h1><form method="post" action="' + owner.begin_path + '">'
                                   '<button type="submit">Continue with ChatGPT</button></form>',
                                   referrer_policy="same-origin")
                elif self.path == "/complete":
                    self.send_body(200, "<!doctype html><title>Fenjue</title><p>" + attempt.status + "</p>")
                    if attempt.consumed:
                        owner.completed_page_seen = True
                else:
                    self.send_body(404, "not_found")

            def do_POST(self):
                if not self.local_request() or self.headers.get("Origin", owner.origin) != owner.origin:
                    self.send_body(400, "invalid_origin")
                    return
                if self.path != owner.begin_path or attempt.consumed:
                    self.send_body(404, "not_found")
                    return
                if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                    self.send_body(400, "invalid_request")
                    return
                try:
                    url = attempt.authorization_url()
                except LoginError:
                    self.send_body(400, "attempt_expired")
                    return
                self.send_body(303, "", location=url)

        self.httpd = _QuietHTTPServer(("127.0.0.1", 0), Handler)
        # HTTPServer resolves server_name to the host name; use the literal
        # loopback authority for strict Host validation and redirect matching.
        self.httpd.server_name = "127.0.0.1"
        self.httpd.timeout = 0.25
        self.httpd.deadline = attempt.deadline
        self.origin = "http://127.0.0.1:" + str(self.httpd.server_port)
        self.start_url = self.origin + "/start/" + self.launch_secret
        attempt.bind_callback(self.origin + "/auth/callback")
        self.publish_status()

    def publish_status(self):
        self.attempt.store.write("public.json", {"start_url": self.start_url, "status": self.attempt.status})
        self.on_status(self.attempt.status)

    def _expire(self):
        self._stop.set()
        with self.attempt._lock:
            if self.attempt.status != "saved":
                self.attempt.status = "attempt_expired"
                self.attempt.consumed = True
        self.httpd.close_active_connections()
        try:
            self.publish_status()
        except LoginError:
            self.on_status(self.attempt.status)

    def serve_until_done(self):
        self._deadline_timer = threading.Timer(max(0, self.attempt.deadline - time.monotonic()), self._expire)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()
        try:
            while not self.completed_page_seen and not self._stop.is_set():
                if self.finished_at is not None and time.monotonic() - self.finished_at >= 3:
                    break
                try:
                    self.httpd.handle_request()
                except (OSError, ValueError):
                    if not self._stop.is_set():
                        raise
        finally:
            self._deadline_timer.cancel()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._stop.set()
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
        self.httpd.close_active_connections()
        self.httpd.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Start Fenjue's local Continue with ChatGPT page.")
    parser.add_argument("--state-dir", type=Path, default=Path("runtime/siwc"))
    parser.add_argument("--expected-subject-hash", required=True)
    parser.add_argument("--timeout", type=float, default=MAX_DEADLINE_SECONDS)
    args = parser.parse_args(argv)
    attempt = None
    try:
        attempt = LoginAttempt(SecureStore(args.state_dir), args.expected_subject_hash, timeout=args.timeout)
        with LoginServer(attempt) as server:
            print(json.dumps({"start_url": server.start_url, "status": attempt.status}), flush=True)
            server.on_status = lambda status: print(json.dumps({"status": status}), flush=True)
            server.serve_until_done()
        return 0 if attempt.status == "saved" else 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled"}), flush=True)
        return 1
    except LoginError as error:
        print(json.dumps({"status": error.code}), flush=True)
        return 1
    except Exception:
        print(json.dumps({"status": "internal_error"}), flush=True)
        return 1
    finally:
        if attempt is not None:
            attempt.close()


if __name__ == "__main__":
    raise SystemExit(main())
