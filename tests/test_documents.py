from pathlib import Path

import pymupdf

from cmrmp.extract import fields
from cmrmp.extract.documents import pdf_text

AVIS = (Path(__file__).parent / "fixtures" / "avis_synthetique.txt").read_text(encoding="utf-8")


def test_pdf_text_roundtrip(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), AVIS, fontsize=9)
    path = tmp_path / "avis.pdf"
    doc.save(path)
    text, meta = pdf_text(path, ocr=False)
    assert meta["pages"] == 1 and meta["scanned_pages"] == 0
    assert fields.caution_soumission(text) == 3100500
