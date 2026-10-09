import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fenjue.discovery.native_codex import native_codex_call, _auth_bundle, _resolve_cli_command


CREDENTIALS = {'id_token': 'fixture-id-token', 'access_token': 'fixture-access-token',
               'refresh_token': 'fixture-refresh-token', 'chatgpt_account_id': 'fixture-account',
               'last_refresh': '2026-10-07T01:00:00Z'}

FAKE_CLI = r'''
import json, os, pathlib, subprocess, sys, time
mode, receipt = sys.argv[1:3]
args = sys.argv[3:]
home = pathlib.Path(os.environ['CODEX_HOME'])
auth = json.loads((home / 'auth.json').read_text())
prompt = sys.stdin.read()
pathlib.Path(receipt).write_text(json.dumps({
    'cwd': str(pathlib.Path.cwd()), 'home': str(home), 'args': args, 'prompt': prompt,
    'foreign_env_present': any(k in os.environ for k in ('OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN',
        'OPENAI_BASE_URL', 'CODEX_REMOTE_SERVER', 'HTTP_PROXY', 'FENJUE_SECRET')),
    'auth_fields': sorted(auth['tokens']), 'auth_mode': auth['auth_mode'],
    'correct_selected_token': auth['tokens']['access_token'] == 'fixture-access-token',
    'no_refresh_secret': auth['tokens']['refresh_token'] == '',
    'work_was_empty': not list(pathlib.Path.cwd().iterdir()),
}))
if mode in ('orphan', 'orphan_success'):
    child_code = """
import os, pathlib, sys, time
credential = (pathlib.Path(os.environ['CODEX_HOME']) / 'auth.json').open()
marker = pathlib.Path(sys.argv[1])
marker.write_text(str(os.getpid()))
until = time.monotonic() + 6
while time.monotonic() < until:
    os.utime(marker, None)
    time.sleep(.03)
credential.close()
"""
    marker = pathlib.Path(receipt).with_suffix('.child')
    streams = {} if mode == 'orphan' else {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', child_code, str(marker)],
                     stdin=subprocess.DEVNULL, **streams)
    until = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < until: time.sleep(.01)
    if mode == 'orphan': sys.exit(0)
if mode == 'flood':
    sys.stdout.write('x' * 20000000)
    sys.stdout.flush()
    time.sleep(6)
def emit(value): print(json.dumps(value), flush=True)
emit({'type': 'thread.started', 'thread_id': 'fixture-thread'})
emit({'type': 'turn.started'})
if mode == 'hang':
    while True:
        emit({'type': 'item.updated', 'item': {'id': 'r1', 'type': 'reasoning', 'text': 'working'}})
        time.sleep(.04)
if mode == 'tool':
    emit({'type': 'item.started', 'item': {'id': 'c1', 'type': 'command_execution', 'command': 'example'}})
    time.sleep(60)
if mode == 'error':
    emit({'type': 'turn.failed', 'error': {'message': 'fixture-access-token opaque-reasoning'}})
    sys.exit(1)
emit({'type': 'item.completed', 'item': {'id': 'm0', 'type': 'agent_message', 'text': 'Working'}})
emit({'type': 'item.completed', 'item': {'id': 'm1', 'type': 'agent_message', 'text': 'Final result'}})
final_path = pathlib.Path(args[args.index('--output-last-message') + 1])
final_path.write_text('Final result' if mode != 'mismatch' else 'unconfirmed text')
if mode != 'partial': emit({'type': 'turn.completed', 'usage': {'input_tokens': 9, 'output_tokens': 4}})
'''


class NativeCodexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cli = self.root / 'fake_cli.py'
        self.cli.write_text(FAKE_CLI, encoding='utf-8')
        self.receipt = self.root / 'receipt.json'
        self.runtime = self.root / 'runtime'

    def tearDown(self):
        self.tmp.cleanup()

    async def call(self, mode='ok', **kwargs):
        return await native_codex_call(CREDENTIALS, '  exact task\n',
            cli_command=(sys.executable, str(self.cli), mode, str(self.receipt)),
            runtime_dir=self.runtime, **kwargs)

    def assert_cleaned(self):
        self.assertEqual(list(self.runtime.iterdir()), [])

    async def test_selected_auth_only_exact_configuration_and_real_final(self):
        foreign = {'OPENAI_API_KEY': 'other-account', 'CODEX_ACCESS_TOKEN': 'other-token',
                   'CODEX_HOME': str(self.root / 'foreign'), 'HTTP_PROXY': 'http://wrong',
                   'OPENAI_BASE_URL': 'http://wrong', 'FENJUE_SECRET': 'private'}
        with patch.dict(os.environ, foreign):
            result = await self.call(effort='max')
            self.assertEqual(os.environ['CODEX_HOME'], foreign['CODEX_HOME'])
        seen = json.loads(self.receipt.read_text())
        self.assertFalse(seen['foreign_env_present'])
        self.assertTrue(seen['work_was_empty'])
        self.assertTrue(seen['correct_selected_token'])
        self.assertTrue(seen['no_refresh_secret'])
        self.assertEqual(seen['auth_mode'], 'chatgpt')
        self.assertEqual(seen['auth_fields'], ['access_token', 'account_id', 'id_token', 'refresh_token'])
        self.assertEqual(seen['prompt'], '  exact task\n')
        for flag in ('--json', '--ephemeral', '--ignore-user-config', '--ignore-rules', '--no-daemon'):
            self.assertIn(flag, seen['args'])
        self.assertIn('model_reasoning_effort="max"', seen['args'])
        self.assertIn('features.shell_tool=false', seen['args'])
        self.assertIn('web_search="disabled"', seen['args'])
        self.assertIn('read-only', seen['args'])
        self.assertTrue(result.ok, result.status)
        self.assertEqual(result.answer, 'Final result')
        self.assertEqual(result.usage['input_tokens'], 9)
        self.assertEqual(result.diagnostic['client_submissions'], 1)
        self.assertIsNone(result.diagnostic['upstream_attempts'])
        self.assertEqual(result.diagnostic['identity_verification'], 'unverified')
        self.assertIsNone(result.model)
        self.assert_cleaned()

    async def test_partial_or_mismatched_final_is_not_success(self):
        for mode in ('partial', 'mismatch'):
            result = await self.call(mode)
            self.assertFalse(result.ok)
            self.assertEqual(result.answer, '')
            self.assert_cleaned()

    async def test_absolute_timeout_despite_continuous_progress_cleans_auth(self):
        started = time.monotonic()
        result = await self.call('hang', timeout=1.5)
        self.assertEqual(result.status, 'timeout')
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.diagnostic['client_submissions'], 1)
        self.assert_cleaned()

    async def test_exited_parent_descendants_are_killed_before_auth_cleanup(self):
        marker = self.receipt.with_suffix('.child')
        try:
            for mode in ('orphan', 'orphan_success'):
                with self.subTest(mode=mode):
                    started = time.monotonic()
                    result = await self.call(mode, timeout=1.5 if mode == 'orphan' else 10)
                    self.assertTrue(marker.exists(), 'Descendant fixture must have started')
                    self.assertEqual(result.status, 'timeout' if mode == 'orphan' else 'completed')
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertTrue(result.diagnostic['auth_cleanup_verified'])
                    self.assert_cleaned()
                    last_write = marker.stat().st_mtime_ns
                    await asyncio.sleep(.12)
                    self.assertEqual(marker.stat().st_mtime_ns, last_write, 'Descendant heartbeat survived cleanup')
                    marker.unlink()
        finally:
            # A failing regression must still let its bounded fixture exit.
            if list(self.runtime.iterdir()):
                await asyncio.sleep(6.1)

    async def test_oversized_pipe_output_does_not_deadlock_cleanup(self):
        started = time.monotonic()
        result = await self.call('flood', timeout=5)
        self.assertIn(result.status, ('protocol_error', 'output_limit'))
        self.assertLess(time.monotonic() - started, 5)
        self.assert_cleaned()

    async def test_cancel_event_and_caller_cancellation_clean_auth(self):
        cancel = asyncio.Event()
        pending = asyncio.create_task(self.call('hang', cancel_event=cancel, timeout=10))
        async with asyncio.timeout(8):
            while not self.receipt.exists():
                if pending.done(): self.fail(str(pending.result()))
                await asyncio.sleep(.02)
        cancel.set()
        result = await pending
        self.assertEqual(result.status, 'cancelled')
        self.assert_cleaned()
        self.receipt.unlink()
        pending = asyncio.create_task(self.call('hang', timeout=10))
        async with asyncio.timeout(8):
            while not self.receipt.exists():
                if pending.done(): self.fail(str(pending.result()))
                await asyncio.sleep(.02)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError): await pending
        self.assert_cleaned()

    async def test_tools_and_sensitive_errors_fail_without_leaking(self):
        result = await self.call('tool')
        self.assertEqual(result.status, 'unexpected_tool_call')
        self.assert_cleaned()
        result = await self.call('error')
        self.assertEqual(result.status, 'upstream_failed')
        serialized = json.dumps(vars(result))
        for secret in CREDENTIALS.values(): self.assertNotIn(secret, serialized)
        self.assertNotIn('opaque-reasoning', serialized)
        self.assert_cleaned()

    async def test_missing_auth_and_wrong_model_never_start(self):
        for creds, settings in ((CREDENTIALS | {'id_token': ''}, {}),
                                 (CREDENTIALS, {'model': 'other'}),
                                 (CREDENTIALS, {'effort': 'low'})):
            with self.assertRaises(ValueError):
                await native_codex_call(creds, 'task', runtime_dir=self.runtime, **settings)
        self.assertFalse(self.receipt.exists())

    def test_auth_uses_real_issue_time_and_never_shares_refresh(self):
        payload = base64.urlsafe_b64encode(json.dumps({'iat': 1700000000}).encode()).decode().rstrip('=')
        selected = {k: v for k, v in CREDENTIALS.items() if k != 'last_refresh'}
        selected['access_token'] = 'eyJhbGciOiJub25lIn0.' + payload + '.fixture'
        bundle = _auth_bundle(selected)
        self.assertEqual(bundle['last_refresh'], '2023-11-14T22:13:20Z')
        self.assertEqual(bundle['tokens']['refresh_token'], '')
        self.assertNotIn(CREDENTIALS['refresh_token'], json.dumps(bundle))
        self.assertEqual(selected['refresh_token'], CREDENTIALS['refresh_token'])

    async def test_acl_failure_prevents_auth_write_and_launch(self):
        with patch('fenjue.discovery.native_codex._restrict_directory', side_effect=OSError('acl failed')):
            result = await self.call()
        self.assertEqual(result.status, 'isolation_error')
        self.assertEqual(result.diagnostic['client_submissions'], 0)
        self.assertFalse(self.receipt.exists())
        self.assert_cleaned()

    @unittest.skipUnless(os.name == 'nt', 'Windows suspended Job assignment')
    async def test_job_assignment_failure_never_runs_suspended_cli(self):
        with patch('fenjue.discovery.native_codex._WindowsJob.assign_and_resume', side_effect=OSError('fixture assignment failure')):
            result = await self.call()
        self.assertEqual(result.status, 'isolation_error')
        self.assertEqual(result.diagnostic['client_submissions'], 0)
        self.assertFalse(self.receipt.exists())
        self.assert_cleaned()

    def test_executable_override_is_explicit_and_precedes_discovery(self):
        executable = self.root / 'fixture-codex.exe'
        executable.touch()
        with patch.dict(os.environ, {'FENJUE_CODEX_EXE': str(executable)}):
            self.assertEqual(_resolve_cli_command(None), (str(executable),))
            self.assertEqual(_resolve_cli_command(('explicit-command',)), ('explicit-command',))
        with patch.dict(os.environ, {'FENJUE_CODEX_EXE': 'relative-codex.exe'}):
            with self.assertRaises(ValueError): _resolve_cli_command(None)

    def test_path_executable_is_used_before_desktop_discovery(self):
        with patch.dict(os.environ, {'FENJUE_CODEX_EXE': ''}), \
                patch('fenjue.discovery.native_codex.shutil.which', return_value='fixture-path-codex'):
            self.assertEqual(_resolve_cli_command(None), ('fixture-path-codex',))

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop install discovery')
    def test_desktop_executable_discovery_has_no_fixed_install_hash(self):
        executable = self.root / 'OpenAI' / 'Codex' / 'bin' / 'fixture-install' / 'codex.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        with patch.dict(os.environ, {'FENJUE_CODEX_EXE': '', 'LOCALAPPDATA': str(self.root)}), \
                patch('fenjue.discovery.native_codex.shutil.which', return_value=None):
            self.assertEqual(_resolve_cli_command(None), (str(executable),))
