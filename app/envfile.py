"""Load the repo .env for local uvicorn. Docker already injects these variables."""
from __future__ import annotations

import os


def load_local_env() -> None:
    # Real environment variables win. A missing python-dotenv just leaves them unset.
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
