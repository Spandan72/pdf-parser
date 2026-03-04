"""Core PDF parsing pipeline.

Public API
----------
parse_pdf_to_markdown(pdf_path, table_strategy, table_fallback_strategy)
    Parse a single PDF file into Markdown + table structure JSON.

parse_pdf_folder(input_dir, output_dir, table_strategy, table_fallback_strategy,
                 progress_callback)
    Parse every PDF inside *input_dir* and write all outputs to *output_dir*.
"""

from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Rect = tuple[float, float, float, float]

logger = logging.getLogger(__name__)

# Fields that are simple integer counters (used for package-level aggregation).
QUALITY_COUNT_FIELDS = [
    "pages",
    "text_blocks",
    "text_chars",
    "pages_with_tables",
    "table_count",
    "total_cells",
    "cells_with_bbox",
    "cells_without_bbox",
    "non_empty_cells",
    "empty_cells",
    "non_empty_cell_text_chars",
    "tables_with_null_bbox_cells",
    "tables_with_cell_count_mismatch",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ParseStats:
    """Summary statistics for a single parsed PDF."""

    pdf_file: str
    pages: int
    tables: int
    text_blocks: int
    text_chars: int


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def configure_logging(level_name: str) -> None:
    """Configure the root logger to *level_name* (e.g. ``"INFO"``)."""
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
    root_logger.setLevel(level)


def attach_log_file(output_dir: Path) -> None:
    """Add a ``FileHandler`` writing to *output_dir/parse.log* (idempotent)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "parse.log"
    root_logger = logging.getLogger()
    resolved_target = str(log_path.resolve())
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            if str(getattr(handler, "baseFilename", "")) == resolved_target:
                return

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setLevel(root_logger.level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    root_logger.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def clean_text_block(text: str) -> str:
    """Strip null bytes, normalise line endings, trim blank leading/trailing lines."""
    text = text.replace("\x00", "")
    text = text.replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def cell_to_html(value: Any) -> str:
    """Escape *value* for embedding inside an HTML ``<td>``."""
    if value is None:
        return ""
    text = str(value).replace("\x00", "").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")
    return html.escape(text).replace("\n", "<br/>")


def rows_to_html_table(rows: list[list[Any]]) -> str:
    """Render a list-of-rows as a simple HTML ``<table>``."""
    lines = ["<table>"]
    for row in rows:
        lines.append("  <tr>")
        if row:
            for cell in row:
                lines.append(f"    <td>{cell_to_html(cell)}</td>")
        else:
            lines.append("    <td></td>")
        lines.append("  </tr>")
    lines.append("</table>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def rect_to_list(rect: Rect) -> list[float]:
    """Round a 4-float rect to 3 decimal places and return as a list."""
    return [
        round(float(rect[0]), 3),
        round(float(rect[1]), 3),
        round(float(rect[2]), 3),
        round(float(rect[3]), 3),
    ]


def any_rect_to_tuple(value: Any) -> Rect | None:
    """Coerce a fitz.Rect / list / tuple to a plain ``(x0, y0, x1, y1)`` tuple."""
    if value is None:
        return None
    if isinstance(value, fitz.Rect):
        return (float(value.x0), float(value.y0), float(value.x1), float(value.y1))
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
        except Exception:
            return None
    return None


def rects_intersect(a: Rect, b: Rect, pad: float = 0.5) -> bool:
    """Return ``True`` when rectangles *a* and *b* overlap (with *pad* tolerance)."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (
        (ax1 + pad) < bx0 or (bx1 + pad) < ax0 or (ay1 + pad) < by0 or (by1 + pad) < ay0
    )


# ---------------------------------------------------------------------------
# Quality metrics
# ---------------------------------------------------------------------------


def _init_quality_counts(
    pages: int,
    text_blocks: int,
    text_chars: int,
) -> dict[str, int]:
    return {
        "pages": pages,
        "text_blocks": text_blocks,
        "text_chars": text_chars,
        "pages_with_tables": 0,
        "table_count": 0,
        "total_cells": 0,
        "cells_with_bbox": 0,
        "cells_without_bbox": 0,
        "non_empty_cells": 0,
        "empty_cells": 0,
        "non_empty_cell_text_chars": 0,
        "tables_with_null_bbox_cells": 0,
        "tables_with_cell_count_mismatch": 0,
    }


def _finalize_quality_metrics(counts: dict[str, int]) -> dict[str, Any]:
    pages = counts["pages"]
    table_count = counts["table_count"]
    total_cells = counts["total_cells"]
    non_empty_cells = counts["non_empty_cells"]

    return {
        **counts,
        "pages_with_tables_ratio": round(
            counts["pages_with_tables"] / pages if pages else 0.0,
            4,
        ),
        "cells_with_bbox_ratio": round(
            counts["cells_with_bbox"] / total_cells if total_cells else 0.0,
            4,
        ),
        "non_empty_cell_ratio": round(
            non_empty_cells / total_cells if total_cells else 0.0,
            4,
        ),
        "avg_cells_per_table": round(
            total_cells / table_count if table_count else 0.0,
            3,
        ),
        "avg_chars_per_non_empty_cell": round(
            counts["non_empty_cell_text_chars"] / non_empty_cells
            if non_empty_cells
            else 0.0,
            3,
        ),
        "text_blocks_per_page": round(
            counts["text_blocks"] / pages if pages else 0.0,
            3,
        ),
        "text_chars_per_page": round(
            counts["text_chars"] / pages if pages else 0.0,
            3,
        ),
    }


def _compute_quality_metrics(
    table_structure: dict[str, Any],
    pages: int,
    text_blocks: int,
    text_chars: int,
) -> dict[str, Any]:
    counts = _init_quality_counts(
        pages=pages,
        text_blocks=text_blocks,
        text_chars=text_chars,
    )

    for page in table_structure.get("pages", []):
        tables = page.get("tables", [])
        if tables:
            counts["pages_with_tables"] += 1

        for table in tables:
            counts["table_count"] += 1
            row_count = int(table.get("row_count") or 0)
            col_count = int(table.get("col_count") or 0)
            cells = table.get("cells", []) or []

            expected = (
                row_count * col_count if row_count > 0 and col_count > 0 else None
            )
            if expected is not None and expected != len(cells):
                counts["tables_with_cell_count_mismatch"] += 1

            has_missing_bbox = False
            for cell in cells:
                counts["total_cells"] += 1
                bbox = cell.get("bbox")
                if isinstance(bbox, list) and len(bbox) == 4:
                    counts["cells_with_bbox"] += 1
                else:
                    counts["cells_without_bbox"] += 1
                    has_missing_bbox = True

                text = str(cell.get("text") or "").strip()
                if text:
                    counts["non_empty_cells"] += 1
                    counts["non_empty_cell_text_chars"] += len(text)
                else:
                    counts["empty_cells"] += 1

            if has_missing_bbox:
                counts["tables_with_null_bbox_cells"] += 1

    return _finalize_quality_metrics(counts)


# ---------------------------------------------------------------------------
# Table extraction
# ---------------------------------------------------------------------------


def _extract_table_cells(
    page: fitz.Page,
    table: Any,
    rows: list[list[Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Extract per-cell bounding boxes and text from a PyMuPDF table object."""
    cell_items: list[dict[str, Any]] = []

    row_count = len(rows)
    col_count = max((len(r) for r in rows), default=0)

    raw_row_count = getattr(table, "row_count", None)
    raw_col_count = getattr(table, "col_count", None)
    if isinstance(raw_row_count, int) and raw_row_count > row_count:
        row_count = raw_row_count
    if isinstance(raw_col_count, int) and raw_col_count > col_count:
        col_count = raw_col_count

    table_rows = getattr(table, "rows", None)
    if not table_rows:
        # No bbox info available — record text only.
        for r_idx, row in enumerate(rows):
            for c_idx, raw_text in enumerate(row):
                text = clean_text_block("" if raw_text is None else str(raw_text))
                cell_items.append(
                    {"row": r_idx, "col": c_idx, "bbox": None, "text": text}
                )
        return cell_items, row_count, col_count

    for r_idx, row_obj in enumerate(table_rows):
        raw_cells = getattr(row_obj, "cells", None) or []
        if len(raw_cells) > col_count:
            col_count = len(raw_cells)

        for c_idx, raw_bbox in enumerate(raw_cells):
            bbox = any_rect_to_tuple(raw_bbox)
            text = ""
            if r_idx < len(rows) and c_idx < len(rows[r_idx]):
                value = rows[r_idx][c_idx]
                text = "" if value is None else str(value)
            if not text and bbox is not None:
                try:
                    text = page.get_textbox(fitz.Rect(*bbox))
                except Exception:
                    text = ""
            text = clean_text_block(text)
            cell_items.append(
                {
                    "row": r_idx,
                    "col": c_idx,
                    "bbox": rect_to_list(bbox) if bbox is not None else None,
                    "text": text,
                }
            )

    if len(table_rows) > row_count:
        row_count = len(table_rows)

    return cell_items, row_count, col_count


def _extract_page_tables(
    page: fitz.Page,
    primary_strategy: str,
    fallback_strategy: str,
) -> list[dict[str, Any]]:
    """Try *primary_strategy* first; fall back to *fallback_strategy* if no tables found."""
    extracted: list[dict[str, Any]] = []
    strategies = [primary_strategy]
    if fallback_strategy and fallback_strategy != primary_strategy:
        strategies.append(fallback_strategy)

    for strategy in strategies:
        try:
            finder = page.find_tables(strategy=strategy)
        except Exception:
            continue

        extracted = []
        for idx, table in enumerate(finder.tables, start=1):
            rows = table.extract() or []
            if not rows:
                continue
            bbox = tuple(float(v) for v in table.bbox)
            cells, row_count, col_count = _extract_table_cells(page, table, rows)
            extracted.append(
                {
                    "index": idx,
                    "bbox": bbox,
                    "rows": rows,
                    "row_count": row_count,
                    "col_count": col_count,
                    "cells": cells,
                    "strategy": strategy,
                }
            )

        if extracted:
            return extracted

    return extracted


def _extract_page_text_blocks(
    page: fitz.Page,
    table_bboxes: list[Rect],
) -> list[dict[str, Any]]:
    """Return text blocks from *page* that don't overlap with any table bbox."""
    blocks: list[dict[str, Any]] = []
    for block in page.get_text("blocks", sort=True):
        if len(block) < 5:
            continue

        x0, y0, x1, y1, text = block[:5]
        if not isinstance(text, str):
            continue

        cleaned = clean_text_block(text)
        if not cleaned.strip():
            continue

        bbox: Rect = (float(x0), float(y0), float(x1), float(y1))
        if any(rects_intersect(bbox, tb) for tb in table_bboxes):
            continue

        blocks.append({"bbox": bbox, "text": cleaned})

    return blocks


# ---------------------------------------------------------------------------
# Page → Markdown
# ---------------------------------------------------------------------------


def _page_to_markdown(
    page: fitz.Page,
    page_number: int,
    table_strategy: str,
    table_fallback_strategy: str,
) -> tuple[str, int, int, int, dict[str, Any]]:
    """Convert a single page to Markdown and return extraction counts + struct."""
    tables = _extract_page_tables(page, table_strategy, table_fallback_strategy)
    table_bboxes: list[Rect] = [t["bbox"] for t in tables]
    text_blocks = _extract_page_text_blocks(page, table_bboxes)

    # Merge text blocks and tables sorted by (y0, x0) — reading order.
    items: list[tuple[str, float, float, dict[str, Any]]] = []
    for block in text_blocks:
        x0, y0, _, _ = block["bbox"]
        items.append(("text", y0, x0, block))
    for table in tables:
        x0, y0, _, _ = table["bbox"]
        items.append(("table", y0, x0, table))
    items.sort(key=lambda e: (e[1], e[2]))

    lines: list[str] = [f"## Page {page_number}"]
    table_count = 0
    text_block_count = 0
    text_chars = 0

    for kind, _, _, payload in items:
        lines.append("")
        if kind == "text":
            text_block_count += 1
            block_text = payload["text"]
            text_chars += len(block_text)
            lines.append(f"### Text Block {text_block_count}")
            lines.append("")
            lines.append("```text")
            lines.append(block_text)
            lines.append("```")
            continue

        table_count += 1
        lines.append(f"### Table {table_count}")
        bbox = payload["bbox"]
        lines.append(
            f"<!-- strategy: {payload['strategy']} | bbox: "
            f"{bbox[0]:.2f}, {bbox[1]:.2f}, {bbox[2]:.2f}, {bbox[3]:.2f} -->"
        )
        lines.append("")
        lines.append(rows_to_html_table(payload["rows"]))

    if not items:
        lines.append("")
        lines.append("_No text or tables extracted from this page._")

    table_structs = [
        {
            "table_index": t["index"],
            "strategy": t["strategy"],
            "table_bbox": rect_to_list(t["bbox"]),
            "row_count": t["row_count"],
            "col_count": t["col_count"],
            "cells": t["cells"],
        }
        for t in tables
    ]

    page_struct = {"page_number": page_number, "tables": table_structs}
    return "\n".join(lines), table_count, text_block_count, text_chars, page_struct


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_pdf_to_markdown(
    pdf_path: Path,
    table_strategy: str = "lines_strict",
    table_fallback_strategy: str = "lines",
) -> tuple[str, ParseStats, dict[str, Any]]:
    """Parse a single PDF into Markdown text, stats, and a table-structure dict.

    Parameters
    ----------
    pdf_path:
        Path to the ``.pdf`` file to parse.
    table_strategy:
        Primary PyMuPDF ``find_tables()`` strategy (default ``"lines_strict"``).
    table_fallback_strategy:
        Fallback strategy tried when the primary finds no tables (default ``"lines"``).

    Returns
    -------
    markdown : str
        Full Markdown text.  Each page is headed ``## Page N``.  Text blocks are
        wrapped in `` ```text ``` `` fences; tables are rendered as HTML ``<table>``.
    stats : ParseStats
        Page / table / text-block / char counts.
    table_structure : dict
        Structured representation of every table (bbox + per-cell data).
    """
    pdf_started = time.perf_counter()
    logger.info("Parsing PDF: %s", pdf_path.name)
    lines: list[str] = [f"# Source: {pdf_path.name}"]
    total_tables = 0
    total_text_blocks = 0
    total_text_chars = 0
    page_structs: list[dict[str, Any]] = []

    doc = fitz.open(pdf_path)
    page_count = 0
    try:
        page_count = doc.page_count
        for page_idx in range(page_count):
            page = doc.load_page(page_idx)
            page_md, tables, text_blocks, text_chars, page_struct = _page_to_markdown(
                page,
                page_idx + 1,
                table_strategy,
                table_fallback_strategy,
            )
            lines.append("")
            lines.append(page_md)
            total_tables += tables
            total_text_blocks += text_blocks
            total_text_chars += text_chars
            page_structs.append(page_struct)
            logger.debug(
                "PDF %s page %d/%d | tables=%d text_blocks=%d text_chars=%d",
                pdf_path.name,
                page_idx + 1,
                page_count,
                tables,
                text_blocks,
                text_chars,
            )
    finally:
        doc.close()

    elapsed = time.perf_counter() - pdf_started
    logger.info(
        "Parsed %s | pages=%d tables=%d text_blocks=%d text_chars=%d time=%.2fs",
        pdf_path.name,
        page_count,
        total_tables,
        total_text_blocks,
        total_text_chars,
        elapsed,
    )

    stats = ParseStats(
        pdf_file=pdf_path.name,
        pages=page_count,
        tables=total_tables,
        text_blocks=total_text_blocks,
        text_chars=total_text_chars,
    )
    table_structure: dict[str, Any] = {
        "pdf_file": pdf_path.name,
        "table_strategy": table_strategy,
        "table_fallback_strategy": table_fallback_strategy,
        "pages": page_structs,
    }

    return "\n".join(lines).strip() + "\n", stats, table_structure


def parse_pdf_folder(
    input_dir: Path,
    output_dir: Path,
    table_strategy: str = "lines_strict",
    table_fallback_strategy: str = "lines",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Parse every ``*.pdf`` in *input_dir* and write outputs to *output_dir*.

    Output layout
    --------------
    ``parsed_md/<stem>.md``
        Per-PDF Markdown.
    ``parsed_struct/<stem>.tables.json``
        Per-PDF table-structure JSON.
    ``package_parsed.md``
        All PDFs concatenated into one Markdown document.
    ``package_tables.json``
        Combined table structure for all PDFs.
    ``parse_summary.json``
        Per-PDF stats + quality metrics and a package-level rollup.
    ``parse.log``
        Full log of the run.

    Parameters
    ----------
    progress_callback:
        Optional callable invoked after each PDF with a dict::

            {
                "phase": "parsing",
                "completed": <int>,   # PDFs done so far
                "total": <int>,       # total PDFs
                "pdf_file": <str>,    # current PDF filename
            }
    """
    run_started = time.perf_counter()
    attach_log_file(output_dir)
    logger.info(
        "Parser run started | input_dir=%s output_dir=%s strategy=%s fallback=%s",
        input_dir,
        output_dir,
        table_strategy,
        table_fallback_strategy,
    )

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"No PDF files found in {input_dir}")
    logger.info("Found %d PDF file(s)", len(pdf_files))

    if progress_callback is not None:
        try:
            progress_callback(
                {
                    "phase": "parsing",
                    "completed": 0,
                    "total": len(pdf_files),
                    "pdf_file": "",
                }
            )
        except Exception:
            pass

    parsed_dir = output_dir / "parsed_md"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    parsed_struct_dir = output_dir / "parsed_struct"
    parsed_struct_dir.mkdir(parents=True, exist_ok=True)

    package_lines: list[str] = ["# Parsed Package"]
    package_struct_rows: list[dict[str, Any]] = []
    package_quality_counts = _init_quality_counts(pages=0, text_blocks=0, text_chars=0)
    summary_rows: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []

    for idx, pdf_path in enumerate(pdf_files, start=1):
        logger.info("Processing PDF %d/%d: %s", idx, len(pdf_files), pdf_path.name)
        md_text, stats, table_structure = parse_pdf_to_markdown(
            pdf_path,
            table_strategy,
            table_fallback_strategy,
        )

        per_pdf_md_path = parsed_dir / f"{pdf_path.stem}.md"
        per_pdf_md_path.write_text(md_text, encoding="utf-8")

        per_pdf_struct_path = parsed_struct_dir / f"{pdf_path.stem}.tables.json"
        per_pdf_struct_path.write_text(
            json.dumps(table_structure, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

        package_lines.append("")
        package_lines.append(md_text)
        package_struct_rows.append(table_structure)

        quality = _compute_quality_metrics(
            table_structure=table_structure,
            pages=stats.pages,
            text_blocks=stats.text_blocks,
            text_chars=stats.text_chars,
        )
        for key in QUALITY_COUNT_FIELDS:
            package_quality_counts[key] += int(quality[key])
        quality_rows.append({"pdf_file": stats.pdf_file, **quality})

        summary_rows.append(
            {
                "pdf_file": stats.pdf_file,
                "pages": stats.pages,
                "tables": stats.tables,
                "text_blocks": stats.text_blocks,
                "text_chars": stats.text_chars,
                "markdown_path": str(per_pdf_md_path),
                "table_structure_path": str(per_pdf_struct_path),
                "quality": quality,
            }
        )
        logger.info(
            "Saved outputs for %s | md=%s json=%s",
            stats.pdf_file,
            per_pdf_md_path,
            per_pdf_struct_path,
        )

        if progress_callback is not None:
            try:
                progress_callback(
                    {
                        "phase": "parsing",
                        "completed": idx,
                        "total": len(pdf_files),
                        "pdf_file": stats.pdf_file,
                    }
                )
            except Exception:
                pass

    # --- Package-level outputs -----------------------------------------------
    package_md_path = output_dir / "package_parsed.md"
    package_md_path.write_text(
        "\n".join(package_lines).strip() + "\n", encoding="utf-8"
    )

    package_tables_path = output_dir / "package_tables.json"
    package_tables_path.write_text(
        json.dumps(
            {
                "input_dir": str(input_dir),
                "pdf_count": len(pdf_files),
                "table_strategy": table_strategy,
                "table_fallback_strategy": table_fallback_strategy,
                "pdfs": package_struct_rows,
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    summary: dict[str, Any] = {
        "input_dir": str(input_dir),
        "pdf_count": len(pdf_files),
        "table_strategy": table_strategy,
        "table_fallback_strategy": table_fallback_strategy,
        "package_markdown": str(package_md_path),
        "package_tables": str(package_tables_path),
        "quality": {
            "package": _finalize_quality_metrics(package_quality_counts),
            "per_pdf": quality_rows,
        },
        "pdfs": summary_rows,
    }
    (output_dir / "parse_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    logger.info(
        "Parser run complete in %.2fs | pdfs=%d pages=%d tables=%d text_blocks=%d",
        time.perf_counter() - run_started,
        len(pdf_files),
        package_quality_counts["pages"],
        package_quality_counts["table_count"],
        package_quality_counts["text_blocks"],
    )
    logger.info(
        "Outputs | package_md=%s package_tables=%s summary=%s",
        package_md_path,
        package_tables_path,
        output_dir / "parse_summary.json",
    )
