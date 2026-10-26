from pathlib import Path

from cmrmp.sources.armp import pridesoft_path_meta
from cmrmp.sources.common import describe_page, document_links, items_from_listing

HTML = (Path(__file__).parent / "fixtures" / "katika_liste.html").read_text(encoding="utf-8")


def test_items_from_listing():
    items = items_from_listing(HTML, "https://katika237.com/tender/", ["table tbody tr"])
    assert len(items) == 2
    assert items[0]["links"][0]["url"] == "https://katika237.com/tender/12345/"
    docs = document_links(items[0]["links"])
    assert docs and docs[0]["url"].endswith(".pdf")


def test_describe_page():
    d = describe_page(HTML, "https://katika237.com/tender/")
    assert d["tables"] == 1
    assert any("/tender/{n}/" in p for p, _ in d["url_patterns"])


def test_pridesoft_path_meta():
    m = pridesoft_path_meta("https://pridesoft.armp.cm/_lib/file/doc/0903/2026/03/23/C_YAOUNDE_I/AVIS.pdf")
    assert m == {"upload_date": "2026-03-23", "entity_code": "C_YAOUNDE_I"}
