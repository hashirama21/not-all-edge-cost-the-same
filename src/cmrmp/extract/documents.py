"""Texte des documents (PDF, DOCX, HTML), avec OCR si le PDF est scanné."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser as HTMLParser

log = logging.getLogger(__name__)

MIN_CHARS_PER_PAGE = 80  # en dessous, la page est probablement une image


def pdf_text(path: Path, ocr: bool = True) -> tuple[str, dict]:
    import pymupdf

    meta = {"pages": 0, "ocr": False, "scanned_pages": 0}
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # PDF corrompu
        log.warning("PDF illisible %s : %s", path, exc)
        return "", {**meta, "error": str(exc)}
    pages = [page.get_text("text", sort=True) for page in doc]
    meta["pages"] = len(pages)
    meta["scanned_pages"] = sum(len(p.strip()) < MIN_CHARS_PER_PAGE for p in pages)
    doc.close()
    if ocr and pages and meta["scanned_pages"] / len(pages) > 0.5:
        text = _ocr(path)
        if text:
            meta["ocr"] = True
            return text, meta
    return "\n\f".join(pages), meta


def _ocr(path: Path) -> str:
    """OCR via ocrmypdf (tesseract, langue fra+eng) s'il est installé."""
    if not shutil.which("ocrmypdf"):
        log.info("ocrmypdf absent : %s reste sans OCR", path.name)
        return ""
    import pymupdf

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.pdf"
        cmd = ["ocrmypdf", "-l", "fra+eng", "--skip-text", "--quiet", str(path), str(out)]
        if subprocess.run(cmd, capture_output=True).returncode != 0 or not out.exists():
            return ""
        with pymupdf.open(out) as doc:
            return "\n\f".join(page.get_text("text", sort=True) for page in doc)


def docx_text(path: Path) -> str:
    import docx

    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def html_text(html: str) -> str:
    tree = HTMLParser(html)
    for node in tree.css("script, style, noscript, nav, footer, header"):
        node.decompose()
    body = tree.body or tree.root
    return body.text(separator="\n", strip=True) if body else ""


def html_tables(html: str) -> list[list[dict]]:
    """Chaque <table> devient une liste de dicts {en-tête: cellule}."""
    tree = HTMLParser(html)
    out = []
    for table in tree.css("table"):
        rows = table.css("tr")
        if not rows:
            continue
        headers = [c.text(strip=True) for c in rows[0].css("th, td")]
        records = []
        for row in rows[1:]:
            cells = [c.text(strip=True) for c in row.css("td, th")]
            if cells and any(cells):
                keys = headers if len(headers) == len(cells) else [f"col{i}" for i in range(len(cells))]
                records.append(dict(zip(keys, cells)))
        out.append(records)
    return out


def document_text(path: Path, content_type: str = "", ocr: bool = True) -> tuple[str, dict]:
    suffix = path.suffix.lower()
    if suffix == ".pdf" or "pdf" in content_type:
        return pdf_text(path, ocr=ocr)
    if suffix == ".docx":
        return docx_text(path), {}
    if suffix in (".html", ".htm") or "html" in content_type:
        return html_text(path.read_text(encoding="utf-8", errors="replace")), {}
    if suffix in (".txt", ".json"):
        return path.read_text(encoding="utf-8", errors="replace"), {}
    return "", {"unsupported": suffix}
