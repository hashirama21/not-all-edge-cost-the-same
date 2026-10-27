"""Assemblage des tables finales (une ligne par lot) à partir des extractions.

Sources, par ordre de priorité : extraction LLM, puis repli regex (champs `rx_*` des
documents, fiches ARMP « LIBELLÉ : valeur », listes d'entreprises des communiqués MINMAP).
Les lots sont normalisés (« Lot n°01 » -> « 1 », absent -> « unique ») pour que avis, PV et
attributions se joignent sur (référence, lot).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pandas as pd

from ..config import data_dir
from ..extract.fields import normalize_lot, normalize_reference, parse_fcfa, procedure_from_reference
from ..sources.common import interim
from ..storage import read_jsonl
from . import entities

log = logging.getLogger(__name__)

AVIS_KINDS = ("avis", "dao", "additif")
# Plusieurs documents d'un même marché : l'avis prime sur l'additif, puis sur le DAO.
AVIS_PRIORITY = {"avis": 0, "additif": 1, "dao": 2}

_NOMBRES = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7,
            "huit": 8, "neuf": 9, "dix": 10, "douze": 12, "quinze": 15, "dix-huit": 18, "vingt-quatre": 24}


def _jsonl(name: str) -> list[dict]:
    return read_jsonl(interim(name))


def _val(m: dict | None) -> int | None:
    return (m or {}).get("valeur_fcfa")


def _ref(r: str | None) -> str | None:
    return normalize_reference(r) if isinstance(r, str) and r.strip() else None


def _int(v) -> int | None:
    return None if v is None or pd.isna(v) else int(v)


def _str(v) -> str | None:
    return None if v is None or (not isinstance(v, str) and pd.isna(v)) else str(v)


def _duree_mois(duree: str | None) -> int | None:
    """« deux ans » -> 24 ; « 6 mois » -> 6."""
    if not duree:
        return None
    m = re.match(r"\s*([\w\-]+)\s+(ans?|mois)", duree.lower())
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else _NOMBRES.get(m.group(1))
    if n is None:
        return None
    return n * 12 if m.group(2).startswith("an") else n


def _sanction_type(text: str) -> str:
    t = text.lower()
    for kind, rx in (("exclusion", r"exclu"), ("suspension", r"suspen"), ("resiliation", r"r[ée]siliation"),
                     ("mise_en_demeure", r"mise\s+en\s+demeure")):
        if re.search(rx, t):
            return kind
    return "autre"


def build_publications() -> pd.DataFrame:
    """Index brut des publications listées par Katika, l'ARMP et PRIDESOFT (une ligne par URL)."""
    rows = []
    for rec in _jsonl("katika_items.jsonl"):
        rows.append({"source": "katika", "url": rec.get("detail_url") or rec["list_url"], "reference": rec.get("reference"),
                     "doc_type": rec.get("doc_type"), "date": rec.get("date") or None, "texte": rec["text"],
                     "n_documents": len(rec.get("documents", [])), "fetched_at": rec.get("fetched_at")})
    for rec in _jsonl("armp_details.jsonl"):
        rows.append({"source": "armp", "url": rec["url"], "reference": rec.get("reference"),
                     "doc_type": rec.get("doc_type"), "date": None, "texte": rec.get("text", "")[:5000],
                     "n_documents": len(rec.get("documents", [])), "fetched_at": rec.get("fetched_at")})
    for rec in _jsonl("pridesoft_reports.jsonl"):
        rows.append({"source": "pridesoft", "url": rec["url"], "reference": rec.get("reference"),
                     "doc_type": "avis", "date": None, "texte": rec.get("text", "")[:5000],
                     "n_documents": len(rec.get("documents", [])), "fetched_at": rec.get("fetched_at")})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # Les anciens fichiers peuvent contenir des doublons de relance : la dernière capture prime.
    return df.drop_duplicates(["source", "url", "texte"], keep="last").reset_index(drop=True)


def _dedupe_avis(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.assign(_p_model=(df["modele_extraction"] == "regex").astype(int),
                   _p_type=df["type_document"].map(AVIS_PRIORITY).fillna(9))
    df = df.sort_values(["_p_model", "_p_type"], kind="stable")
    has_ref = df["reference"].notna()
    out = pd.concat([df[has_ref].drop_duplicates(["reference", "lot"]),
                     df[~has_ref].drop_duplicates(["doc_id", "lot"])])
    return out.sort_index().drop(columns=["_p_model", "_p_type"]).reset_index(drop=True)


def _dedupe_attributions(df: pd.DataFrame) -> pd.DataFrame:
    """Une attribution par (référence, lot, attributaire) ; le LLM prime sur la regex ARMP."""
    if df.empty:
        return df
    name = df["attributaire_brut"].map(lambda v: entities.name_key(v) if isinstance(v, str) else "")
    df = df.assign(_p=(df["modele_extraction"] == "regex").astype(int), _name=name).sort_values("_p", kind="stable")
    has_ref = df["reference"].notna()
    out = pd.concat([df[has_ref].drop_duplicates(["reference", "lot", "_name"]),
                     df[~has_ref].drop_duplicates(["url_source", "lot", "_name"])])
    return out.sort_index().drop(columns=["_p", "_name"]).reset_index(drop=True)


def _avis_from_regex(docs: pd.DataFrame, done: set[str]) -> list[dict]:
    """Repli sans LLM : une ligne par document d'avis à partir des champs `rx_*`."""
    if docs.empty or "doc_type" not in docs:
        return []
    rows = []
    todo = docs[docs["doc_type"].isin(AVIS_KINDS) & ~docs["doc_id"].isin(done)]
    for r in todo.to_dict("records"):
        ref = _ref(r.get("rx_reference"))
        rows.append({
            "doc_id": r["doc_id"], "url_source": r.get("url"), "modele_extraction": "regex", "reference": ref,
            "type_document": r["doc_type"], "maitre_ouvrage": None, "region": None, "objet": None, "secteur": None,
            "procedure": _str(r.get("rx_procedure")) or procedure_from_reference(ref), "financement": None,
            "capacite_financiere_fcfa": _int(r.get("rx_capacite_financiere_fcfa")),
            "date_publication": None, "date_depot": _str(r.get("rx_date_depot")),
            "date_ouverture": _str(r.get("rx_date_ouverture")), "validite_offres_jours": None, "via_coleps": None,
            "lot": "unique", "intitule_lot": None,
            "cout_previsionnel_fcfa": _int(r.get("rx_cout_previsionnel_fcfa")),
            "cout_dossier_fcfa": _int(r.get("rx_cout_dossier_fcfa")),
            "caution_fcfa": _int(r.get("rx_caution_fcfa")),
        })
    return rows


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
        if kind in AVIS_KINDS:
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
                row = {**common, "lot": normalize_lot(lot.get("numero")), "intitule_lot": lot.get("intitule"),
                       "cout_previsionnel_fcfa": _val(lot.get("cout_previsionnel")) or _val(d.get("cout_previsionnel")),
                       "cout_dossier_fcfa": _val(lot.get("cout_dossier")) or _val(d.get("cout_dossier")),
                       "caution_fcfa": _val(lot.get("caution")) or _val(d.get("caution"))}
                # Contrôle croisé avec les regex (sur le document entier, donc valable sans lots).
                if doc_id in getattr(rx, "index", []) and len(lots) == 1:
                    for f in ("cout_dossier_fcfa", "caution_fcfa", "capacite_financiere_fcfa"):
                        rv = rx.at[doc_id, f"rx_{f}"] if f"rx_{f}" in rx.columns else None
                        row[f"{f}_regex_concorde"] = None if rv is None or pd.isna(rv) or row.get(f) is None else int(rv) == row[f]
                avis.append(row)
        elif kind in ("pv_ouverture", "pv_jugement"):
            for s in d.get("soumissions", []):
                soum.append({**base, "reference": _ref(d.get("reference")), "type_pv": d.get("type_pv"),
                             "maitre_ouvrage": d.get("maitre_ouvrage"), "date_seance": d.get("date_seance"),
                             "lot": normalize_lot(s.get("lot")), "soumissionnaire_brut": s["soumissionnaire"],
                             "montant_offre_fcfa": _val(s.get("montant_offre")), "recevable": s.get("recevable"),
                             "rang": s.get("rang"), "motif_rejet": s.get("motif_rejet"), "niu": s.get("niu"),
                             "rccm": s.get("rccm"), "adresse": s.get("adresse"), "telephone": s.get("telephone"),
                             "email": s.get("email"), "representant": s.get("representant"),
                             "nombre_offres_annonce": d.get("nombre_offres_annonce")})
        elif kind in ("decision_attribution", "infructueux", "annulation"):
            for a in d.get("attributions") or [{}]:
                attr.append({**base, "reference": _ref(d.get("reference")), "maitre_ouvrage": d.get("maitre_ouvrage"),
                             "statut": d.get("statut"), "date_decision": d.get("date_decision"),
                             "lot": normalize_lot(a.get("lot")), "attributaire_brut": a.get("attributaire"),
                             "montant_fcfa": _val(a.get("montant")), "delai_execution": a.get("delai_execution"),
                             "niu": a.get("niu"), "rccm": a.get("rccm")})
        elif kind == "sanction":
            for e in d.get("entreprises", []):
                sanc.append({**base, "autorite": d.get("autorite"), "date_decision": d.get("date_decision"),
                             "entreprise_brut": e["nom"], "niu": e.get("niu"), "type_sanction": e.get("type_sanction"),
                             "duree_mois": e.get("duree_mois"), "motif": e.get("motif"),
                             "marche_reference": _ref(e.get("marche_reference"))})

    # Avis sans extraction LLM : champs regex du document.
    avis += _avis_from_regex(docs, {r["doc_id"] for r in avis})

    # PV sans extraction LLM : soumissions lues dans les tableaux PyMuPDF.
    tb_path = interim("extract_table_bids.parquet")
    if tb_path.exists():
        done = {r["doc_id"] for r in soum}
        tb = pd.read_parquet(tb_path)
        for r in tb[~tb["doc_id"].isin(done)].to_dict("records") if not tb.empty else []:
            soum.append({"doc_id": r["doc_id"], "url_source": url_of.get(r["doc_id"]), "modele_extraction": "pymupdf_tables",
                         "reference": _ref(r.get("reference")), "type_pv": None, "maitre_ouvrage": None,
                         "date_seance": None, "lot": "unique", "soumissionnaire_brut": r["soumissionnaire"],
                         "montant_offre_fcfa": r.get("montant_offre_fcfa"), "recevable": r.get("recevable"),
                         "rang": r.get("rang"), "motif_rejet": r.get("observation"), "niu": None, "rccm": None,
                         "adresse": None, "telephone": None, "email": None, "representant": None,
                         "nombre_offres_annonce": None})

    # Fiches ARMP « LIBELLÉ : valeur » : attributions sans LLM.
    for rec in _jsonl("armp_details.jsonl"):
        lv = rec.get("label_values", {})
        name = lv.get("attributaire") or lv.get("attributaires")
        if name:
            attr.append({"doc_id": None, "url_source": rec["url"], "modele_extraction": "regex",
                         "reference": _ref(rec.get("reference")),
                         "maitre_ouvrage": lv.get("maitre_d_ouvrage") or lv.get("maitre_ouvrage") or lv.get("autorite_contractante"),
                         "statut": "attribue", "date_decision": None, "lot": normalize_lot(lv.get("lot")),
                         "attributaire_brut": name,
                         "montant_fcfa": parse_fcfa(lv.get("montant") or lv.get("montant_ttc") or ""),
                         "delai_execution": lv.get("delai") or lv.get("delai_d_execution"),
                         "niu": None, "rccm": None})

    # Communiqués MINMAP sans extraction LLM de leurs documents : liste d'entreprises par regex.
    llm_sanction_docs = {r["doc_id"] for r in sanc}
    covered = {r["url_source"] for r in sanc}
    if "referer" in docs.columns:
        covered |= set(docs.loc[docs["doc_id"].isin(llm_sanction_docs), "referer"].dropna())
    for rec in _jsonl("minmap_sanctions_raw.jsonl"):
        if rec.get("url") in covered:
            continue
        kind = _sanction_type(f"{rec.get('title', '')} {rec.get('text', '')[:3000]}")
        for name in dict.fromkeys(rec.get("entreprises_regex") or []):
            sanc.append({"doc_id": None, "url_source": rec.get("url"), "modele_extraction": "regex", "autorite": "MINMAP",
                         "date_decision": (rec.get("published") or "")[:10] or None, "entreprise_brut": name,
                         "niu": None, "type_sanction": kind, "duree_mois": _duree_mois(rec.get("duree")),
                         "motif": None, "marche_reference": None})

    sanctions = pd.DataFrame(sanc)
    if not sanctions.empty:
        sanctions = sanctions.drop_duplicates(["url_source", "entreprise_brut", "doc_id"]).reset_index(drop=True)
    t = {
        "publications": build_publications(),
        "avis": _dedupe_avis(pd.DataFrame(avis)),
        "soumissions": pd.DataFrame(soum),
        "attributions": _dedupe_attributions(pd.DataFrame(attr)),
        "sanctions": sanctions,
        "documents": docs.drop(columns=[c for c in ("raw_path", "text_path", "meta_path") if c in docs.columns]),
    }
    t.update(_resolve_entities(t))
    return t


TABLE_NAME_COLS = (("soumissions", "soumissionnaire_brut"), ("attributions", "attributaire_brut"), ("sanctions", "entreprise_brut"))


def _resolve_entities(t: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    mentions = []
    for table, col in TABLE_NAME_COLS:
        df = t[table]
        if df.empty:
            continue
        for idx, row in df.iterrows():
            name = row.get(col)
            if not isinstance(name, str) or not name.strip():
                continue
            members = entities.split_groupement(name) if entities.is_groupement(name) else [name]
            members = members or [name]
            for member in members:
                mentions.append({"table": table, "row": idx, "nom_brut": member, "groupement": len(members) > 1,
                                 "niu": row.get("niu") if len(members) == 1 else None,
                                 "rccm": row.get("rccm") if len(members) == 1 else None,
                                 "adresse": row.get("adresse"), "telephone": row.get("telephone"),
                                 "email": row.get("email"), "representant": row.get("representant")})
    if not mentions:
        return {"entreprises": pd.DataFrame(), "mentions": pd.DataFrame(), "er_paires_a_verifier": pd.DataFrame()}
    m, pairs = entities.resolve(pd.DataFrame(mentions))
    # Une ligne de groupement porte l'entité de son premier membre (`entity_id`) et tous ses
    # membres (`entity_ids`) ; le graphe lit les mentions pour relier chacun d'eux.
    members = m.groupby(["table", "row"])["entity_id"].agg(lambda s: list(dict.fromkeys(s)))
    for table, _ in TABLE_NAME_COLS:
        if not t[table].empty:
            ids = [members.get((table, i)) for i in t[table].index]
            t[table]["entity_id"] = [x[0] if x else None for x in ids]
            t[table]["entity_ids"] = [x if x else [] for x in ids]
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
    from ..time import utc_now_iso
    for name, df in tables.items():
        if df is None:
            continue
        df = df.copy()
        for col in df.columns:
            if df[col].dtype == object and df[col].map(lambda v: isinstance(v, (dict, list))).any():
                df[col] = df[col].map(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        # Add provenance timestamp if missing
        if "written_at_utc" not in df.columns:
            df["written_at_utc"] = utc_now_iso("seconds")
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
        log.info("%-22s %8d lignes", name, len(df))
    return out_dir
