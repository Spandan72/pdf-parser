"""HTTP server entry point.

Usage
-----
    uv run python serve.py
    uv run python serve.py --host 0.0.0.0 --port 8001
    uv run python serve.py --config config.toml --port 8001
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from pdf_parser.config import load_config
from pdf_parser.pipeline import configure_logging


def main() -> None:
    p = argparse.ArgumentParser(description="Start the PDF-parser REST API server.")
    p.add_argument("--config", default="config.toml", help="Path to TOML config file.")
    p.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0).")
    p.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    p.add_argument(
        "--reload", action="store_true", help="Enable auto-reload (dev mode)."
    )
    args = p.parse_args()

    cfg = load_config(Path(args.config))
    configure_logging(str(cfg.get("log_level", "INFO")))

    uvicorn.run(
        "pdf_parser.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
