"""Bout en bout hors réseau : extractions LLM simulées -> tables -> graphe -> coûts -> pseudonymisation."""

import json

import pandas as pd

from cmrmp.build import graph, tables
from cmrmp.sources.common import interim
from cmrmp.publish.hf import dataset_card, pseudonymize


def _m(v):
    return {"valeur_fcfa": v, "citation": None}


def test_end_to_end(isolated_data):
    ref = "025/AONO/CAYI/2026"
    recs = [
        {"doc_id": "a1", "kind": "avis", "model": "test", "data": {
            "reference": ref, "maitre_ouvrage": "Commune de Yaoundé 1er", "region": "Centre", "objet": "salles",
            "secteur": "travaux", "procedure": "AONO", "financement": "BIP", "cout_dossier": _m(100000),
            "caution": _m(3100500), "capacite_financiere": _m(50000000), "cout_previsionnel": _m(103350000),
            "lots": [], "date_publication": "2026-03-20", "date_depot": "2026-04-05", "date_ouverture": "2026-04-05",
            "delai_execution": "4 mois", "validite_offres_jours": 90, "via_coleps": True}},
        {"doc_id": "p1", "kind": "pv_ouverture", "model": "test", "data": {
            "reference": ref, "maitre_ouvrage": "Commune de Yaoundé 1er", "type_pv": "ouverture",
            "date_seance": "2026-04-05", "nombre_offres_annonce": 3, "soumissions": [
                {"lot": None, "soumissionnaire": n, "montant_offre": _m(v), "rabais": None, "recevable": True,
                 "rang": None, "motif_rejet": None, "niu": None, "rccm": None, "adresse": None,
                 "telephone": tel, "email": None, "representant": rep}
                for n, v, tel, rep in [("ETS ALPHA SARL", 100_000_000, "699123456", "Jean X"),
                                       ("BETA BTP SA", 100_200_000, "699123456", "Jean X"),
                                       ("GAMMA SERVICES", 102_000_000, None, None)]]}},
        {"doc_id": "d1", "kind": "decision_attribution", "model": "test", "data": {
            "reference": ref, "maitre_ouvrage": "Commune de Yaoundé 1er", "statut": "attribue",
            "date_decision": "2026-05-02", "attributions": [
                {"lot": None, "attributaire": "Ets Alpha", "montant": _m(100_000_000), "delai_execution": "4 mois",
                 "niu": None, "rccm": None}]}},
    ]
    with (interim("extract_llm.jsonl")).open("w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")

    t = tables.build_all()
    assert len(t["soumissions"]) == 3 and t["soumissions"]["entity_id"].nunique() == 3
    t["aretes"] = graph.edges(t)
    rel = t["aretes"]["relation"].value_counts()
    assert rel["co_soumission"] == 3
    assert rel["meme_telephone"] == 1 and rel["meme_dirigeant"] == 1
    flags = graph.red_flags(t)
    assert bool(flags.loc[0, "flag_delai_court"]) and not bool(flags.loc[0, "flag_offre_unique"])
    summary, per_market = graph.relation_costs(t, t["aretes"])
    expected = 100000 + 3100500 * 0.12 * 90 / 365 + 103350000 * 0.005
    assert abs(per_market.loc[0, "cout_injection_cosoumission"] - expected) < 1
    out = tables.write({**t, "couts_relations": summary, "couts_marches": per_market})
    assert (out / "aretes.parquet").exists()

    anon = pseudonymize(pd.read_parquet(out / "aretes.parquet"), "sel")
    assert not anon["evidence"].str.contains("Jean X").any()
    assert "configs:" in dataset_card("u/d", {"avis": 1})
