from __future__ import annotations

import sys
from datetime import datetime, timedelta
from types import ModuleType

from pipelines.common.metadata_enrichment import generate_document_id
from pipelines.common.pdf_processing import (
    _extract_pdf_text_with_ocr,
    analyze_pdf_text_quality,
    chunk_pdf_pages_layout,
    clean_pdf_pages,
    extract_pdf_content,
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


def test_extract_pdf_content_prefers_ocr_when_direct_extraction_is_low_quality(monkeypatch):
    monkeypatch.setattr(
        "pipelines.common.pdf_processing._extract_pdf_text_with_pypdf",
        lambda _path: (["���\x00\x01"], []),
    )
    monkeypatch.setattr(
        "pipelines.common.pdf_processing._extract_pdf_text_with_ocr",
        lambda _path, _count: (["Readable protocol text from OCR"], True, []),
    )

    extraction = extract_pdf_content("/tmp/ignored.pdf", ocr_fallback=True)

    assert extraction["ocr_attempted"] is True
    assert extraction["ocr_used"] is True
    assert extraction["extraction_method"] == "ocr"
    assert extraction["quality"]["quality_status"] in {"ok", "warning"}


def test_extract_pdf_content_flags_ocr_unavailable_for_low_quality_pdf(monkeypatch):
    monkeypatch.setattr(
        "pipelines.common.pdf_processing._extract_pdf_text_with_pypdf",
        lambda _path: (["���\x00\x01"], []),
    )
    monkeypatch.setattr(
        "pipelines.common.pdf_processing._extract_pdf_text_with_ocr",
        lambda _path, _count: ([], False, ["ocr_dependency_missing_pymupdf"]),
    )

    extraction = extract_pdf_content("/tmp/ignored.pdf", ocr_fallback=True)

    assert "ocr_dependency_missing_pymupdf" in extraction["warnings"]
    assert "ocr_unavailable_for_low_quality_pdf" in extraction["warnings"]
    assert extraction["ocr_available"] is False


def test_extract_pdf_text_with_ocr_rasterizes_pages(monkeypatch):
    calls: dict[str, int] = {"get_pixmap": 0}

    class FakePixmap:
        n = 3
        width = 1
        height = 1
        samples = b"\x00\x00\x00"

    class FakePage:
        @property
        def images(self):
            raise AssertionError("page.images should not be used for OCR fallback")

        def get_pixmap(self, matrix, alpha):  # noqa: ARG002
            calls["get_pixmap"] += 1
            return FakePixmap()

    class FakePdf:
        def __iter__(self):
            return iter([FakePage(), FakePage()])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ARG002
            return None

    fitz = ModuleType("fitz")
    fitz.open = lambda _path: FakePdf()
    fitz.Matrix = lambda _x, _y: object()

    pytesseract = ModuleType("pytesseract")
    pytesseract.image_to_string = lambda _img: "OCR PAGE TEXT"

    image_mod = ModuleType("PIL.Image")
    image_mod.frombytes = lambda _mode, _size, _bytes: object()
    pil_mod = ModuleType("PIL")
    pil_mod.Image = image_mod

    monkeypatch.setitem(sys.modules, "fitz", fitz)
    monkeypatch.setitem(sys.modules, "pytesseract", pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", pil_mod)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_mod)

    pages, available, warnings = _extract_pdf_text_with_ocr("/tmp/ignored.pdf", page_count_hint=2)

    assert available is True
    assert warnings == []
    assert pages == ["OCR PAGE TEXT", "OCR PAGE TEXT"]
    assert calls["get_pixmap"] == 2
