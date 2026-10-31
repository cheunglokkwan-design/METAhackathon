"""Vercel serverless entrypoint exposing the Margin FastAPI app."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.app import app

__all__ = ["app"]
