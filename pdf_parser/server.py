"""FastAPI server for the PDF parser.

Endpoints
---------
GET  /health
    Liveness check — returns ``{"status": "ok"}``.

POST /parse/upload
    Upload one or more PDF files, parse them on-the-fly, and return the
    combined Markdown + table-structure JSON in a single response.

    Form fields:
        files          — one or more PDF files (multipart/form-data)
        table_strategy      — optional, default "lines_strict"
        table_fallback      — optional, default "lines"

POST /parse/folder
    Parse all ``*.pdf`` files already present at *input_dir* on the server
    and write outputs to *output_dir*.

    JSON body::

        {
            "input_dir": "/absolute/or/relative/path",
            "output_dir": "/absolute/or/relative/path",   // optional
            "table_strategy": "lines_strict",             // optional
            "table_fallback_strategy": "lines"            // optional
        }

    Returns a summary dict (the content of ``parse_summary.json``).
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pdf_parser.pipeline import parse_pdf_folder, parse_pdf_to_markdown

logger = logging.getLogger(__name__)

app = FastAPI(
    title="PDF Parser",
    description="Convert PDF files to structured Markdown and table JSON.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FolderParseRequest(BaseModel):
    input_dir: str
    output_dir: str = "output"
    table_strategy: str = "lines_strict"
    table_fallback_strategy: str = "lines"


class ParsedFileResult(BaseModel):
    filename: str
    pages: int
    tables: int
    text_blocks: int
    text_chars: int
    markdown: str
    table_structure: dict[str, Any]


class UploadParseResponse(BaseModel):
    pdf_count: int
    table_strategy: str
    table_fallback_strategy: str
    files: list[ParsedFileResult]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/parse/upload", response_model=UploadParseResponse, tags=["parse"])
async def parse_upload(
    files: Annotated[list[UploadFile], File(description="PDF file(s) to parse")],
    table_strategy: Annotated[str, Form()] = "lines_strict",
    table_fallback: Annotated[str, Form()] = "lines",
) -> UploadParseResponse:
    """Upload PDF(s) and receive parsed Markdown + table structure in the response."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    results: list[ParsedFileResult] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for upload in files:
            if not (upload.filename or "").lower().endswith(".pdf"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Only PDF files are accepted. Got: {upload.filename!r}",
                )
            dest = tmp_path / (upload.filename or "upload.pdf")
            dest.write_bytes(await upload.read())

            try:
                markdown, stats, table_structure = parse_pdf_to_markdown(
                    pdf_path=dest,
                    table_strategy=table_strategy,
                    table_fallback_strategy=table_fallback,
                )
            except Exception as exc:
                logger.exception("Failed to parse %s", upload.filename)
                raise HTTPException(
                    status_code=500,
                    detail=f"Parse error for {upload.filename!r}: {exc}",
                ) from exc

            results.append(
                ParsedFileResult(
                    filename=stats.pdf_file,
                    pages=stats.pages,
                    tables=stats.tables,
                    text_blocks=stats.text_blocks,
                    text_chars=stats.text_chars,
                    markdown=markdown,
                    table_structure=table_structure,
                )
            )

    return UploadParseResponse(
        pdf_count=len(results),
        table_strategy=table_strategy,
        table_fallback_strategy=table_fallback,
        files=results,
    )


@app.post("/parse/folder", tags=["parse"])
def parse_folder(req: FolderParseRequest) -> dict[str, Any]:
    """Parse all PDFs in *input_dir* on the server and return the summary JSON."""
    input_dir = Path(req.input_dir)
    output_dir = Path(req.output_dir)

    if not input_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"input_dir not found: {input_dir}",
        )

    try:
        parse_pdf_folder(
            input_dir=input_dir,
            output_dir=output_dir,
            table_strategy=req.table_strategy,
            table_fallback_strategy=req.table_fallback_strategy,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("parse_pdf_folder failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    summary_path = output_dir / "parse_summary.json"
    if summary_path.exists():
        return json.loads(summary_path.read_text(encoding="utf-8"))
    return {"status": "complete", "output_dir": str(output_dir.resolve())}
