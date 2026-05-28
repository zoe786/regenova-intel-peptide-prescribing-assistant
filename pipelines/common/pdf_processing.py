"""PDF-specific extraction, quality checks, cleaning, and chunking utilities."""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from typing import Any

from pipelines.common.chunking import chunk_by_tokens

logger = logging.getLogger(__name__)


def analyze_pdf_text_quality(text: str, page_count: int) -> dict[str, Any]:
    """Return quality diagnostics for extracted PDF text."""
    text = text or ""
    total_chars = len(text)
    alpha_chars = sum(1 for ch in text if ch.isalpha())
    alpha_ratio = alpha_chars / max(1, total_chars)

    weird_chars = 0
    for ch in text:
        category = unicodedata.category(ch)
        if (
            ch == "�"
            or category in {"Co", "Cs", "Cn"}
            or (not ch.isprintable() and not ch.isspace())
        ):
            weird_chars += 1
    weird_char_ratio = weird_chars / max(1, total_chars)
    chars_per_page = total_chars / max(1, page_count)

    warnings: list[str] = []
    if alpha_ratio < 0.45:
        warnings.append("low_alpha_ratio")
    if weird_char_ratio > 0.08:
        warnings.append("high_weird_char_ratio")
    if page_count >= 3 and chars_per_page < 120:
        warnings.append("very_short_text_for_page_count")

    is_rejected = (
        alpha_ratio < 0.2
        or weird_char_ratio > 0.2
        or (page_count >= 3 and chars_per_page < 40)
    )
    quality_status = "rejected" if is_rejected else ("warning" if warnings else "ok")

    return {
        "page_count": page_count,
        "char_count": total_chars,
        "alpha_ratio": round(alpha_ratio, 4),
        "weird_char_ratio": round(weird_char_ratio, 4),
        "chars_per_page": round(chars_per_page, 2),
        "warnings": warnings,
        "quality_status": quality_status,
    }


def _extract_pdf_text_with_pypdf(path: str) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    try:
        import pypdf  # type: ignore[import]
    except ImportError:
        logger.warning("pypdf is not installed; PDF text extraction unavailable")
        return [], ["pypdf_unavailable"]

    try:
        reader = pypdf.PdfReader(path)
        return [(page.extract_text() or "") for page in reader.pages], warnings
    except Exception as exc:
        logger.error("Failed direct PDF extraction for %s: %s", path, exc)
        return [], ["pdf_extract_failed"]


def _extract_pdf_text_with_ocr(path: str, page_count_hint: int) -> tuple[list[str], bool, list[str]]:
    warnings: list[str] = []
    missing_deps: list[str] = []
    try:
        import fitz  # type: ignore[import]
    except ImportError:
        missing_deps.append("pymupdf")
        fitz = None  # type: ignore[assignment]
    try:
        import pytesseract  # type: ignore[import]
    except ImportError:
        missing_deps.append("pytesseract")
        pytesseract = None  # type: ignore[assignment]
    try:
        from PIL import Image as pil_image  # type: ignore[import]
    except ImportError:
        missing_deps.append("pillow")
        pil_image = None  # type: ignore[assignment]

    if missing_deps:
        warnings.extend(f"ocr_dependency_missing_{dep}" for dep in missing_deps)
        return [], False, warnings

    try:
        pages: list[str] = []
        zoom = 2.0  # ~144 DPI baseline rasterization for better OCR
        matrix = fitz.Matrix(zoom, zoom)
        with fitz.open(path) as pdf:
            for page in pdf:
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                mode = "RGB" if pix.n >= 3 else "L"
                img = pil_image.frombytes(mode, (pix.width, pix.height), pix.samples)
                ocr_text = pytesseract.image_to_string(img)
                pages.append((ocr_text or "").strip())

        if not pages:
            pages = [""] * max(1, page_count_hint)
        return pages, True, warnings
    except Exception as exc:
        if exc.__class__.__name__ == "TesseractNotFoundError":
            logger.warning("OCR engine unavailable for %s: %s", path, exc)
            return [], False, ["ocr_engine_unavailable"]
        logger.warning("OCR extraction failed for %s: %s", path, exc)
        return [], True, ["ocr_failed"]


def _remove_repeated_headers_footers(pages: list[str]) -> list[str]:
    if len(pages) < 2:
        return pages

    top_counts: dict[str, int] = {}
    bottom_counts: dict[str, int] = {}
    page_lines: list[list[str]] = []
    for page in pages:
        lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
        page_lines.append(lines)
        for ln in lines[:2]:
            top_counts[ln.lower()] = top_counts.get(ln.lower(), 0) + 1
        for ln in lines[-2:]:
            bottom_counts[ln.lower()] = bottom_counts.get(ln.lower(), 0) + 1

    threshold = max(2, math.ceil(len(pages) * 0.6))
    repeated_top = {ln for ln, count in top_counts.items() if count >= threshold}
    repeated_bottom = {ln for ln, count in bottom_counts.items() if count >= threshold}

    cleaned_pages: list[str] = []
    for lines in page_lines:
        while lines and (
            lines[0].lower() in repeated_top
            or re.fullmatch(r"(?:page\s+)?\d+(?:\s*/\s*\d+)?", lines[0].lower())
        ):
            lines = lines[1:]
        while lines and (
            lines[-1].lower() in repeated_bottom
            or re.fullmatch(r"(?:page\s+)?\d+(?:\s*/\s*\d+)?", lines[-1].lower())
        ):
            lines = lines[:-1]
        cleaned_pages.append("\n".join(lines))
    return cleaned_pages


def clean_pdf_pages(pages: list[str]) -> list[str]:
    """Apply PDF-specific text cleaning without flattening layout."""
    cleaned_pages: list[str] = []
    for page in pages:
        page_text = page or ""
        page_text = page_text.replace("\r\n", "\n").replace("\r", "\n")
        page_text = re.sub(r"(\w+)-\n(\w+)", r"\1\2", page_text)  # dehyphenate line wraps
        page_text = re.sub(r"[ \t]+\n", "\n", page_text)
        page_text = re.sub(r"\n{3,}", "\n\n", page_text)
        lines = [ln.strip() for ln in page_text.splitlines()]

        repaired_lines: list[str] = []
        for ln in lines:
            if not ln:
                repaired_lines.append("")
                continue
            if (
                repaired_lines
                and repaired_lines[-1]
                and not repaired_lines[-1].endswith((".", ":", ";", "!", "?"))
                and ln
                and ln[0].islower()
            ):
                repaired_lines[-1] = f"{repaired_lines[-1]} {ln}"  # pragmatic column-merge repair
            else:
                repaired_lines.append(ln)

        cleaned_pages.append("\n".join(repaired_lines).strip())

    return _remove_repeated_headers_footers(cleaned_pages)


def _looks_like_heading(block: str) -> bool:
    stripped = block.strip()
    if not stripped or len(stripped) > 100 or "\n" in stripped:
        return False
    if stripped.endswith((".", "!", "?")):
        return False
    words = stripped.split()
    return 1 <= len(words) <= 12 and (stripped.isupper() or stripped.istitle())


def _looks_like_table(block: str) -> bool:
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    delimiter_lines = sum(
        1
        for ln in lines
        if "|" in ln or "\t" in ln or re.search(r"\S\s{2,}\S", ln)
    )
    numeric_lines = sum(1 for ln in lines if sum(ch.isdigit() for ch in ln) >= 3)
    return delimiter_lines >= 2 or (delimiter_lines >= 1 and numeric_lines >= 2)


def chunk_pdf_pages_layout(
    pages: list[str],
    max_tokens: int = 512,
    overlap: int = 50,
) -> list[str]:
    """Chunk PDF text by page/section with simple heading & table preservation."""
    if not pages:
        return []

    chunks: list[str] = []
    for page_idx, page in enumerate(pages, start=1):
        if not page.strip():
            continue
        blocks = [b.strip() for b in re.split(r"\n\s*\n", page) if b.strip()]
        active_heading = ""
        for block in blocks:
            if _looks_like_heading(block):
                active_heading = block
                continue

            block_type = "TABLE" if _looks_like_table(block) else "PROSE"
            prefix = f"[Page {page_idx}]"
            if block_type == "TABLE":
                prefix += " [TABLE]"

            section_text = block
            if active_heading and active_heading.lower() not in block.lower():
                section_text = f"{active_heading}\n{block}"

            section = f"{prefix}\n{section_text}".strip()
            section_chunks = chunk_by_tokens(section, max_tokens=max_tokens, overlap=overlap)
            chunks.extend(section_chunks or [section])
    return chunks


def extract_pdf_content(path: str, *, ocr_fallback: bool = True) -> dict[str, Any]:
    """Extract PDF content with quality checks and optional OCR fallback."""
    pages_direct, warnings = _extract_pdf_text_with_pypdf(path)
    page_count = len(pages_direct) if pages_direct else 1
    direct_text = "\n\n".join(pages_direct)
    direct_quality = analyze_pdf_text_quality(direct_text, page_count)

    selected_pages = pages_direct
    selected_method = "pypdf"
    selected_quality = direct_quality
    ocr_attempted = False
    ocr_used = False
    ocr_available = False

    if ocr_fallback and direct_quality["quality_status"] != "ok":
        ocr_attempted = True
        ocr_pages, ocr_available, ocr_warnings = _extract_pdf_text_with_ocr(path, page_count)
        warnings.extend(ocr_warnings)
        if ocr_pages:
            ocr_text = "\n\n".join(ocr_pages)
            ocr_quality = analyze_pdf_text_quality(ocr_text, max(page_count, len(ocr_pages)))
            direct_score = (
                direct_quality["alpha_ratio"] - direct_quality["weird_char_ratio"],
                direct_quality["chars_per_page"],
            )
            ocr_score = (
                ocr_quality["alpha_ratio"] - ocr_quality["weird_char_ratio"],
                ocr_quality["chars_per_page"],
            )
            if ocr_score > direct_score:
                selected_pages = ocr_pages
                selected_method = "ocr"
                selected_quality = ocr_quality
                ocr_used = True
        elif direct_quality["quality_status"] != "ok" and not ocr_available:
            warnings.append("ocr_unavailable_for_low_quality_pdf")

    cleaned_pages = clean_pdf_pages(selected_pages)
    cleaned_text = "\n\n".join(page for page in cleaned_pages if page.strip())
    raw_preview = (direct_text or "")[:1200]
    clean_preview = cleaned_text[:1200]

    if not cleaned_text.strip():
        selected_quality["quality_status"] = "rejected"
        if "empty_after_cleaning" not in selected_quality["warnings"]:
            selected_quality["warnings"].append("empty_after_cleaning")

    return {
        "raw_text": direct_text,
        "cleaned_text": cleaned_text,
        "cleaned_pages": cleaned_pages,
        "page_count": max(page_count, len(cleaned_pages)),
        "extraction_method": selected_method,
        "quality": selected_quality,
        "warnings": sorted({*warnings, *selected_quality["warnings"]}),
        "ocr_attempted": ocr_attempted,
        "ocr_used": ocr_used,
        "ocr_available": ocr_available,
        "raw_preview": raw_preview,
        "clean_preview": clean_preview,
    }
