# pdf-parser

Standalone PDF parser that converts PDF files into structured Markdown and table JSON using [PyMuPDF](https://pymupdf.readthedocs.io/).

Each page is rendered in reading order — text blocks as fenced code blocks, tables as inline HTML `<table>` elements — producing Markdown that is both human-readable and easy to parse programmatically.

---

## Features

- **Text extraction** — extracts text blocks per page, sorted by reading order `(y, x)`
- **Table detection** — uses PyMuPDF `find_tables()` with a configurable primary strategy and automatic fallback
- **HTML tables** — every detected table is rendered as an HTML `<table>` embedded in the Markdown
- **Table structure JSON** — per-cell bounding boxes and text saved alongside the Markdown
- **Quality metrics** — `parse_summary.json` includes per-PDF and package-level stats (cell counts, bbox coverage, chars per page, etc.)
- **REST API** — upload PDFs over HTTP or parse a server-side folder via FastAPI endpoints
- **CLI** — driven by `config.toml` with full CLI flag overrides

---

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

---

## Installation

```bash
git clone <repo-url> pdf-parser
cd pdf-parser
uv sync
```

Or with pip:

```bash
pip install pymupdf fastapi uvicorn python-multipart
```

---

## Quick Start

### 1. CLI — parse a folder of PDFs

Put your PDF files in `input/` (or point `--input` anywhere), then:

```bash
uv run python parse.py
# or with explicit paths:
uv run python parse.py --input path/to/pdfs --output path/to/output
```

### 2. REST API server

```bash
uv run python serve.py
# custom port:
uv run python serve.py --port 8001 --reload
```

API is now available at `http://localhost:8000`. See [API endpoints](#api-endpoints) below.

### 3. Python API

```python
from pathlib import Path
from pdf_parser import parse_pdf_to_markdown, parse_pdf_folder

# Single file
markdown, stats, table_structure = parse_pdf_to_markdown(Path("document.pdf"))
print(markdown)
print(stats)  # ParseStats(pdf_file=..., pages=..., tables=..., ...)

# Entire folder
parse_pdf_folder(
    input_dir=Path("input"),
    output_dir=Path("output"),
    table_strategy="lines_strict",
    table_fallback_strategy="lines",
)
```

---

## Output Layout

After parsing, `output_dir` contains:

```
output/
├── parsed_md/
│   └── <stem>.md               # per-PDF Markdown
├── parsed_struct/
│   └── <stem>.tables.json      # per-PDF table structure (bbox + cells)
├── package_parsed.md           # all PDFs concatenated
├── package_tables.json         # combined table structure
├── parse_summary.json          # stats + quality metrics
└── parse.log                   # full run log
```

### Markdown format

```markdown
# Source: document.pdf

## Page 1

### Text Block 1

```text
Some extracted text here.
```

### Table 1
<!-- strategy: lines_strict | bbox: 57.00, 120.00, 540.00, 300.00 -->

<table>
  <tr>
    <td>Header 1</td>
    <td>Header 2</td>
  </tr>
  <tr>
    <td>Value A</td>
    <td>Value B</td>
  </tr>
</table>
```

---

## Configuration

Edit `config.toml` to set defaults:

```toml
[parser]
input_dir  = "input"
output_dir = "output"

# Primary PyMuPDF find_tables() strategy.
# Options: "lines_strict" | "lines" | "text" | "explicit"
table_strategy          = "lines_strict"
table_fallback_strategy = "lines"

log_level = "INFO"
```

### CLI overrides

Any config value can be overridden at the command line:

```
parse.py [--config CONFIG] [--input DIR] [--output DIR]
         [--strategy STRATEGY] [--fallback FALLBACK] [--log-level LEVEL]
```

---

## API Endpoints

### `GET /health`

Liveness check.

```json
{"status": "ok"}
```

---

### `POST /parse/upload`

Upload one or more PDF files. Parsed Markdown and table structure are returned directly in the response — nothing is written to disk.

**Request** — `multipart/form-data`

| Field | Type | Default | Description |
|---|---|---|---|
| `files` | file(s) | required | PDF file(s) to parse |
| `table_strategy` | string | `lines_strict` | Primary detection strategy |
| `table_fallback` | string | `lines` | Fallback strategy |

**Example**

```bash
curl -X POST http://localhost:8000/parse/upload \
  -F "files=@document.pdf" \
  -F "table_strategy=lines_strict"
```

**Response**

```json
{
  "pdf_count": 1,
  "table_strategy": "lines_strict",
  "table_fallback_strategy": "lines",
  "files": [
    {
      "filename": "document.pdf",
      "pages": 10,
      "tables": 3,
      "text_blocks": 42,
      "text_chars": 8500,
      "markdown": "# Source: document.pdf\n\n## Page 1\n...",
      "table_structure": { ... }
    }
  ]
}
```

---

### `POST /parse/folder`

Parse a folder of PDFs already present on the server. Outputs are written to `output_dir` and the `parse_summary.json` content is returned.

**Request** — JSON body

```json
{
  "input_dir": "/path/to/pdfs",
  "output_dir": "output",
  "table_strategy": "lines_strict",
  "table_fallback_strategy": "lines"
}
```

**Example**

```bash
curl -X POST http://localhost:8000/parse/folder \
  -H "Content-Type: application/json" \
  -d '{"input_dir": "/path/to/pdfs", "output_dir": "output"}'
```

**Response** — contents of `parse_summary.json` (pdf count, per-PDF stats, quality metrics).

---

## Table Detection Strategies

PyMuPDF `find_tables()` supports several strategies. The parser tries the primary strategy first; if no tables are found on a page it automatically retries with the fallback.

| Strategy | Best for |
|---|---|
| `lines_strict` | PDFs with clear ruled lines (default) |
| `lines` | PDFs with partial or faint lines |
| `text` | Text-only / borderless tables |
| `explicit` | Pass explicit column/row boundaries |

---

## Development

```bash
# Compile check
uv run python -m compileall pdf_parser

# Smoke imports
uv run python -c "from pdf_parser.pipeline import parse_pdf_to_markdown; print('ok')"
uv run python -c "from pdf_parser.server import app; print('ok')"

# Dev server with auto-reload
uv run python serve.py --reload
```
