from pipeline.phase1_filter_batch import extract_auditor_signatures


def test_page_207_not_dropped_by_numeric_density_gate():
    pdf_path = r"c:\Users\dhruv\Music\ar_slim\uploads\Godrej Properties Annual Report 2025-26.pdf"

    results = extract_auditor_signatures(pdf_path, no_pdf=True)
    page_numbers = {item["page_number"] for item in results}

    assert 207 in page_numbers
