from __future__ import annotations

from datetime import datetime, timedelta

from pipelines.common.metadata_enrichment import generate_document_id
from pipelines.common.pdf_processing import (
    analyze_pdf_text_quality,
    chunk_pdf_pages_layout,
    clean_pdf_pages,
)


def test_pdf_quality_rejects_garbled_text():
    garbled = "�" * 300 + "\x00\x01\x02"
    quality = analyze_pdf_text_quality(garbled, page_count=5)

    assert quality["quality_status"] == "rejected"
    assert "low_alpha_ratio" in quality["warnings"]


def test_pdf_quality_flags_very_short_text_for_many_pages():
    quality = analyze_pdf_text_quality("short text", page_count=6)

    assert "very_short_text_for_page_count" in quality["warnings"]


def test_clean_pdf_pages_dehyphenates_and_removes_repeated_headers_footers():
    pages = [
        "REGENOVA PROTOCOL\nBPC-157 sup-\nplementation guidance\nPage 1",
        "REGENOVA PROTOCOL\nTB-500 recom-\nmendations\nPage 2",
    ]

    cleaned = clean_pdf_pages(pages)

    assert all("REGENOVA PROTOCOL" not in page for page in cleaned)
    assert all("Page " not in page for page in cleaned)
    assert "supplementation guidance" in cleaned[0]
    assert "recommendations" in cleaned[1]


def test_pdf_chunking_is_layout_aware_for_headings_and_tables():
    pages = [
        "DOSING GUIDE\n\nBPC-157 supports soft tissue healing.\n\nPeptide  Dose  Frequency\nBPC-157  250mcg  Daily"
    ]

    chunks = chunk_pdf_pages_layout(pages, max_tokens=80, overlap=10)
    merged = "\n".join(chunks)

    assert chunks
    assert "DOSING GUIDE" in merged
    assert "[TABLE]" in merged


def test_generate_document_id_is_stable_for_local_file_paths():
    acquired_a = datetime(2025, 1, 1)
    acquired_b = acquired_a + timedelta(days=30)

    first = generate_document_id(None, acquired_a, "doc", file_path="documents/protocol.pdf")
    second = generate_document_id(None, acquired_b, "doc", file_path="documents/protocol.pdf")

    assert first == second


def test_generate_document_id_keeps_url_based_determinism():
    acquired_a = datetime(2025, 1, 1)
    acquired_b = acquired_a + timedelta(days=5)
    url = "https://example.com/protocol"

    assert generate_document_id(url, acquired_a, "x") == generate_document_id(url, acquired_b, "x")
