from pathlib import Path

import pymupdf

from cmrmp.extract import fields
from cmrmp.extract import documents
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


def _pv_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 50), "PROCES-VERBAL D'OUVERTURE DES PLIS N°025/AONO/CAYI/2026", fontsize=10)
    rows = [["N°", "Soumissionnaire", "Montant de l'offre (FCFA)", "Observations"],
            ["1", "ETS ALPHA SARL", "100 000 000", "Recevable"],
            ["2", "BETA BTP SA", "100 200 000", "Recevable"],
            ["3", "GAMMA SERVICES", "102 000 000", "Non recevable"]]
    xs, y0, h = [40, 80, 260, 420, 560], 80, 24
    for i in range(len(rows) + 1):
        page.draw_line((xs[0], y0 + i * h), (xs[-1], y0 + i * h))
    for x in xs:
        page.draw_line((x, y0), (x, y0 + len(rows) * h))
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            page.insert_text((xs[j] + 3, y0 + i * h + 16), cell, fontsize=8)
    doc.save(path)


def test_pv_table_bids(tmp_path):
    path = tmp_path / "pv.pdf"
    _pv_pdf(path)
    text, meta = pdf_text(path, ocr=False)
    assert meta["tables"], "PyMuPDF doit détecter le tableau"
    bids = fields.bids_from_tables(meta["tables"])
    assert [b["soumissionnaire"] for b in bids] == ["ETS ALPHA SARL", "BETA BTP SA", "GAMMA SERVICES"]
    assert bids[1]["montant_offre_fcfa"] == 100200000
    assert bids[2]["recevable"] is False
    assert "| Soumissionnaire" in documents.pdf_markdown(text, meta)


def test_pipeline_on_pdf(tmp_path, isolated_data):
    from cmrmp.build import tables
    from cmrmp.extract import pipeline
    from cmrmp.storage import RawStore

    path = tmp_path / "pv.pdf"
    _pv_pdf(path)
    RawStore("docs/pridesoft.armp.cm").put(
        "https://pridesoft.armp.cm/_lib/file/doc/0903/2026/04/05/C_YAOUNDE_I/PV.pdf",
        "https://pridesoft.armp.cm/_lib/file/doc/0903/2026/04/05/C_YAOUNDE_I/PV.pdf",
        200, {"content-type": "application/pdf"}, path.read_bytes(), {"title": "PV d'ouverture des plis"})
    df = pipeline.run(llm=False, workers=1)
    assert df.loc[0, "doc_type"] == "pv_ouverture" and df.loc[0, "n_tables"] >= 1
    t = tables.build_all()
    assert len(t["soumissions"]) == 3
    assert t["soumissions"]["modele_extraction"].eq("pymupdf_tables").all()
    assert t["soumissions"]["reference"].iloc[0] == "025/AONO/CAYI/2026"
