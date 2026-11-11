from pathlib import Path

from cmrmp.sources.armp import pridesoft_path_meta
from cmrmp.sources.common import describe_page, document_links, items_from_listing

HTML = (Path(__file__).parent / "fixtures" / "katika_liste.html").read_text(encoding="utf-8")
TENDER = (Path(__file__).parent / "fixtures" / "katika_tender.html").read_text(encoding="utf-8")


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


def test_katika_parse_site_date():
    from cmrmp.sources.katika import parse_site_date

    assert parse_site_date("Nov 10, 2026") == "2026-11-10"
    assert parse_site_date("Feb 1, 2019") == "2019-02-01"
    assert parse_site_date("15 octobre 2026") == ""
    assert parse_site_date("") == ""


def test_katika_bs4_items():
    from cmrmp.sources.katika import parse_entries_bs4

    items, meta = parse_entries_bs4(
        TENDER, "https://katika237.com/tender/?o=&page=1&r=&sort=date&type=&y=2026")
    # cartographie de page
    assert meta["result_count"] == 14791
    assert meta["last_page"] == 148
    assert set(meta["types"]) == {"ADDITIF", "AO", "DEC-ATTR"}
    assert "date" in meta["sorts"]
    assert "SUD" in meta["regions"] and "SERVICES CENTRAUX" in meta["regions"]
    assert len(items) == 3
    # item 1 : tous les champs structurés
    it = items[0]
    assert it["date"] == "2026-11-10"
    assert it["maitre_ouvrage"] == "PAK"
    assert it["maitre_ouvrage_complet"] == "PORT AUTONOME DE KRIBI"
    assert it["owner_id"] == "884"
    assert it["region"] == "SUD"
    assert it["type_avis"] == "AO"
    assert it["montant_fcfa"] == 300000000  # attribut title exact, pas le texte arrondi
    assert it["detail_url"] == "https://armp.cm/details?type_publication=AO&id_publication=65721"
    assert it["reference"] == "031/AONO/PAK/CIPM/2026"
    assert it["doc_type"] == "avis"
    assert any("0903_dao_dl" in d["url"] for d in it["documents"])
    assert it["armp_links"] == ["https://armp.cm/details?type_publication=AO&id_publication=65721",
                                "http://pridesoft.armp.cm//0903_dao_dl?type_publication=AO&id_publication=65721"]
    # item 2 : sans maître d'ouvrage, montant exact conservé
    it2 = items[1]
    assert it2["maitre_ouvrage"] is None
    assert it2["owner_id"] is None
    assert it2["region"] == "SERVICES CENTRAUX"
    assert it2["montant_fcfa"] == 847504026
    # item 3 : montant illisible -> None, document PDF repéré
    it3 = items[2]
    assert it3["montant_fcfa"] is None
    assert any(d["url"].endswith(".pdf") for d in it3["documents"])
    assert it3["doc_type"] == "dao" or it3["type_avis"] == "DC"
