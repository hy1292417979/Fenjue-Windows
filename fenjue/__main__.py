"""Run with: python -m fenjue --config fenjue.local.json"""

from __future__ import annotations

import argparse
import logging

from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="焚决：固定账号的有界求解、复核与修正引擎")
    parser.add_argument("--config", required=True, help="Operator JSON configuration path")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
    except (ValueError, OSError) as error:
        parser.exit(2, f"Configuration error: {error}\n")
    import uvicorn
    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run(
        create_app(settings), host=settings.host, port=settings.port,
        access_log=False, limit_concurrency=settings.max_inflight * 4,
    )


if __name__ == "__main__":
    main()
