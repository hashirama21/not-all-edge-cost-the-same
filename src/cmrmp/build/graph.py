"""Graphe hétérogène, signaux d'alerte et coûts de manipulation calibrés en FCFA."""

from __future__ import annotations

import itertools
import json
import re

import numpy as np
import pandas as pd

from ..config import settings


def _pairs(df: pd.DataFrame, group_cols: list[str], relation: str, evidence_col: str | None = None) -> list[dict]:
    rows = []
    for key, grp in df.groupby(group_cols):
        ents = sorted(set(grp["entity_id"].dropna()))
        if len(ents) < 2 or len(ents) > 60:  # au-delà, valeur générique (adresse de centre d'affaires…)
            continue
        ev = key if isinstance(key, str) else "|".join(map(str, key))
        for a, b in itertools.combinations(ents, 2):
            rows.append({"src": a, "dst": b, "relation": relation, "evidence": ev})
    return rows


def _norm_contact(v: str | None, kind: str) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    if kind == "telephone":
        d = re.sub(r"\D", "", v)
        return d[-9:] if len(d) >= 9 else None
    return re.sub(r"\s+", " ", v.strip().lower())


def edges(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    soum, attr, mentions = t["soumissions"], t["attributions"], t.get("mentions", pd.DataFrame())
    rows: list[dict] = []
    if not soum.empty:
        s = soum.dropna(subset=["reference", "entity_id"]).assign(lot=lambda d: d["lot"].fillna("unique"))
        rows += _pairs(s, ["reference", "lot"], "co_soumission")
    if not mentions.empty:
        for col, rel in (("adresse", "meme_adresse"), ("telephone", "meme_telephone"),
                         ("email", "meme_email"), ("representant", "meme_dirigeant"),
                         ("niu", "meme_niu"), ("rccm", "meme_rccm")):
            if col in mentions:
                mm = mentions.assign(_k=mentions[col].map(lambda v: _norm_contact(v, col))).dropna(subset=["_k"])
                rows += _pairs(mm, ["_k"], rel)
    e = pd.DataFrame(rows, columns=["src", "dst", "relation", "evidence"])
    if not attr.empty:
        a = attr.dropna(subset=["entity_id", "maitre_ouvrage"])
        hist = a.groupby(["entity_id", "maitre_ouvrage"]).size().reset_index(name="n_attributions")
        hist = hist.rename(columns={"entity_id": "src", "maitre_ouvrage": "dst"}).assign(relation="historique_maitre_ouvrage", evidence="")
        e = pd.concat([e, hist], ignore_index=True)
        e = pd.concat([e, _succession(a)], ignore_index=True)
    return e.drop_duplicates(["src", "dst", "relation", "evidence"])


def _succession(a: pd.DataFrame) -> pd.DataFrame:
    """Attributaires successifs chez un même maître d'ouvrage (signal de rotation)."""
    rows = []
    a = a.dropna(subset=["date_decision"]).sort_values("date_decision")
    for _, grp in a.groupby("maitre_ouvrage"):
        ids = grp["entity_id"].tolist()
        for x, y in zip(ids, ids[1:]):
            if x != y:
                rows.append({"src": x, "dst": y, "relation": "succession_attribution", "evidence": grp["maitre_ouvrage"].iloc[0]})
    return pd.DataFrame(rows, columns=["src", "dst", "relation", "evidence"])


# --------------------------------------------------------------------------- signaux d'alerte
def red_flags(t: dict[str, pd.DataFrame], short_window_days: int = 21) -> pd.DataFrame:
    """Un rang par (référence, lot). Les signaux sont des étiquettes faibles : à NE PAS
    réutiliser comme variables du modèle évalué (règle anti-circularité)."""
    avis, soum, attr = t["avis"], t["soumissions"], t["attributions"]
    if avis.empty:
        return pd.DataFrame()
    a = avis.copy()
    a["lot"] = a["lot"].fillna("unique")
    for col in ("date_publication", "date_depot"):
        a[col] = pd.to_datetime(a[col], errors="coerce")
    a["delai_depot_jours"] = (a["date_depot"] - a["date_publication"]).dt.days
    a["flag_delai_court"] = a["delai_depot_jours"] < short_window_days
    a["flag_procedure_non_ouverte"] = a["procedure"].fillna("").str.upper().isin(["AONR", "AOIR", "GG", "DC", "DCU"])

    if not soum.empty:
        s = soum.assign(lot=soum["lot"].fillna("unique"))
        g = s.groupby(["reference", "lot"])
        stats = pd.DataFrame({
            "n_offres": g["entity_id"].nunique(),
            "n_offres_recevables": g["recevable"].apply(lambda x: int((x == True).sum())),  # noqa: E712
            "cv_offres": g["montant_offre_fcfa"].apply(lambda x: x.std() / x.mean() if x.notna().sum() >= 2 and x.mean() else np.nan),
            "ecart_deux_premieres": g["montant_offre_fcfa"].apply(_gap_two_lowest),
        }).reset_index()
        a = a.merge(stats, on=["reference", "lot"], how="left")
        a["flag_offre_unique"] = a["n_offres"] == 1
        a["flag_offres_groupees"] = a["cv_offres"] < 0.02
        a["flag_ecart_minime"] = a["ecart_deux_premieres"] < 0.005
    if not attr.empty:
        w = attr.assign(lot=attr["lot"].fillna("unique"))[["reference", "lot", "entity_id", "montant_fcfa", "maitre_ouvrage"]]
        w = w.rename(columns={"entity_id": "attributaire_id", "maitre_ouvrage": "mo_attr"})
        a = a.merge(w.drop_duplicates(["reference", "lot"]), on=["reference", "lot"], how="left")
        a["ratio_attribution_estimation"] = a["montant_fcfa"] / a["cout_previsionnel_fcfa"]
        a["flag_proche_estimation"] = a["ratio_attribution_estimation"].between(0.97, 1.0)
        share = attr.dropna(subset=["entity_id", "maitre_ouvrage"]).groupby("maitre_ouvrage")["entity_id"].value_counts(normalize=True)
        a["part_attributaire_chez_mo"] = [share.get((mo, e), np.nan) if isinstance(mo, str) else np.nan
                                          for mo, e in zip(a["maitre_ouvrage"], a["attributaire_id"])]
        a["flag_attributaire_recurrent"] = a["part_attributaire_chez_mo"] > 0.5
    flag_cols = [c for c in a.columns if c.startswith("flag_")]
    a["score_alerte"] = a[flag_cols].astype("float").mean(axis=1, skipna=True)  # indice composite à la Fazekas
    return a


def _gap_two_lowest(x: pd.Series) -> float:
    v = np.sort(x.dropna().to_numpy())
    return (v[1] - v[0]) / v[0] if len(v) >= 2 and v[0] > 0 else np.nan


# --------------------------------------------------------------------------- coûts calibrés
def relation_costs(t: dict[str, pd.DataFrame], e: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Coût de création (injection) et de suppression de chaque lien, en FCFA.

    co_soumission : coût du dossier + coût d'opportunité de la caution sur la durée de
    validité des offres + préparation de l'offre (part du montant). Les valeurs viennent
    des avis ; les hypothèses non observables sont dans config/sources.yaml (section costs).
    """
    c = settings()["costs"]
    avis = t["avis"].assign(lot=t["avis"]["lot"].fillna("unique")) if not t["avis"].empty else pd.DataFrame()
    per_market = pd.DataFrame()
    if not avis.empty:
        days = avis["validite_offres_jours"].fillna(c["caution_days_default"])
        per_market = avis[["reference", "lot", "cout_dossier_fcfa", "caution_fcfa", "cout_previsionnel_fcfa"]].copy()
        per_market["cout_opportunite_caution"] = avis["caution_fcfa"] * c["caution_rate_annual"] * days / 365
        per_market["cout_preparation"] = avis["cout_previsionnel_fcfa"] * c["bid_preparation_share"]
        per_market["cout_injection_cosoumission"] = per_market[["cout_dossier_fcfa", "cout_opportunite_caution", "cout_preparation"]].sum(axis=1, min_count=1)
        per_market["cout_injection_part_marche"] = per_market["cout_injection_cosoumission"] / per_market["cout_previsionnel_fcfa"]

    def q(s: pd.Series) -> dict:
        s = s.dropna()
        return {"n": int(len(s)), "p25": s.quantile(0.25) if len(s) else None,
                "mediane": s.median() if len(s) else None, "p75": s.quantile(0.75) if len(s) else None}

    inj = per_market["cout_injection_cosoumission"] if not per_market.empty else pd.Series(dtype=float)
    table = [
        {"relation": "co_soumission", "creer": "dossier + caution immobilisée + offre", "cacher": "ne pas soumissionner",
         "cout_creation": json.dumps(q(inj), default=float), "cout_suppression_fcfa": 0, "mesure": "avis (observé)"},
        {"relation": "historique_maitre_ouvrage", "creer": "gagner un marché", "cacher": "impossible a posteriori",
         "cout_creation": "non manipulable à court terme", "cout_suppression_fcfa": None, "mesure": "structurel"},
        {"relation": "meme_adresse", "creer": "déclarer l'adresse", "cacher": "déclarer une autre adresse",
         "cout_creation": "faible", "cout_suppression_fcfa": None, "mesure": "fréquence des changements observés"},
        {"relation": "meme_telephone", "creer": "quasi gratuit", "cacher": "quasi gratuit",
         "cout_creation": 0, "cout_suppression_fcfa": 0, "mesure": "hypothèse"},
        {"relation": "meme_email", "creer": "quasi gratuit", "cacher": "quasi gratuit",
         "cout_creation": 0, "cout_suppression_fcfa": 0, "mesure": "hypothèse"},
        {"relation": "meme_dirigeant", "creer": "nommer la personne", "cacher": "prête-nom",
         "cout_creation": c["statute_change_fcfa"], "cout_suppression_fcfa": c["statute_change_fcfa"], "mesure": "hypothèse (frais statutaires)"},
        {"relation": "meme_niu", "creer": "impossible sans fraude", "cacher": "créer une société",
         "cout_creation": None, "cout_suppression_fcfa": c["company_creation_fcfa"], "mesure": "frais CFCE (à vérifier)"},
        {"relation": "succession_attribution", "creer": "perdre volontairement", "cacher": "—",
         "cout_creation": "signal", "cout_suppression_fcfa": None, "mesure": "signal, pas une action"},
    ]
    summary = pd.DataFrame(table)
    summary["cout_creation"] = summary["cout_creation"].astype(str)
    summary["cout_suppression_fcfa"] = pd.to_numeric(summary["cout_suppression_fcfa"], errors="coerce")
    if not e.empty and not per_market.empty:
        cos = e[e["relation"] == "co_soumission"].copy()
        cos[["reference", "lot"]] = cos["evidence"].str.rsplit("|", n=1, expand=True)
        cos = cos.merge(per_market[["reference", "lot", "cout_injection_cosoumission", "cout_injection_part_marche"]],
                        on=["reference", "lot"], how="left")
        per_market = per_market.merge(cos.groupby(["reference", "lot"]).size().rename("n_liens").reset_index(), on=["reference", "lot"], how="left")
    return summary, per_market
