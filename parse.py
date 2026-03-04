"""CLI entry point — parse a folder of PDFs via config.toml.

Usage
-----
    uv run python parse.py
    uv run python parse.py --config config.toml
    uv run python parse.py --input input/ --output output/
    uv run python parse.py --input input/ --output output/ \
        --strategy lines_strict --fallback lines
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pdf_parser.config import DEFAULT_CONFIG, load_config
from pdf_parser.pipeline import configure_logging, parse_pdf_folder


def main() -> None:
    p = argparse.ArgumentParser(
        description="Parse PDF files into Markdown and table-structure JSON."
    )
    p.add_argument(
        "--config",
        default="config.toml",
        help="Path to TOML config file (default: config.toml).",
    )
    p.add_argument(
        "--input",
        dest="input_dir",
        default=None,
        help="Directory containing PDF files. Overrides config.",
    )
    p.add_argument(
        "--output",
        dest="output_dir",
        default=None,
        help="Directory to write outputs. Overrides config.",
    )
    p.add_argument(
        "--strategy",
        default=None,
        help='Table detection strategy (default: "lines_strict"). Overrides config.',
    )
    p.add_argument(
        "--fallback",
        default=None,
        help='Fallback table strategy (default: "lines"). Overrides config.',
    )
    p.add_argument(
        "--log-level",
        default=None,
        help="Logging level (DEBUG, INFO, WARNING). Overrides config.",
    )
    args = p.parse_args()

    # Load config then apply any CLI overrides.
    cfg = load_config(Path(args.config))
    if args.input_dir is not None:
        cfg["input_dir"] = args.input_dir
    if args.output_dir is not None:
        cfg["output_dir"] = args.output_dir
    if args.strategy is not None:
        cfg["table_strategy"] = args.strategy
    if args.fallback is not None:
        cfg["table_fallback_strategy"] = args.fallback
    if args.log_level is not None:
        cfg["log_level"] = args.log_level

    configure_logging(str(cfg.get("log_level", DEFAULT_CONFIG["log_level"])))

    input_dir = Path(str(cfg["input_dir"]))
    output_dir = Path(str(cfg["output_dir"]))
    output_dir.mkdir(parents=True, exist_ok=True)

    parse_pdf_folder(
        input_dir=input_dir,
        output_dir=output_dir,
        table_strategy=str(cfg["table_strategy"]),
        table_fallback_strategy=str(cfg["table_fallback_strategy"]),
    )
    print(f"Done. Outputs written to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
