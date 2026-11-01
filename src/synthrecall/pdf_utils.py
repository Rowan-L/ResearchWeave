from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf as fitz

MAX_INDEX_CHARS = 250_000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pdf_metadata(path: Path) -> dict[str, str]:
    try:
        with fitz.open(path) as document:
            metadata = document.metadata or {}
            return {
                "title": metadata.get("title") or path.stem,
                "authors": metadata.get("author") or "",
            }
    except Exception:  # noqa: BLE001 — 损坏 PDF 的元数据读取按缺失处理
        return {"title": path.stem, "authors": ""}


def extract_pdf_text(path: Path, max_chars: int = MAX_INDEX_CHARS) -> tuple[str, int]:
    chunks: list[str] = []
    extracted_chars = 0
    page_count = 0
    try:
        with fitz.open(path) as document:
            page_count = document.page_count
            for page in document:
                if extracted_chars >= max_chars:
                    break
                page_text = page.get_text("text")
                chunks.append(page_text)
                extracted_chars += len(page_text)
    except Exception:  # noqa: BLE001 — 损坏 PDF 的全文提取按空处理
        return "", 0
    return "\n".join(chunks).strip()[:max_chars], page_count


def extract_text_preview(path: Path, max_pages: int = 2, max_chars: int = 4000) -> str:
    chunks: list[str] = []
    try:
        with fitz.open(path) as document:
            for page in document[:max_pages]:
                chunks.append(page.get_text("text"))
    except Exception:  # noqa: BLE001 — 损坏 PDF 的预览按空处理
        return ""
    return "\n".join(chunks).strip()[:max_chars]


def extract_paper_identifiers(text: str) -> dict[str, str]:
    doi_match = re.search(
        r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
        text,
        flags=re.IGNORECASE,
    )
    arxiv_match = re.search(
        r"\b(?:arxiv\s*:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?\b",
        text,
        flags=re.IGNORECASE,
    )
    doi = doi_match.group(1).rstrip(".,;:)]}") if doi_match else ""
    return {
        "doi": doi,
        "arxiv_id": arxiv_match.group(1) if arxiv_match else "",
    }
