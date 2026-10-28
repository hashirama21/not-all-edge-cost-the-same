"""Non-régression des bugs relevés lors de l'analyse de bout en bout."""

import json
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import httpx
import pandas as pd
import pytest

from cmrmp.build import entities, graph, tables
from cmrmp.config import settings
from cmrmp.extract import fields
from cmrmp.sources.common import interim
from cmrmp import storage

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- montants, types, libellés
def test_amounts_with_decimals_and_separators():
    assert fields.amounts("offre de 3 100 500,00 FCFA TTC")[0][0] == 3100500
    assert fields.parse_fcfa("12 500 000,00") == 12500000
    assert fields.parse_fcfa("12.500.000,50 FCFA") == 12500000
    assert fields.amounts("exercice 2026 500 000 FCFA")[0][0] == 500000
    assert fields.amounts("3 100 500 F CFA")[0][0] == 3100500
    assert fields.bids_from_tables([{"header": ["Soumissionnaire", "Montant TTC"], "rows": [["ALPHA", "12 500 000,00"]]}])[0][
        "montant_offre_fcfa"] == 12500000


def test_classify_order():
    assert fields.classify("Communiqué", "Exclusion de la commande publique, appel d'offres N°12") == "sanction"
    assert fields.classify("PROCES-VERBAL DE JUGEMENT", "proposition d'attributaire : ETS X") == "pv_jugement"
    assert fields.is_sanction("Communiqué", "Les entreprises suivantes sont suspendues des marchés publics")


def test_label_key_without_apostrophe():
    assert fields.label_values("MAÎTRE D'OUVRAGE : COMMUNE X") == {"maitre_d_ouvrage": "COMMUNE X"}
    assert [fields.normalize_lot(v) for v in ("Lot n°01", None, "LOT 2 : voirie")] == ["1", "unique", "2"]


# --------------------------------------------------------------------------- résolution d'entités
def test_groupement_plus_and_identifiers():
    assert entities.is_groupement("ALPHA SARL + BETA SA")
    m = pd.DataFrame({"nom_brut": ["ALPHA", "ZETA"], "niu": ["M 011500012345 A", "M011500012345A"]})
    out, _ = entities.resolve(m)
    assert out["entity_id"].nunique() == 1


def test_shared_identifier_over_limit_not_merged():
    names = [f"SOCIETE {c}{c}{c}" for c in "ABCDEFG"]
    out, _ = entities.resolve(pd.DataFrame({"nom_brut": names, "niu": ["M011500012345A"] * len(names)}))
    assert out["entity_id"].nunique() == len(names)


def test_entity_id_stable_across_order():
    names = ["ETS ALPHA ET FILS SARL", "Alpha & Fils", "BETA BTP SA"]
    a, _ = entities.resolve(pd.DataFrame({"nom_brut": names}))
    b, _ = entities.resolve(pd.DataFrame({"nom_brut": names[::-1] + ["Alpha et Fils"]}))
    assert a.set_index("nom_brut")["entity_id"]["Alpha & Fils"] == b.set_index("nom_brut")["entity_id"]["Alpha & Fils"]


def test_large_block_is_refined(monkeypatch):
    monkeypatch.setattr(entities, "MAX_BLOCK", 2)
    out, pairs = entities.resolve(pd.DataFrame({"nom_brut": ["ALPHA BETON", "ALPHA BETONS", "ALPHA ZINC", "ALPHA ZINCS"]}))
    ids = out.set_index("nom_brut")["entity_id"]
    assert ids["ALPHA BETON"] == ids["ALPHA BETONS"] and ids["ALPHA ZINC"] == ids["ALPHA ZINCS"]


# --------------------------------------------------------------------------- tables
def _write_llm(recs):
    with interim("extract_llm.jsonl").open("w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _m(v):
    return {"valeur_fcfa": v, "citation": None}


def _sub(name, amount, lot=None):
    return {"lot": lot, "soumissionnaire": name, "montant_offre": _m(amount), "rabais": None, "recevable": True,
            "rang": None, "motif_rejet": None, "niu": None, "rccm": None, "adresse": None, "telephone": None,
            "email": None, "representant": None}


def test_tables_regex_fallback_dedupe_and_lots(isolated_data):
    docs = pd.DataFrame([
        {"doc_id": "r1", "url": "u1", "doc_type": "avis", "rx_reference": "1/AONO/X/2026", "rx_procedure": "AONO",
         "rx_cout_dossier_fcfa": 50000.0, "rx_caution_fcfa": 1000000.0, "rx_capacite_financiere_fcfa": None,
         "rx_cout_previsionnel_fcfa": 40000000.0, "rx_date_depot": "2026-04-01", "rx_date_ouverture": None},
        {"doc_id": "r2", "url": "u2", "doc_type": "dao", "rx_reference": "1/AONO/X/2026", "rx_procedure": "AONO",
         "rx_cout_dossier_fcfa": 1.0, "rx_caution_fcfa": None, "rx_capacite_financiere_fcfa": None,
         "rx_cout_previsionnel_fcfa": None, "rx_date_depot": None, "rx_date_ouverture": None},
    ])
    docs.to_parquet(interim("extract_documents.parquet"), index=False)
    _write_llm([{"doc_id": "p1", "kind": "pv_ouverture", "model": "test", "data": {
        "reference": "1/AONO/X/2026", "maitre_ouvrage": None, "type_pv": "ouverture", "date_seance": None,
        "nombre_offres_annonce": 2, "soumissions": [_sub("ALPHA SARL", 10, "Lot n°01"), _sub("BETA SA", 11, "1")]}}])
    # Ligne tronquée (tâche interrompue) : ignorée, pas de plantage.
    with interim("extract_llm.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"doc_id": "x", "kind": "avis", "da')
    t = tables.build_all()
    assert len(t["avis"]) == 1 and t["avis"].loc[0, "cout_dossier_fcfa"] == 50000  # avis prime sur DAO
    assert set(t["soumissions"]["lot"]) == {"1"}


def test_armp_label_values_and_attribution_dedupe(isolated_data):
    rec = {"url": "https://armp.cm/details?id=1", "reference": "2/AONO/Y/2026",
           "label_values": fields.label_values("ATTRIBUTAIRE : ETS ALPHA\nMAÎTRE D'OUVRAGE : COMMUNE Y\nMONTANT : 1 000 000 FCFA")}
    for _ in range(2):  # anciens fichiers avec doublons de relance
        storage.append_jsonl(interim("armp_details.jsonl"), rec)
    t = tables.build_all()
    assert len(t["attributions"]) == 1
    assert t["attributions"].loc[0, "maitre_ouvrage"] == "COMMUNE Y"


def test_minmap_regex_sanctions(isolated_data):
    storage.append_jsonl(interim("minmap_sanctions_raw.jsonl"), {
        "url": "https://minmap.cm/?p=1", "title": "Exclusion de la commande publique", "published": "2026-05-02T10:00:00",
        "text": "...", "duree": "deux ans", "entreprises_regex": ["ETS ALPHA", "BETA BTP"]})
    s = tables.build_all()["sanctions"]
    assert set(s["entreprise_brut"]) == {"ETS ALPHA", "BETA BTP"}
    assert set(s["duree_mois"]) == {24} and set(s["type_sanction"]) == {"exclusion"}


def test_groupement_members_in_cosubmission(isolated_data):
    _write_llm([{"doc_id": "p1", "kind": "pv_ouverture", "model": "test", "data": {
        "reference": "3/AONO/Z/2026", "maitre_ouvrage": None, "type_pv": "ouverture", "date_seance": None,
        "nombre_offres_annonce": 2, "soumissions": [_sub("GROUPEMENT ALPHA SARL / BETA SA", 10), _sub("GAMMA SERVICES", 11)]}}])
    t = tables.build_all()
    assert len(t["soumissions"].loc[0, "entity_ids"]) == 2
    e = graph.edges(t)
    assert (e["relation"] == "co_soumission").sum() == 2  # ALPHA–GAMMA et BETA–GAMMA
    assert (e["relation"] == "meme_groupement").sum() == 1
    assert not e["relation"].isin(["meme_niu", "meme_rccm"]).any()


# --------------------------------------------------------------------------- signaux d'alerte, coûts
def _avis(ref, mo="MO A", prev=100.0):
    return {"reference": ref, "lot": "unique", "procedure": None, "date_publication": None, "date_depot": None,
            "cout_previsionnel_fcfa": prev, "maitre_ouvrage": mo, "cout_dossier_fcfa": None, "caution_fcfa": None,
            "validite_offres_jours": None}


def test_red_flags_no_nan_join_and_missing_is_na():
    avis = pd.DataFrame([_avis(None)])
    attr = pd.DataFrame([{"reference": None, "lot": "unique", "entity_id": "E1", "montant_fcfa": 99.0, "maitre_ouvrage": "MO B"}])
    f = graph.red_flags({"avis": avis, "soumissions": pd.DataFrame(), "attributions": attr})
    assert pd.isna(f.loc[0, "attributaire_id"])
    assert pd.isna(f.loc[0, "flag_delai_court"]) and pd.isna(f.loc[0, "flag_proche_estimation"])
    assert pd.isna(f.loc[0, "score_alerte"])


def test_recurrent_winner_leave_one_out():
    refs = [f"{i}/AONO/X/2026" for i in range(5)]
    avis = pd.DataFrame([_avis(r) for r in refs])
    winners = ["E1", "E1", "E1", "E2", "E1"]
    attr = pd.DataFrame([{"reference": r, "lot": "unique", "entity_id": w, "montant_fcfa": 50.0, "maitre_ouvrage": "MO A"}
                         for r, w in zip(refs, winners)])
    f = graph.red_flags({"avis": avis, "soumissions": pd.DataFrame(), "attributions": attr})
    assert f.loc[0, "part_attributaire_chez_mo"] == pytest.approx(3 / 4)  # autres marchés seulement
    one = graph.red_flags({"avis": avis.iloc[:1], "soumissions": pd.DataFrame(), "attributions": attr.iloc[:1]})
    assert pd.isna(one.loc[0, "flag_attributaire_recurrent"])  # un seul marché : pas de signal


def test_injection_cost_nan_when_component_missing(isolated_data):
    avis = pd.DataFrame([{**_avis("1/X/2026"), "cout_dossier_fcfa": 100000.0, "caution_fcfa": None}])
    _, per_market = graph.relation_costs({"avis": avis}, pd.DataFrame())
    assert pd.isna(per_market.loc[0, "cout_injection_cosoumission"])
    assert per_market.loc[0, "composantes_manquantes"] == "cout_opportunite_caution"


# --------------------------------------------------------------------------- stockage et HTTP
def test_capture_paths_portable_and_versioned(isolated_data):
    store = storage.RawStore("t")
    a = store.put("https://x.cm/a", "https://x.cm/a", 200, {"content-type": "text/html"}, b"v1")
    b = store.put("https://x.cm/a", "https://x.cm/a", 200, {"content-type": "text/html"}, b"v2")
    assert Path(a.path).read_bytes() == b"v1" and Path(b.path).read_bytes() == b"v2"
    meta = json.loads(next(isolated_data.glob("raw/t/*/*.meta.json")).read_text(encoding="utf-8"))
    assert not Path(meta["path"]).is_absolute()
    old = "/home/runner/work/x/x/data/" + meta["path"]
    assert Path(storage.resolve_path(old)).read_bytes() == b"v2"
    assert store.get("https://x.cm/never") is None
    assert len(list(isolated_data.glob("raw/t/*"))) == 2  # dossier + manifeste, pas de dossier vide créé


def _client(handler, **kw):
    from cmrmp.http import PoliteClient

    c = PoliteClient("t", min_delay_s=0, jitter_s=0, transport=httpx.MockTransport(handler), **kw)
    c.robots_backoff_s = [0, 0]
    return c


def test_robots_retry_on_5xx_and_max_age(isolated_data):
    calls = {"robots": 0, "page": 0}

    def handler(req):
        if req.url.path == "/robots.txt":
            calls["robots"] += 1
            return httpx.Response(503 if calls["robots"] == 1 else 404)
        calls["page"] += 1
        return httpx.Response(200, html="<p>ok</p>")

    with _client(handler) as c:
        assert c.fetch("https://x.cm/p") is not None
        c.fetch("https://x.cm/p")
        assert calls["page"] == 1
        c.fetch("https://x.cm/p", max_age_s=-1)
        assert calls["page"] == 2


def test_retry_after_http_date():
    from cmrmp.http import parse_retry_after

    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    assert 100 < parse_retry_after(when) <= 121
    assert parse_retry_after("30") == 30


# --------------------------------------------------------------------------- collecteurs
def _patch_client(monkeypatch, module, handler):
    from cmrmp.http import PoliteClient

    def factory(source, **kw):
        c = PoliteClient(source, min_delay_s=0, jitter_s=0, transport=httpx.MockTransport(handler))
        c.robots_backoff_s = [0, 0]
        return c

    monkeypatch.setattr(module, "PoliteClient", factory)


def test_katika_rerun_no_duplicates(isolated_data, monkeypatch):
    from cmrmp.sources import katika

    html = (FIXTURES / "katika_liste.html").read_text(encoding="utf-8")

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=html if req.url.params.get("page") == "1" else "<html><body></body></html>")

    _patch_client(monkeypatch, katika, handler)
    katika.crawl([2020], max_pages=3, details=False)
    storage.reset_jsonl_index()  # nouveau processus
    katika.crawl([2020], max_pages=3, details=False)
    assert len(storage.read_jsonl(interim("katika_items.jsonl"))) == 2


def test_minmap_wp_400_ends_pagination(isolated_data, monkeypatch):
    from cmrmp.sources import minmap

    monkeypatch.setitem(settings()["minmap"], "search_terms", ["exclusion"])
    paths = []

    def handler(req):
        paths.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if "wp-json" in req.url.path:
            if req.url.params.get("page") == "1":
                return httpx.Response(200, json=[{"link": "https://minmap.cm/?p=1", "date": "2026-05-02",
                                                  "title": {"rendered": "Communiqué"},
                                                  "content": {"rendered": "<p>Exclusion de la commande publique des entreprises "
                                                                          "dans le cadre de l'appel d'offres</p>"}}])
            return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})
        return httpx.Response(200, html="<html></html>")

    _patch_client(monkeypatch, minmap, handler)
    assert minmap.crawl() == 1
    assert not any("?s=" in p for p in paths)


# --------------------------------------------------------------------------- publication
def test_bucket_always_private_and_address_hashed(monkeypatch):
    import huggingface_hub

    from cmrmp.publish import hf

    created = []

    class FakeApi:
        def create_bucket(self, bucket_id, private, exist_ok):
            created.append(private)

        def sync_bucket(self, *a, **k):
            pass

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    hf.sync_raw("u/b", direction="up", private=False, paths=["raw"])
    assert created == [True]
    df = hf.pseudonymize(pd.DataFrame({"relation": ["meme_adresse"], "evidence": ["rue 12 bastos"], "adresse": ["rue 12 bastos"]}), "s")
    assert df.loc[0, "evidence"].startswith("h_") and df.loc[0, "adresse"].startswith("h_")


# --------------------------------------------------------------------------- documents
def test_xlsx_text(tmp_path):
    from cmrmp.extract.documents import document_text

    path = tmp_path / "liste.xlsx"
    pd.DataFrame({"Entreprise": ["ETS ALPHA"], "Montant": ["1 000 000"]}).to_excel(path, index=False)
    text, meta = document_text(path)
    assert "ETS ALPHA" in text and meta["sheets"]


# --------------------------------------------------------------------------- LLM
def test_llm_max_tokens_retry_and_truncation_flag(isolated_data, monkeypatch):
    import anthropic

    from cmrmp.extract import llm

    calls = []

    class Resp:
        def __init__(self, stop, parsed):
            self.stop_reason, self.parsed_output, self.model, self.content = stop, parsed, "m", []
            self.usage = type("U", (), {"input_tokens": 1, "output_tokens": 2})()

    def fake_request(client, schema, content, max_tokens):
        calls.append(max_tokens)
        if max_tokens == llm.MAX_TOKENS:
            return Resp("max_tokens", None)
        return Resp("end_turn", schema(autorite=None, date_decision=None, entreprises=[]))

    monkeypatch.setattr(anthropic, "Anthropic", lambda: object())
    monkeypatch.setattr(llm, "_request", fake_request)
    monkeypatch.setattr(llm, "MAX_CHARS", 10)
    res = llm.extract("abc", "sanction", "x" * 50)
    assert calls == [llm.MAX_TOKENS, llm.MAX_TOKENS_RETRY]
    assert res["tronque"] is True and res["data"]["entreprises"] == []
    assert llm.MAX_PDF_BYTES * 4 / 3 < 32_000_000


# --------------------------------------------------------------------------- documents PRIDESOFT (liens _dl + iframe)
def test_pridesoft_download_links_follow_iframe(isolated_data, monkeypatch):
    from cmrmp.sources import common, documents

    links = [{"url": "https://pridesoft.armp.cm//0903_publications_dl?type_publication=DEC-ATTR&id_publication=1", "text": ""},
             {"url": "http://pridesoft.armp.cm//0903_dao_dl?type_publication=DEC-ATTR&id_publication=1", "text": ""},
             {"url": "https://armp.cm/details?type_publication=DEC-ATTR&id_publication=1", "text": ""}]
    assert len(common.document_links(links)) == 2
    assert len(common.document_links(links, settings()["katika"]["document_link_regex"])) == 2
    common.queue_documents("armp", "https://armp.cm/details", common.document_links(links))

    pdf = b"%PDF-1.4 test"

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if "publications_dl" in req.url.path:  # le nom du fichier contient une apostrophe
            return httpx.Response(200, html='<iframe width="1000" src="https://pridesoft.armp.cm/_lib/file/doc/0903/'
                                             "2026/04/21/C_X/Décision d'attribution (1).pdf\"></iframe>")
        if "dao_dl" in req.url.path:  # iframe vers un dossier : pas de document publié
            return httpx.Response(200, html='<iframe src="https://pridesoft.armp.cm/_lib/file/doc/0903/2026/04/21/C_X/"></iframe>')
        if req.url.path.endswith(".pdf"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    _patch_client(monkeypatch, documents, handler)
    stats = documents.download_all()
    assert stats["ok"] == 1 and stats["sans_fichier"] == 1 and stats["error"] == 0
    caps = [c for c in storage.iter_manifest() if c.content_type == "application/pdf"]
    assert len(caps) == 1 and caps[0].body() == pdf and caps[0].extra["via"].endswith("id_publication=1")


def test_audit_with_empty_tables(isolated_data):
    from typer.testing import CliRunner

    from cmrmp.cli import app

    tables.write({"avis": pd.DataFrame(), "soumissions": pd.DataFrame(), "documents": pd.DataFrame()})
    res = CliRunner().invoke(app, ["audit"])
    assert res.exit_code == 0, res.output
