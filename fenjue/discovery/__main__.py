import argparse
import asyncio
import json
import subprocess

from .study import prepare, open_study
from .report import write_report
from .candidates import candidate_inventory
from ..research.tasks import initial_payload


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='焚决：固定题集、同账号渠道对照（默认离线查看）')
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('prepare');p.add_argument('--config', required=True);p.add_argument('--out', required=True)
    for cmd in ('inventory', 'request-diff', 'run', 'report', 'status'):
        p = subs.add_parser(cmd);p.add_argument('--study', required=True)
        if cmd == 'run':
            p.add_argument('--approve-calls', type=int, required=True, help='explicitly authorize this frozen study call cap')
    p = subs.add_parser('candidates');p.add_argument('--codex-executable')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            emit(prepare(args.config, args.out));return 0
        if args.command == 'candidates':
            result = {'candidates': candidate_inventory()}
            if args.codex_executable:
                p = subprocess.run([args.codex_executable, '--version'], capture_output=True, timeout=10)
                result['cli_version_check'] = {'exit_code': p.returncode,
                                               'version': p.stdout.decode(errors='replace').strip()[:120],
                                               'account_isolation_verified': False}
            emit(result);return 0
        with open_study(args.study) as study:
            if args.command == 'status':
                emit({'calls': len(study.db.calls()), 'max_calls': study.config['max_calls'],
                      'identity_bound': study.db.metadata('identity') is not None,
                      'run_lock': (study.directory / 'run.lock').exists()})
            elif args.command == 'request-diff':
                payload = initial_payload(study.tasks[0], study.config['model'], {'effort': study.config['effort']})
                payload['tools'] = study.tasks[0]['tools'];payload['include'] = ['reasoning.encrypted_content']
                emit({'shared_body_first_task': payload, 'route_difference': {
                    'sub2api': '127.0.0.1:18080/v1/responses; temporary local key',
                    'codex_http': 'chatgpt.com/backend-api/codex/responses; selected OAuth + account ID + beta header'},
                    'limitation': 'Comparison is before Sub2API internal forwarding transformations.'})
            elif args.command == 'report':
                r = write_report(study);emit({'report': str(study.directory.resolve() / 'report.md'), 'conclusion': r['conclusion']})
            else:
                if args.command == 'run' and args.approve_calls != study.config['max_calls']:
                    raise ValueError('Explicit call authorization must equal frozen study max_calls')
                from .local import LocalAccountSource
                from .runner import bind, run
                source = LocalAccountSource(study.config)
                def record_cleanup(record):
                    study.db.connection.execute("INSERT INTO metadata(key,value) VALUES('cleanup',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(record),))
                    study.db.connection.commit()
                source.on_cleanup = record_cleanup
                try:
                    if args.command == 'inventory':
                        with study.db.exclusive_run():
                            emit({'identity': bind(study, source), 'candidates': candidate_inventory()})
                    else:
                        import httpx
                        from .transport import RouteTransport
                        async def execute():
                            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                                         timeout=study.config['timeout_seconds']) as client:
                                return await run(study, source, RouteTransport(source, client))
                        result = asyncio.run(execute())
                        emit(result)
                        write_report(study)
                finally:
                    source.close()
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # Exception text from HTTP/database/OS objects may contain secrets.
        emit({'error': type(exc).__name__, 'action': 'Inspect frozen configuration, binding, ledger and run.lock; no automatic retry.'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
