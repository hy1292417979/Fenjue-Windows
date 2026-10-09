"""Single native-client text request using the selected Sub2API account."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from .discovery.config import read
from .discovery.local import LocalAccountSource
from .discovery.native_codex import native_codex_call


async def run(args):
    prompt = Path(args.input).read_text(encoding='utf-8-sig') if args.input else sys.stdin.read()
    if not prompt.strip():
        raise ValueError('Provide a UTF-8 text file with --input, or pipe the task on stdin')
    config = read(args.config)
    source = LocalAccountSource(config)
    try:
        before = source.inventory()
        result = await native_codex_call(source.credentials(), prompt, model=config['model'],
                                        effort=args.effort, timeout=args.timeout)
        same_account = source.inventory()['account_ref'] == before['account_ref']
        print(json.dumps({'route':'native_codex', 'status':result.status,
                          'selected_account_unchanged':same_account,
                          'requested_model':config['model'], 'requested_effort':args.effort,
                          'reported_model':result.model, 'reported_effort':result.effort,
                          'seconds':result.elapsed_seconds}, ensure_ascii=False), file=sys.stderr)
        if not result.ok or not same_account:
            return 1
        print(result.answer)
        return 0
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='screen.proposal.json')
    parser.add_argument('--input', help='UTF-8 task file; otherwise read stdin')
    parser.add_argument('--effort', choices=['low','medium','high','xhigh','max','ultra'], default='high')
    parser.add_argument('--timeout', type=int, default=240, choices=range(1,601), metavar='1..600')
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (OSError, ValueError, KeyError):
        print('Native request configuration failed; check local config and selected account.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
