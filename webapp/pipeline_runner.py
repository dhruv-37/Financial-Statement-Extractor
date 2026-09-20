"""
pipeline_runner.py — runs Step1 + Step2 for one uploaded PDF using a
caller-supplied Gemini API key, with strict cleanup:

- The API key is a plain local variable for the duration of one request.
  It is never written to disk, never logged, never put in a session/cookie,
  and is deleted (`del`) as soon as the pipeline call returns.
- All intermediate files (uploaded PDF, trimmed PDF, xlsx, taxonomy json,
  the two Gemini SQLite caches) live in a per-request temp directory that
  is deleted unconditionally in a `finally` block.
- The finished .xlsx is read into memory BEFORE the temp directory is
  deleted, so nothing lingers on disk after the response is prepared.
"""



from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import io
import os
import shutil
import tempfile
from pathlib import Path

from pipeline.Step1 import extract_core_financial_statements
from pipeline.Step2 import extract_financials

MAX_UPLOAD_BYTES = 40 * 1024 * 1024  # 40 MB


class PipelineError(Exception):
    pass


def run(pdf_bytes: bytes, ticker: str, api_key: str) -> tuple[bytes, str]:
    """
    Returns (xlsx_bytes, filename). Raises PipelineError on any failure.
    Guaranteed not to leave files or the api_key behind, success or failure.
    """
    if not pdf_bytes[:4] == b"%PDF":
        raise PipelineError("That file doesn't look like a PDF.")
    if len(pdf_bytes) > MAX_UPLOAD_BYTES:
        raise PipelineError("File too large (max 40 MB).")
    if not api_key or len(api_key) < 10:
        raise PipelineError("A valid Gemini API key is required.")

    safe_ticker = "".join(ch for ch in ticker if ch.isalnum() or ch in "-_") or "REPORT"

    tmpdir = tempfile.mkdtemp(prefix="ar_run_")
    try:
        src_pdf = os.path.join(tmpdir, "input.pdf")
        trimmed_pdf = os.path.join(tmpdir, f"{safe_ticker}_trimmed.pdf")
        output_xlsx = os.path.join(tmpdir, f"{safe_ticker}.xlsx")

        with open(src_pdf, "wb") as f:
            f.write(pdf_bytes)

        # Isolate Step1's Phase-1 cache per request. Step2's own JSON cache
        # (pipeline/../output/step2_cache/) is process-wide by design, but
        # it's keyed on sha256(model + prompt + extracted PDF text) — never
        # on the api_key — so sharing it across users is safe; it just needs
        # periodic pruning in production (see deploy README).
        cachelite_path = os.path.join(tmpdir, "gemini_cachelite.sqlite3")

        try:
            extract_core_financial_statements(
                src_pdf, trimmed_pdf, api_key,
                use_cachelite=True, cachelite_path=cachelite_path,
            )
            extract_financials(
                trimmed_pdf, output_xlsx, api_key=api_key,
            )
        except Exception as exc:  # noqa: BLE001
            # Never let a raw exception (which could echo request internals)
            # bubble to the browser; log server-side only, message must not
            # be able to contain the api_key (none of our call sites embed
            # it in an exception string — Step2/Step1 raise on HTTP/JSON
            # errors using status codes and response bodies only).
            raise PipelineError(f"Extraction failed: {exc}") from exc

        if not os.path.exists(output_xlsx):
            raise PipelineError("Pipeline finished but produced no output — try again.")

        xlsx_bytes = Path(output_xlsx).read_bytes()
        return xlsx_bytes, f"{safe_ticker}.xlsx"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        del api_key  # best-effort — makes intent explicit even though GC timing isn't guaranteed
