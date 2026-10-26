"""Assemblage des tables finales (une ligne par lot) à partir des extractions."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from ..config import data_dir
from ..sources.common import interim
from ..extract.fields import normalize_reference, parse_fcfa, procedure_from_reference
from . import entities

log = logging.getLogger(__name__)


def _jsonl(name: str) -> list[dict]:
    path = interim(name)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _val(m: dict | None) -> int | None:
    return (m or {}).get("valeur_fcfa")


def _ref(r: str | None) -> str | None:
    return normalize_reference(r) if r else None


def build_publications() -> pd.DataFrame:
    """Index brut des publications listées par Katika, l'ARMP et PRIDESOFT."""
    rows = []
    for rec in _jsonl("katika_items.jsonl"):
        rows.append({"source": "katika", "url": rec.get("detail_url") or rec["list_url"], "reference": rec.get("reference"),
                     "doc_type": rec.get("doc_type"), "date": rec.get("date") or None, "texte": rec["text"],
                     "n_documents": len(rec.get("documents", [])), "fetched_at": rec.get("fetched_at")})
    for rec in _jsonl("armp_details.jsonl"):
        rows.append({"source": "armp", "url": rec["url"], "reference": rec.get("reference"),
                     "doc_type": rec.get("doc_type"), "date": None, "texte": rec["text"][:5000],
                     "n_documents": len(rec.get("documents", [])), "fetched_at": rec.get("fetched_at")})
    for rec in _jsonl("pridesoft_reports.jsonl"):
        rows.append({"source": "pridesoft", "url": rec["url"], "reference": rec.get("reference"),
                     "doc_type": "avis", "date": None, "texte": rec.get("text", "")[:5000],
                     "n_documents": len(rec.get("documents", [])), "fetched_at": rec.get("fetched_at")})
    return pd.DataFrame(rows)


def build_all() -> dict[str, pd.DataFrame]:
    docs_path = interim("extract_documents.parquet")
    docs = pd.read_parquet(docs_path) if docs_path.exists() else pd.DataFrame(columns=["doc_id", "url"])
    url_of = dict(zip(docs["doc_id"], docs["url"]))
    rx = docs.set_index("doc_id") if not docs.empty else docs
    llm = _jsonl("extract_llm.jsonl")

    avis, soum, attr, sanc = [], [], [], []
    for rec in llm:
        d, kind, doc_id = rec["data"], rec["kind"], rec["doc_id"]
        base = {"doc_id": doc_id, "url_source": url_of.get(doc_id), "modele_extraction": rec["model"]}
        if kind in ("avis", "dao", "additif"):
            ref = _ref(d.get("reference"))
            common = {
                **base, "reference": ref, "type_document": kind, "maitre_ouvrage": d.get("maitre_ouvrage"),
                "region": d.get("region"), "objet": d.get("objet"), "secteur": d.get("secteur"),
                "procedure": d.get("procedure") or procedure_from_reference(ref), "financement": d.get("financement"),
                "capacite_financiere_fcfa": _val(d.get("capacite_financiere")),
                "date_publication": d.get("date_publication"), "date_depot": d.get("date_depot"),
                "date_ouverture": d.get("date_ouverture"), "validite_offres_jours": d.get("validite_offres_jours"),
                "via_coleps": d.get("via_coleps"),
            }
            lots = d.get("lots") or [{}]
            for lot in lots:
                row = {**common, "lot": lot.get("numero"), "intitule_lot": lot.get("intitule"),
                       "cout_previsionnel_fcfa": _val(lot.get("cout_previsionnel")) or _val(d.get("cout_previsionnel")),
                       "cout_dossier_fcfa": _val(lot.get("cout_dossier")) or _val(d.get("cout_dossier")),
                       "caution_fcfa": _val(lot.get("caution")) or _val(d.get("caution"))}
                # Contrôle croisé avec les regex (sur le document entier, donc valable sans lots).
                if doc_id in getattr(rx, "index", []) and len(lots) == 1:
                    for f in ("cout_dossier_fcfa", "caution_fcfa", "capacite_financiere_fcfa"):
                        rv = rx.at[doc_id, f"rx_{f}"] if f"rx_{f}" in rx.columns else None
                        row[f"{f}_regex_concorde"] = None if pd.isna(rv) or row.get(f) is None else int(rv) == row[f]
                avis.append(row)
        elif kind in ("pv_ouverture", "pv_jugement"):
            for s in d.get("soumissions", []):
                soum.append({**base, "reference": _ref(d.get("reference")), "type_pv": d.get("type_pv"),
                             "maitre_ouvrage": d.get("maitre_ouvrage"), "date_seance": d.get("date_seance"),
                             "lot": s.get("lot"), "soumissionnaire_brut": s["soumissionnaire"],
                             "montant_offre_fcfa": _val(s.get("montant_offre")), "recevable": s.get("recevable"),
                             "rang": s.get("rang"), "motif_rejet": s.get("motif_rejet"), "niu": s.get("niu"),
                             "rccm": s.get("rccm"), "adresse": s.get("adresse"), "telephone": s.get("telephone"),
                             "email": s.get("email"), "representant": s.get("representant"),
                             "nombre_offres_annonce": d.get("nombre_offres_annonce")})
        elif kind in ("decision_attribution", "infructueux", "annulation"):
            for a in d.get("attributions") or [{}]:
                attr.append({**base, "reference": _ref(d.get("reference")), "maitre_ouvrage": d.get("maitre_ouvrage"),
                             "statut": d.get("statut"), "date_decision": d.get("date_decision"), "lot": a.get("lot"),
                             "attributaire_brut": a.get("attributaire"), "montant_fcfa": _val(a.get("montant")),
                             "delai_execution": a.get("delai_execution"), "niu": a.get("niu"), "rccm": a.get("rccm")})
        elif kind == "sanction":
            for e in d.get("entreprises", []):
                sanc.append({**base, "autorite": d.get("autorite"), "date_decision": d.get("date_decision"),
                             "entreprise_brut": e["nom"], "niu": e.get("niu"), "type_sanction": e.get("type_sanction"),
                             "duree_mois": e.get("duree_mois"), "motif": e.get("motif"),
                             "marche_reference": _ref(e.get("marche_reference"))})

    # Fiches ARMP « LIBELLÉ : valeur » : attributions sans LLM.
    for rec in _jsonl("armp_details.jsonl"):
        lv = rec.get("label_values", {})
        name = lv.get("attributaire") or lv.get("attributaires")
        if name:
            attr.append({"doc_id": None, "url_source": rec["url"], "modele_extraction": "regex",
                         "reference": _ref(rec.get("reference")), "maitre_ouvrage": lv.get("maitre_d_ouvrage") or lv.get("autorite_contractante"),
                         "statut": "attribue", "date_decision": None, "lot": lv.get("lot"), "attributaire_brut": name,
                         "montant_fcfa": parse_fcfa(lv.get("montant", "")), "delai_execution": lv.get("delai"),
                         "niu": None, "rccm": None})

    t = {
        "publications": build_publications(),
        "avis": pd.DataFrame(avis),
        "soumissions": pd.DataFrame(soum),
        "attributions": pd.DataFrame(attr),
        "sanctions": pd.DataFrame(sanc),
        "documents": docs.drop(columns=[c for c in ("raw_path", "text_path") if c in docs.columns]),
    }
    t.update(_resolve_entities(t))
    return t


def _resolve_entities(t: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    mentions = []
    for table, col in (("soumissions", "soumissionnaire_brut"), ("attributions", "attributaire_brut"), ("sanctions", "entreprise_brut")):
        df = t[table]
        if df.empty:
            continue
        for idx, row in df.iterrows():
            name = row.get(col)
            if not isinstance(name, str) or not name.strip():
                continue
            members = entities.split_groupement(name) if entities.is_groupement(name) else [name]
            for member in members:
                mentions.append({"table": table, "row": idx, "nom_brut": member, "groupement": len(members) > 1,
                                 "niu": row.get("niu") if len(members) == 1 else None,
                                 "rccm": row.get("rccm") if len(members) == 1 else None,
                                 "adresse": row.get("adresse"), "telephone": row.get("telephone"),
                                 "email": row.get("email"), "representant": row.get("representant")})
    if not mentions:
        return {"entreprises": pd.DataFrame(), "mentions": pd.DataFrame(), "er_paires_a_verifier": pd.DataFrame()}
    m, pairs = entities.resolve(pd.DataFrame(mentions))
    first_entity = m.groupby(["table", "row"])["entity_id"].first()
    for table, col in (("soumissions", "soumissionnaire_brut"), ("attributions", "attributaire_brut"), ("sanctions", "entreprise_brut")):
        if not t[table].empty:
            t[table]["entity_id"] = [first_entity.get((table, i)) for i in t[table].index]
    agg = m.groupby("entity_id").agg(
        nom_normalise=("nom_normalise", lambda s: s.value_counts().index[0]),
        graphies=("nom_brut", lambda s: sorted(set(s))[:50]),
        n_mentions=("nom_brut", "size"),
        niu=("niu", lambda s: sorted(set(s.dropna()))),
        rccm=("rccm", lambda s: sorted(set(s.dropna()))),
        adresses=("adresse", lambda s: sorted(set(s.dropna()))),
        telephones=("telephone", lambda s: sorted(set(s.dropna()))),
        emails=("email", lambda s: sorted(set(s.dropna()))),
        representants=("representant", lambda s: sorted(set(s.dropna()))),
    ).reset_index()
    return {"entreprises": agg, "mentions": m, "er_paires_a_verifier": entities.review_sample(pairs)}


def write(tables: dict[str, pd.DataFrame], out_dir: Path | None = None) -> Path:
    out_dir = out_dir or data_dir("processed")
    for name, df in tables.items():
        if df is None:
            continue
        df = df.copy()
        for col in df.columns:
            if df[col].dtype == object and df[col].map(lambda v: isinstance(v, (dict, list))).any():
                df[col] = df[col].map(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
        log.info("%-22s %8d lignes", name, len(df))
    return out_dir
