"""One isolated native Codex exec trial using caller-selected managed auth.

The caller owns account selection. This module never looks up another account,
reads the desktop's credentials, logs in, or retries a submission. Native Codex
can refresh/retry internally; those attempts and upstream model identity are not
attested by its exec JSONL protocol. A completed result establishes a functioning
channel, not model identity or a quality advantage.

Auth cache: https://developers.openai.com/codex/auth/ci-cd-auth
CLI: https://developers.openai.com/codex/noninteractive
The auth.json token fields also follow codex-rs/login/src/token_data.rs.
"""
import asyncio
import base64
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .transport import CallResult, scrub


DEFAULT_RUNTIME = Path(__file__).resolve().parents[2] / 'runtime' / 'native-trial'
CHATGPT_BASE = 'https://chatgpt.com/backend-api'
MAX_OUTPUT_BYTES = 8_000_000


class _TrialFailure(Exception):
    """Only static, non-sensitive status codes cross the subprocess boundary."""


def _resolve_cli_command(command):
    if command is not None:
        if not command or isinstance(command, str):
            raise ValueError('cli_command must be an explicit command sequence')
        return tuple(command)
    configured = os.environ.get('FENJUE_CODEX_EXE')
    if configured:
        executable = Path(configured)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError('FENJUE_CODEX_EXE must name an existing absolute executable path')
        return (str(executable),)
    executable = shutil.which('codex.exe' if os.name == 'nt' else 'codex')
    if executable:
        return (executable,)
    local_app_data = os.environ.get('LOCALAPPDATA')
    if os.name == 'nt' and local_app_data:
        installed = Path(local_app_data) / 'OpenAI' / 'Codex' / 'bin'
        candidates = [path for path in installed.glob('*/codex.exe') if path.is_file()]
        if candidates:
            return (str(max(candidates, key=lambda path: path.stat().st_mtime_ns)),)
    raise ValueError('Set FENJUE_CODEX_EXE or pass cli_command for a compatible native Codex executable')


class _WindowsJob:
    """Own the entire process tree, including children of an exited parent."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                ('max_working_set', ctypes.c_size_t), ('active_process_limit', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                ('scheduling', wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io_counters', ctypes.c_ulonglong * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [('times', ctypes.c_longlong * 4), ('page_faults', wintypes.DWORD),
                ('total_processes', wintypes.DWORD), ('active_processes', wintypes.DWORD),
                ('terminated_processes', wintypes.DWORD)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD),
                ('thread_id', wintypes.DWORD), ('process_id', wintypes.DWORD),
                ('base_priority', wintypes.LONG), ('delta_priority', wintypes.LONG),
                ('flags', wintypes.DWORD)]

        self.ctypes, self.Accounting, self.ThreadEntry = ctypes, Accounting, ThreadEntry
        api = self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'OpenThread': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'ResumeThread': ([wintypes.HANDLE], wintypes.DWORD),
            'CreateToolhelp32Snapshot': ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            'Thread32First': ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            'Thread32Next': ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, pid):
        ctypes, api = self.ctypes, self.api
        process = api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            api.CloseHandle(process)
        # CREATE_SUSPENDED ensures the main thread cannot spawn a child before
        # job assignment. Resume it using documented Toolhelp/Thread APIs.
        snapshot = api.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = self.ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.process_id == pid:
                    thread = api.OpenThread(0x0002, False, entry.thread_id)  # SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        api.CloseHandle(thread)
                    return
                found = api.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError('Suspended native process has no main thread')
        finally:
            api.CloseHandle(snapshot)

    async def terminate(self):
        try:
            if not self.api.TerminateJobObject(self.handle, 1):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            deadline = time.monotonic() + 5
            while True:
                accounting = self.Accounting()
                if not self.api.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(accounting),
                                                          self.ctypes.sizeof(accounting), None):
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
                if accounting.active_processes == 0:
                    break
                if time.monotonic() >= deadline:
                    raise OSError('Native process tree did not terminate')
                await asyncio.sleep(0.01)
        finally:
            self.close()

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _restrict_directory(directory):
    """Protect the empty directory before writing any credentials into it."""
    if os.name != 'nt':
        directory.chmod(0o700)
        return
    target = str(directory).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User;"
        "$acl=New-Object System.Security.AccessControl.DirectorySecurity;"
        "$acl.SetAccessRuleProtection($true,$false);$acl.SetOwner($sid);"
        "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule("
        "$sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow');"
        "$acl.AddAccessRule($rule);"
        f"[System.IO.Directory]::SetAccessControl('{target}',$acl);"
        f"$check=[System.IO.Directory]::GetAccessControl('{target}');"
        "if(-not $check.AreAccessRulesProtected){throw 'Unprotected ACL'};"
        "$rules=$check.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]);"
        "if($rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $sid.Value "
        "-or $rules[0].AccessControlType -ne 'Allow'){throw 'Unexpected ACL'}"
    )
    system_root = os.environ.get('SystemRoot', 'C:/Windows')
    command = str(Path(system_root) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    proc = subprocess.run([command, '-NoProfile', '-NonInteractive', '-EncodedCommand',
        base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if proc.returncode:
        raise OSError('Credential directory ACL setup failed')


def _auth_bundle(credentials):
    if not isinstance(credentials, dict):
        raise ValueError('Expected caller-selected OAuth credentials')
    names = ('id_token', 'access_token', 'chatgpt_account_id')
    if any(not isinstance(credentials.get(k), str) or not credentials[k].strip() for k in names):
        raise ValueError('Selected account needs id_token, access_token and chatgpt_account_id')
    bundle = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None, 'tokens': {
        'id_token': credentials['id_token'], 'access_token': credentials['access_token'],
        # Refresh remains exclusively owned by the host. A CLI 401 or proactive
        # refresh attempt gets no reusable secret and cannot rotate host auth.
        'refresh_token': '', 'account_id': credentials['chatgpt_account_id']}}
    # Native get_token_data requires last_refresh. Use a supplied actual cache
    # timestamp or the token's real issue time, never the trial's current time.
    if credentials.get('last_refresh') is not None:
        if not isinstance(credentials['last_refresh'], str):
            raise ValueError('last_refresh must be a timestamp string')
        bundle['last_refresh'] = credentials['last_refresh']
    else:
        try:
            encoded = credentials['access_token'].split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            issued = claims['iat']
            if type(issued) not in (int, float) or issued <= 0:
                raise ValueError
            bundle['last_refresh'] = datetime.fromtimestamp(issued, timezone.utc).isoformat().replace('+00:00', 'Z')
        except (IndexError, KeyError, ValueError, TypeError, OSError, OverflowError):
            raise ValueError('Selected access token needs a valid issue time or actual last_refresh timestamp') from None
    return bundle


def _child_environment(directory, codex_home):
    # Deliberate allowlist: no OpenAI credentials, Codex app session metadata,
    # provider overrides, proxy credentials, Python startup hooks or HOME copied.
    names = ('SystemRoot', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'LANG', 'LC_ALL')
    env = {name: os.environ[name] for name in names if name in os.environ}
    profile = directory / 'profile'
    temporary = directory / 'tmp'
    for folder in (profile, temporary, profile / 'local', profile / 'roaming'):
        folder.mkdir()
    env.update({'CODEX_HOME': str(codex_home), 'USERPROFILE': str(profile),
        'LOCALAPPDATA': str(profile / 'local'), 'APPDATA': str(profile / 'roaming'),
        'TEMP': str(temporary), 'TMP': str(temporary), 'PYTHONIOENCODING': 'utf-8'})
    return env


def _arguments(command, work, final_path, model, effort):
    # Built-in OpenAI provider is intentionally retained. Its retry settings are
    # not redefined using a custom provider (which would change the route).
    configs = {
        'cli_auth_credentials_store': 'file', 'model_provider': 'openai',
        'chatgpt_base_url': CHATGPT_BASE, 'model_reasoning_effort': effort,
        'web_search': 'disabled', 'features.shell_tool': False,
        'features.multi_agent': False, 'agents.enabled': False,
        'apps._default.enabled': False, 'memories.use_memories': False,
        'memories.generate_memories': False, 'project_doc_max_bytes': 0,
        'analytics.enabled': False, 'feedback.enabled': False,
    }
    args = [*command, '--no-daemon', '-a', 'never', 'exec', '--json', '--ephemeral',
        '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check',
        '--sandbox', 'read-only', '--cd', str(work), '--model', model,
        '--output-last-message', str(final_path), '--color', 'never']
    for key, value in configs.items():
        args.extend(['-c', key + '=' + json.dumps(value)])
    args.append('-')
    return args


async def _kill_process(process, job):
    if process is None:
        if job is not None:
            job.close()
        return
    # The protocol reader has stopped. Drain pipes while terminating so a full
    # StreamReader buffer does not prevent subprocess.wait() from observing EOF.
    drains = [asyncio.create_task(_discard_stderr(stream))
              for stream in (process.stdout, process.stderr) if stream is not None]
    try:
        if job is not None:
            await job.terminate()
        elif os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.wait(), 5)
        await asyncio.wait_for(asyncio.gather(*drains), 5)
    finally:
        if job is not None:
            job.close()
        for drain in drains:
            if not drain.done():
                drain.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


async def _discard_stderr(stream):
    size = 0
    while chunk := await stream.read(65536):
        size += len(chunk)
    return size


async def _consume(process, final_path, prompt):
    process.stdin.write(prompt.encode('utf-8'))
    await process.stdin.drain()
    process.stdin.close()
    messages = {}
    counts = {}
    terminal = None
    size = 0
    turn_started = False
    allowed_items = {'agent_message', 'reasoning', 'error'}
    while line := await process.stdout.readline():
        size += len(line)
        if size > MAX_OUTPUT_BYTES:
            raise _TrialFailure('output_limit')
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            raise _TrialFailure('protocol_error') from None
        if not isinstance(event, dict):
            raise _TrialFailure('protocol_error')
        kind = event.get('type')
        if kind not in {'thread.started', 'turn.started', 'turn.completed', 'turn.failed',
                        'item.started', 'item.updated', 'item.completed', 'error'}:
            raise _TrialFailure('unsupported_event')
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ('turn.failed', 'error'):
            raise _TrialFailure('upstream_failed')
        if kind == 'turn.started':
            if turn_started:
                raise _TrialFailure('unexpected_additional_turn')
            turn_started = True
        if kind.startswith('item.'):
            item = event.get('item')
            if not isinstance(item, dict):
                raise _TrialFailure('protocol_error')
            if item.get('type') not in allowed_items:
                raise _TrialFailure('unexpected_tool_call')
            if item.get('type') == 'error':
                raise _TrialFailure('upstream_failed')
            if kind == 'item.completed' and item.get('type') == 'agent_message':
                item_id, text = item.get('id'), item.get('text')
                if not isinstance(item_id, str) or not isinstance(text, str):
                    raise _TrialFailure('protocol_error')
                if item_id in messages and messages[item_id] != text:
                    raise _TrialFailure('conflicting_output')
                messages[item_id] = text
        if kind == 'turn.completed':
            if terminal is not None or not turn_started:
                raise _TrialFailure('protocol_error')
            terminal = event
    exit_code = await process.wait()
    if exit_code:
        raise _TrialFailure('cli_failed')
    if terminal is None:
        raise _TrialFailure('missing_completed')
    if not final_path.is_file() or final_path.stat().st_size > MAX_OUTPUT_BYTES:
        raise _TrialFailure('missing_final_output')
    answer = final_path.read_text(encoding='utf-8')
    # exec items currently omit phase. Its explicit last-message output selects
    # the final answer, and the completed item confirms that text was emitted.
    matches = [text for text in messages.values() if text == answer or text + '\n' == answer]
    if not answer.strip() or not matches:
        raise _TrialFailure('unconfirmed_final_output')
    usage = terminal.get('usage') or {}
    if not isinstance(usage, dict):
        raise _TrialFailure('protocol_error')
    return answer, {k: v for k, v in usage.items() if k in (
        'input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens',
        'total_tokens') and type(v) is int and v >= 0}, counts


async def native_codex_call(credentials, prompt, *, model='gpt-6-astra', effort='high',
                           cli_command=None, timeout=180, runtime_dir=None,
                           cancel_event=None):
    """Submit one task; clean its private auth directory on every exit path.

    Credentials are the selected Sub2API account's token dict, never a path to
    desktop auth. An asyncio cancellation is propagated after cleanup; an
    explicit cancel_event returns status='cancelled'. The deadline includes
    isolation setup and all output, and is not extended by progress events.

    Pass cli_command to pin a compatible CLI. Otherwise selection uses an
    absolute FENJUE_CODEX_EXE, native codex on PATH, then the newest installed
    Windows desktop bin/*/codex.exe. Required exec flags are passed unchanged;
    an incompatible CLI fails without switching provider or retrying.
    """
    bundle = _auth_bundle(credentials)
    if model != 'gpt-6-astra' or effort not in ('high', 'max'):
        raise ValueError('Native trial supports gpt-6-astra with high or max only')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > 2_000_000:
        raise ValueError('Expected a nonempty bounded text task')
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 3600:
        raise ValueError('Expected deadline seconds in (0, 3600]')
    cli_command = _resolve_cli_command(cli_command)
    started = time.monotonic()
    deadline = started + timeout
    result = CallResult(wire_format='codex_exec_jsonl', diagnostic={
        'client_submissions': 0, 'upstream_attempts': None,
        'native_internal_retries': 'unknown', 'identity_verification': 'unverified',
        'requested_model': model, 'requested_effort': effort,
        'requested_provider': 'openai', 'requested_chatgpt_base_url': CHATGPT_BASE,
        'endpoint_verification': 'native_cli_managed_unverified',
        'tool_policy': 'shell_and_search_disabled_abort_on_tool_event',
        'auth_storage': 'isolated_file', 'auth_cleanup_verified': False,
        'refresh_ownership': 'host_only_cli_receives_empty_refresh_token',
    })
    runtime = Path(runtime_dir or DEFAULT_RUNTIME).resolve()
    directory = None
    process = None
    job = None
    tasks = []
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='trial-', dir=runtime)).resolve()
        _restrict_directory(directory)
        if time.monotonic() >= deadline:
            raise _TrialFailure('timeout')
        if cancel_event is not None and cancel_event.is_set():
            raise _TrialFailure('cancelled')
        codex_home, work = directory / 'codex-home', directory / 'work'
        codex_home.mkdir()
        work.mkdir()
        auth_path = codex_home / 'auth.json'
        with auth_path.open('x', encoding='utf-8') as handle:
            if os.name != 'nt':
                os.fchmod(handle.fileno(), 0o600)
            json.dump(bundle, handle)
        env = _child_environment(directory, codex_home)
        final_path = directory / 'final.txt'
        args = _arguments(tuple(cli_command), work, final_path, model, effort)
        if os.name == 'nt':
            job = _WindowsJob()
            spawn_options = {'creationflags': subprocess.CREATE_NO_WINDOW | 0x00000004}
        else:
            spawn_options = {'start_new_session': True}
        process = await asyncio.create_subprocess_exec(*args, cwd=work, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=MAX_OUTPUT_BYTES + 1,
            **spawn_options)
        if time.monotonic() >= deadline:
            raise _TrialFailure('timeout')
        if job is not None:
            job.assign_and_resume(process.pid)
        result.diagnostic['client_submissions'] = 1
        consume = asyncio.create_task(_consume(process, final_path, prompt))
        drain = asyncio.create_task(_discard_stderr(process.stderr))
        tasks.extend([consume, drain])
        waiters = [consume]
        if cancel_event is not None:
            cancelled = asyncio.create_task(cancel_event.wait())
            tasks.append(cancelled)
            waiters.append(cancelled)
        done, _ = await asyncio.wait(waiters, timeout=max(0, deadline - time.monotonic()),
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise _TrialFailure('timeout')
        if cancel_event is not None and cancel_event.is_set():
            raise _TrialFailure('cancelled')
        answer, usage, counts = consume.result()
        secrets = [credentials.get(k) for k in ('id_token', 'access_token', 'refresh_token', 'chatgpt_account_id')]
        result.answer = scrub(answer, secrets)
        result.output = [{'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                          'content': [{'type': 'output_text', 'text': result.answer}]}]
        result.usage = usage
        result.diagnostic['events'] = counts
        result.status, result.ok = 'completed', True
    except _TrialFailure as exc:
        result.status = str(exc)
    except (OSError, subprocess.SubprocessError):
        result.status = 'isolation_error' if not result.diagnostic['client_submissions'] else 'cli_io_error'
    except (ValueError, UnicodeError, KeyError, TypeError, BrokenPipeError):
        result.status = 'protocol_error'
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await _kill_process(process, job)
        except (OSError, TimeoutError):
            result.ok, result.status = False, 'process_cleanup_failed'
            result.answer, result.output = '', []
        if directory is not None:
            # Delete only this invocation's generated child, never caller paths.
            if directory.parent != runtime or not directory.name.startswith('trial-') or directory.is_symlink():
                raise RuntimeError('Unexpected native trial cleanup path')
            try:
                shutil.rmtree(directory)
            except OSError:
                result.ok, result.status = False, 'auth_cleanup_failed'
                result.answer, result.output = '', []
            result.diagnostic['auth_cleanup_verified'] = not directory.exists()
        result.elapsed_seconds = round(time.monotonic() - started, 3)
    return result
