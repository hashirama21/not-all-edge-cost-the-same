"""Collecteurs ARMP : moteur /recherche_avancee, sanctions, experts, TLS COLEPS."""

import httpx
from pathlib import Path

from cmrmp.config import settings
from cmrmp import storage
from cmrmp.build import tables
from cmrmp.sources import armp
from cmrmp.sources.common import interim

FIXTURES = Path(__file__).parent / "fixtures"


def _patch_client(monkeypatch, module, handler):
    from cmrmp.http import PoliteClient

    def factory(source, **kw):
        c = PoliteClient(source, min_delay_s=0, jitter_s=0, transport=httpx.MockTransport(handler))
        c.robots_backoff_s = [0, 0]
        return c

    monkeypatch.setattr(module, "PoliteClient", factory)


# --------------------------------------------------------------------------- liste /recherche_avancee
def test_parse_listing_fields_and_documents():
    html = (FIXTURES / "armp_recherche.html").read_text(encoding="utf-8")
    recs = armp.parse_listing(html, "https://www.armp.cm/recherche_avancee")
    assert [r["type_publication"] for r in recs] == ["AO", "DC"]
    r = recs[0]
    assert r["url"].endswith("/details?type_publication=AO&id_publication=1516642")
    assert r["title"].startswith("AVIS D'APPEL D'OFFRES NATIONAL OUVERT")
    assert r["reference"] == "020/AONO/SOCA/CIPSM/2026"
    assert r["maitre_ouvrage"] == "COMMUNE DE BANDJA"
    assert r["type_procedure"] == "Appel d'Offres Ouvert National"
    assert r["region"] == "OUEST"
    assert r["amount_fcfa"] == 664421185
    assert r["date_publication"] == "2026-11-03 06:54:04"
    assert r["date_cloture"] == "2026-12-09" and r["heure_cloture"] == "11:59"
    assert [d["url"] for d in r["documents"]] == [
        "http://pridesoft.armp.cm//0903_publications_dl?type_publication=AO&id_publication=1516642"]

    r1 = recs[1]
    # date de clôture masquée (01-01-1970 -> None) ; seul le DAO est en file
    assert r1["date_cloture"] is None and r1["date_publication"] == "2026-11-09 08:25:11"
    assert len(r1["documents"]) == 1 and "0903_dao_dl" in r1["documents"][0]["url"]
    assert all("facebook" not in d["url"] for rec in recs for d in rec["documents"])


def test_last_page_from_pagination():
    html = (FIXTURES / "armp_recherche.html").read_text(encoding="utf-8")
    assert armp._last_page(html) == 3


def test_crawl_exercices_couvert_decennale(isolated_data, monkeypatch):
    html = (FIXTURES / "armp_recherche.html").read_text(encoding="utf-8")
    seen = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(str(req.url))
        page = req.url.params.get("page")
        return httpx.Response(200, html=html if page in (None, "1")
                              else "<html><body></body></html>")

    _patch_client(monkeypatch, armp, handler)
    assert armp.crawl_publications(exercices=[2021], codes=[5], recent=False) == 2
    storage.reset_jsonl_index()
    assert armp.crawl_publications(exercices=[2021], codes=[5], recent=False) == 0

    fa = [u for u in seen if "filtres_avancee?" in u]
    assert fa and "exercice=2021" in fa[0] and "type_procedure=5" in fa[0]
    assert any("&page=2" in u for u in fa)
    recs = storage.read_jsonl(interim("armp_publications.jsonl"))
    assert len(recs) == 2 and all(r["exercice"] == 2021 and r["procedure_code"] == "5" for r in recs)


def test_crawl_publications_pagination_filters_and_dedupe(isolated_data, monkeypatch):
    html = (FIXTURES / "armp_recherche.html").read_text(encoding="utf-8")
    seen = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(str(req.url))
        return httpx.Response(200, html=html if req.url.params.get("page") in (None, "1")
                              else "<html><body></body></html>")

    _patch_client(monkeypatch, armp, handler)
    monkeypatch.setitem(settings()["armp"], "max_pages", 99)

    n = armp.crawl_publications(["AO", "DC"], region="2", departement="5",
                                maitre_ouvrage="813", seuil=2, reference="020/AONO")
    storage.reset_jsonl_index()
    assert n == 2

    search = [u for u in seen if "recherche_avancee?" in u]
    assert all("recherche_avancee_do=1" in u for u in search)
    assert search[0].count("type_publication") == 2  # AO + DC, sans suivi de page
    assert any("&page=2" in u for u in search)
    assert "seuil_marches=2" in search[0] and "region=2" in search[0]
    assert "departement=5" in search[0] and "maitre_ouvrage=813" in search[0]
    assert "reference_avis=020%2FAONO" in search[0]

    # relance : aucune doublon ni nouvelle mise en file
    assert armp.crawl_publications(["AO", "DC"], region="2", seuil=2) == 0

    recs = storage.read_jsonl(interim("armp_publications.jsonl"))
    assert {r["id_publication"] for r in recs} == {"1516642", "17440"}
    docq = storage.read_jsonl(interim("armp_documents_queue.jsonl"))
    assert len(docq) == 2 and {d["source"] for d in docq} == {"armp"}
    assert docq[0]["reference"] == "020/AONO/SOCA/CIPSM/2026"


def test_crawl_sanctions_and_build(isolated_data, monkeypatch):
    html = (FIXTURES / "armp_sanctions.html").read_text(encoding="utf-8")

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=html)

    _patch_client(monkeypatch, armp, handler)
    n = armp.crawl_sanctions()
    storage.reset_jsonl_index()
    armp.crawl_sanctions()  # relance sans doublon
    recs = storage.read_jsonl(interim("armp_sanctions.jsonl"))
    assert n == 2 + 2  # 2 sanctions + 2 recours
    assert len(recs) == 2 and recs[0]["prestataire"] == "ETS ALPHA SARL"
    assert recs[0]["date_debut"] == "2017-02-20" and recs[0]["date_fin"] == "2027-02-20"

    recs = storage.read_jsonl(interim("armp_recours.jsonl"))
    assert len(recs) == 2 and recs[0]["code"] == "REC-2025-001"
    assert recs[0]["etat"] == "Instruit" and recs[0]["destinataire"] == "CER"
    assert recs[1]["code"] == "" and recs[1]["prestataire"] == "ETS DELTA"

    t = tables.build_all()["sanctions"]
    row = t[t["entreprise_brut"] == "ETS ALPHA SARL"].iloc[0]
    assert row["autorite"] == "ARMP" and row["type_sanction"] == "exclusion"
    assert row["duree_mois"] == 120
    assert row["marche_reference"] == "009/MCNT/2017"


def test_crawl_experts(isolated_data, monkeypatch):
    html = (FIXTURES / "armp_experts.html").read_text(encoding="utf-8")
    seen = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(str(req.url))
        return httpx.Response(200, html=html)

    _patch_client(monkeypatch, armp, handler)
    n = armp.crawl_experts(region="10", domaines=["AG"])
    storage.reset_jsonl_index()
    armp.crawl_experts(region="10", domaines=["AG"])
    assert n == 2
    assert any("/experts_search?" in u and "domaines%5B%5D=AG" in u for u in seen)
    recs = storage.read_jsonl(interim("armp_experts.jsonl"))
    aba = [r for r in recs if r["nom"] == "ABA EDOA Laurent"][0]
    assert aba["ville"] == "YAOUNDE" and aba["region"] == "CENTRE"
    assert aba["domaines"] == ["SPI"] and set(aba["lieux"]) == {"S/Cent.", "CE"}
    assert len(recs) == 2


# --------------------------------------------------------------------------- TLS COLEPS
def test_documents_verify_for_coleps_hosts(monkeypatch):
    from cmrmp.sources.documents import _verify_for

    monkeypatch.setitem(settings()["coleps"], "insecure_tls", False)
    assert _verify_for("www.marchespublics.cm") is None
    monkeypatch.setitem(settings()["coleps"], "insecure_tls", True)
    assert _verify_for("www.marchespublics.cm") is not None
    assert _verify_for("marchespublics.cm") is not None
    assert _verify_for("pridesoft.armp.cm") is None
    assert _verify_for("www.publiccontracts.cm") is not None
