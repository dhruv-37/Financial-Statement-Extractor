"""
run_pipeline.py  (slim build)
==============================
Runs ONLY:
    Step 1  — extract core financial-statement pages -> trimmed PDF
    Step 2  — parse the trimmed PDF -> formatted Excel workbook
                 (+ a taxonomy JSON, written by Step2 itself)

This is a trimmed-down copy of the original repo's run_pipeline.py with
Step 3 (schema normalization) removed, along with everything else in the
repo (agents, forensic_diagnostic_engine, server, main.py, tools/, etc.)
that Step 1 / Step 2 don't need.

Usage:
    python run_pipeline.py "data/pdfs/Tata Consultancy Services Annual Report.pdf" TCS

Optional:
    python run_pipeline.py <pdf> <ticker> [--out-dir output]

Requires the GEMINI_API_KEY environment variable to be set.

Output files (written into --out-dir, default "output/"):
    output/TCS_trimmed.pdf         Step 1's trimmed core-financial-statements PDF
    output/TCS.xlsx                Step 2's formatted Excel workbook
    output/TCS_taxonomy.json       Step 2's structured line-item JSON
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.Step1 import extract_core_financial_statements
from pipeline.Step2 import extract_financials


def main():
    parser = argparse.ArgumentParser(
        description="Run Step 1 (trimmed PDF) + Step 2 (Excel) of the AR due-diligence pipeline."
    )
    parser.add_argument("pdf_path", help="Path to the source annual report PDF")
    parser.add_argument("ticker", help="Ticker / company identifier, e.g. TCS")
    parser.add_argument("--out-dir", default="output", help="Directory for all output files (default: output/)")
    args = parser.parse_args()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        print("\u274c  GEMINI_API_KEY environment variable not set.")
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)
    trimmed_pdf = os.path.join(args.out_dir, f"{args.ticker}_trimmed.pdf")
    output_xlsx = os.path.join(args.out_dir, f"{args.ticker}.xlsx")
    taxonomy_json = os.path.join(args.out_dir, f"{args.ticker}_taxonomy.json")

    print(f"\n\u2500\u2500 Step 1: Extracting core financial pages \u2192 {trimmed_pdf}")
    extract_core_financial_statements(args.pdf_path, trimmed_pdf, gemini_key)

    print(f"\n\u2500\u2500 Step 2: Parsing & building Excel \u2192 {output_xlsx}")
    extract_financials(trimmed_pdf, output_xlsx)
    # extract_financials writes "<output_xlsx-without-ext>_taxonomy.json" itself;
    # that path is exactly `taxonomy_json` computed above.

    print(f"\n\u2705  Done.")
    print(f"    Trimmed PDF: {trimmed_pdf}")
    print(f"    Excel:       {output_xlsx}")
    print(f"    Taxonomy:    {taxonomy_json}")


if __name__ == "__main__":
    main()
