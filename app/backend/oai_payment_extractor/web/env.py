from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_env_file(path: str | os.PathLike[str], *, required: bool = False) -> bool:
    env_path = Path(path)
    if not env_path.is_file():
        if required:
            raise RuntimeError(f"env file not found: {env_path}")
        return False
    load_dotenv(dotenv_path=env_path, override=False)
    return True


def load_configured_env(env_file: str | None = None) -> None:
    configured = env_file or os.getenv("OPLL_ENV_FILE", "")
    if configured:
        load_env_file(configured, required=True)
    else:
        load_env_file(".env")
