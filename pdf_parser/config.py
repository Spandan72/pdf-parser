from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import tomllib


DEFAULT_CONFIG: dict[str, Any] = {
    "input_dir": "input",
    "output_dir": "output",
    "table_strategy": "lines_strict",
    "table_fallback_strategy": "lines",
    "log_level": "INFO",
}


def load_config(config_path: Path) -> dict[str, Any]:
    """Load config from a TOML file, merging over defaults.

    The TOML file may have a top-level ``[parser]`` section (for compatibility
    with the original tender project layout) or flat keys at the root level.
    Either form is accepted.
    """
    merged = copy.deepcopy(DEFAULT_CONFIG)
    if not config_path.exists():
        return merged

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))

    # Accept both flat-root and [parser]-section layouts.
    section_data: dict[str, Any] = {}
    if "parser" in data and isinstance(data["parser"], dict):
        section_data = data["parser"]
    else:
        section_data = {k: v for k, v in data.items() if not isinstance(v, dict)}

    for key, value in section_data.items():
        merged[key] = value

    return merged
