from __future__ import annotations

import os
import argparse

from .app import create_app
from .env import load_env_file
from .ssl_config import ssl_context_from_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the payment link workbench")
    parser.add_argument("--env-file", help="path to a .env file")
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file, required=True)
        os.environ["OPLL_ENV_FILE"] = args.env_file
    app = create_app()
    app.run(
        host=os.getenv("OPLL_WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("OPLL_WEB_PORT", "5000")),
        threaded=True,
        ssl_context=ssl_context_from_config(app.config),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
