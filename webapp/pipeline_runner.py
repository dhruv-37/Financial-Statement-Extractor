"""
pipeline_runner.py — runs Step1 + Step2 for one uploaded PDF using a
caller-supplied Gemini API key, with strict cleanup:

- The API key is a plain local variable for the duration of one request.
  It is never written to disk, never logged, never put in a session/cookie,
  and is deleted (`del`) as soon as the pipeline call returns.
- All intermediate files (uploaded PDF, trimmed PDF, xlsx, taxonomy json,
  the two Gemini SQLite caches) live in a per-request temp directory that
  is deleted unconditionally in a `finally` block.
- The finished zip (trimmed PDF + xlsx) is built in memory BEFORE the temp
  directory is deleted, so nothing lingers on disk after the response is
  prepared.

Progress reporting: `progress_cb(percent, message)` is called at each real
stage boundary — these are actual completed steps, not a time-based fake
animation. Pass a no-op lambda if you don't need progress.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable

from pipeline.Step1 import extract_core_financial_statements
from pipeline.Step2 import extract_financials
import summary as owner_summary

MAX_UPLOAD_BYTES = 40 * 1024 * 1024  # 40 MB

ProgressCB = Callable[[int, str], None]
SummaryCB = Callable[[list | None], None]


class PipelineError(Exception):
    pass


def _noop(_pct: int, _msg: str) -> None:
    pass


def _noop_summary(_bullets) -> None:
    pass


def _generate_summary_bg(
    trimmed_pdf_path: str, api_key: str, summary_cb: SummaryCB, progress_cb: ProgressCB
) -> None:
    """Runs in a background thread using the VISITOR'S OWN key (the same
    one used for the real extraction), and never raises — worst case it
    calls back with None."""
    progress_cb(60, "Generating quick-take summary…")
    try:
        import fitz
        doc = fitz.open(trimmed_pdf_path)
        text = "\n".join(doc[i].get_text("text") for i in range(len(doc)))
        doc.close()
        bullets = owner_summary.generate(text, api_key)
    except Exception:  # noqa: BLE001
        bullets = None
    if bullets:
        progress_cb(60, "Quick-take summary ready.")
    else:
        progress_cb(60, "Quick-take summary skipped.")
    summary_cb(bullets)


def run(
    pdf_bytes: bytes,
    ticker: str,
    api_key: str,
    progress_cb: ProgressCB = _noop,
    summary_cb: SummaryCB = _noop_summary,
) -> tuple[bytes, str]:
    """
    Returns (zip_bytes, filename) — the zip contains both the trimmed PDF
    and the finished .xlsx. Raises PipelineError on any failure. Guaranteed
    not to leave files or the api_key behind, success or failure.
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

        progress_cb(3, "Saving upload…")
        with open(src_pdf, "wb") as f:
            f.write(pdf_bytes)

        # Isolate Step1's Phase-1 cache per request. Step2's own JSON cache
        # (pipeline/../output/step2_cache/) is process-wide by design, but
        # it's keyed on sha256(model + prompt + extracted PDF text) — never
        # on the api_key — so sharing it across users is safe; it just needs
        # periodic pruning in production (see deploy README).
        cachelite_path = os.path.join(tmpdir, "gemini_cachelite.sqlite3")

        try:
            progress_cb(10, "Scanning pages for the auditor's signature…")
            extract_core_financial_statements(
                src_pdf, trimmed_pdf, api_key,
                use_cachelite=True, cachelite_path=cachelite_path,
            )
            progress_cb(55, "Statements found — parsing figures with Gemini…")

            # Bonus "quick take" using the VISITOR's own key, in parallel
            # with the real extraction below — never allowed to slow down
            # or fail the paid part.
            summary_thread = threading.Thread(
                target=_generate_summary_bg,
                args=(trimmed_pdf, api_key, summary_cb, progress_cb),
                daemon=True,
            )
            summary_thread.start()

            extract_financials(
                trimmed_pdf, output_xlsx, api_key=api_key,
            )
            progress_cb(90, "Building workbook…")
            summary_thread.join(timeout=20)  # don't let a slow bonus call hold up finishing
        except Exception as exc:  # noqa: BLE001
            # Never let a raw exception (which could echo request internals)
            # bubble to the browser; log server-side only, message must not
            # be able to contain the api_key (none of our call sites embed
            # it in an exception string — Step2/Step1 raise on HTTP/JSON
            # errors using status codes and response bodies only).
            raise PipelineError(f"Extraction failed: {exc}") from exc

        if not os.path.exists(output_xlsx):
            raise PipelineError("Pipeline finished but produced no output — try again.")
        if not os.path.exists(trimmed_pdf):
            raise PipelineError("Pipeline finished but the trimmed PDF is missing — try again.")

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(output_xlsx, arcname=f"{safe_ticker}.xlsx")
            zf.write(trimmed_pdf, arcname=f"{safe_ticker}_trimmed.pdf")
        zip_bytes = zip_buf.getvalue()

        # Verify before ever handing this back — a silently-empty or
        # truncated zip must surface as a clear error, never as a "success"
        # that downloads nothing.
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as check_zf:
            bad_entry = check_zf.testzip()
            if bad_entry is not None:
                raise PipelineError(f"Built zip is corrupt (bad entry: {bad_entry}) — try again.")
            names = set(check_zf.namelist())
            expected = {f"{safe_ticker}.xlsx", f"{safe_ticker}_trimmed.pdf"}
            if names != expected:
                raise PipelineError(
                    f"Built zip is missing expected files (found: {sorted(names) or 'none'}) — try again."
                )

        progress_cb(100, "Done.")
        return zip_bytes, f"{safe_ticker}.zip"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        del api_key  # best-effort — makes intent explicit even though GC timing isn't guaranteed