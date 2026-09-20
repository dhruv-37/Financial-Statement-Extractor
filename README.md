# AR Due Diligence — Step 1 + Step 2 only (slim build)

This is a trimmed-down copy of `ar_due_diligence` containing **only** what's
needed to go from a source annual-report PDF to:

1. **Trimmed PDF** — the core financial-statement pages only (Step 1)
2. **Excel workbook (.xlsx)** — parsed financials, plus a taxonomy JSON (Step 2)

Everything downstream (schema normalization / Step 3, the memo/narrative/
red-flag agents, the forensic engine, the server, `main.py`, etc.) has been
removed since it isn't needed for this output.

## Files

```
run_pipeline.py               CLI entry point — runs Step 1 then Step 2
requirements.txt              Only the packages actually imported below
pipeline/
  Step1.py                    Phase 1-3: locates auditor-signature pages,
                               classifies BS/PL/CF/EQ pages via Gemini,
                               assembles the trimmed PDF
  phase1_filter_batch.py      Step1 dependency — auditor-signature page finder
  phase2_llm_classify.py      Step1 dependency — Gemini page classification
  Step2.py                    Parses the trimmed PDF and writes the .xlsx
                               + a "<ticker>_taxonomy.json"
  taxonomy.py                 Step2 dependency — taxonomy definitions
  taxonomy_mapper.py          Step2 dependency — fuzzy line-item mapping
  fs_dictionary.py            taxonomy_mapper.py dependency
```

## Setup

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your_key_here
```

## Usage

```bash
python run_pipeline.py "path/to/Annual Report.pdf" TICKER
```

Output (written to `output/` by default):

```
output/TICKER_trimmed.pdf        Step 1 output
output/TICKER.xlsx               Step 2 output
output/TICKER_taxonomy.json      Step 2's structured line-item JSON
```

Optional: `--out-dir <dir>` to change the output directory.

## Note

`Step2.py` constructs its Gemini client (`google_genai.Client(...)`) at
**import time**, reading `GEMINI_API_KEY` from the environment — this is
inherited from the original repo's design, not something changed here.
Make sure the environment variable is set before running.
