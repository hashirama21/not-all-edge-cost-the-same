"""Graphe hétérogène, signaux d'alerte et coûts de manipulation calibrés en FCFA.

Les liens « même NIU » et « même RCCM » ne sont pas des arêtes : la résolution d'entités
fusionne déjà les noms qui partagent un identifiant (entities.resolve), ils forment donc
une seule entité. Le coût de les cacher (créer une société) reste dans config (costs).
"""

from __future__ import annotations

import itertools
import json
import re

import numpy as np
import pandas as pd

from ..config import settings

MAX_GROUP = 60          # au-delà, valeur générique (adresse de centre d'affaires…)
MIN_MO_ATTRIBUTIONS = 3  # part de l'attributaire chez le maître d'ouvrage : pas en dessous


def _pairs(df: pd.DataFrame, group_cols: list[str], relation: str) -> list[dict]:
    rows = []
    for key, grp in df.groupby(group_cols):
        ents = sorted(set(grp["entity_id"].dropna()))
        if len(ents) < 2 or len(ents) > MAX_GROUP:
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


def _bid_members(soum: pd.DataFrame, mentions: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par (offre, entité) : tous les membres d'un groupement, pas seulement le premier."""
    keys = soum[["reference", "lot"]].assign(lot=lambda d: d["lot"].fillna("unique"))
    if not mentions.empty and {"table", "row", "entity_id"} <= set(mentions.columns):
        sm = mentions.loc[mentions["table"] == "soumissions", ["row", "entity_id"]]
        sm = sm.join(keys, on="row")
    else:
        sm = soum[["entity_id"]].join(keys).rename_axis("row").reset_index()
    return sm.dropna(subset=["reference", "entity_id"])


def _cosubmission(sm: pd.DataFrame) -> list[dict]:
    """co_soumission entre offres distinctes d'un même lot ; meme_groupement entre membres d'une offre."""
    rows = []
    for (ref, lot), grp in sm.groupby(["reference", "lot"]):
        ev = f"{ref}|{lot}"
        by_bid = [set(s) for _, s in grp.groupby("row")["entity_id"]]
        ents = set().union(*by_bid)
        if len(ents) < 2 or len(ents) > MAX_GROUP:
            continue
        co, gp = set(), set()
        for i, a_set in enumerate(by_bid):
            gp |= {tuple(sorted(p)) for p in itertools.combinations(sorted(a_set), 2)}
            for b_set in by_bid[i + 1:]:
                co |= {tuple(sorted((a, b))) for a in a_set for b in b_set if a != b}
        rows += [{"src": a, "dst": b, "relation": "co_soumission", "evidence": ev} for a, b in sorted(co)]
        rows += [{"src": a, "dst": b, "relation": "meme_groupement", "evidence": ev} for a, b in sorted(gp)]
    return rows


def edges(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    soum, attr, mentions = t["soumissions"], t["attributions"], t.get("mentions", pd.DataFrame())
    rows: list[dict] = []
    if not soum.empty:
        rows += _cosubmission(_bid_members(soum, mentions))
    if not mentions.empty:
        for col, rel in (("adresse", "meme_adresse"), ("telephone", "meme_telephone"),
                         ("email", "meme_email"), ("representant", "meme_dirigeant")):
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
    return e.drop_duplicates(["src", "dst", "relation", "evidence"]).reset_index(drop=True)


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
def _flag(cond: pd.Series, valid: pd.Series) -> pd.Series:
    """Signal booléen nullable : NA quand la donnée nécessaire manque (pas « faux »)."""
    out = cond.fillna(False).astype("boolean")
    out[~valid.fillna(False).astype(bool)] = pd.NA
    return out


def red_flags(t: dict[str, pd.DataFrame], short_window_days: int = 21) -> pd.DataFrame:
    """Un rang par (référence, lot). Les signaux sont des étiquettes faibles : à NE PAS
    réutiliser comme variables du modèle évalué (règle anti-circularité).

    Un signal vaut NA quand on ne peut pas le calculer ; le score composite est la moyenne
    des signaux disponibles (NaN si aucun)."""
    avis, soum, attr = t["avis"], t["soumissions"], t["attributions"]
    if avis.empty:
        return pd.DataFrame()
    a = avis.copy()
    a["lot"] = a["lot"].fillna("unique")
    for col in ("date_publication", "date_depot"):
        a[col] = pd.to_datetime(a[col], errors="coerce")
    a["delai_depot_jours"] = (a["date_depot"] - a["date_publication"]).dt.days
    a["flag_delai_court"] = _flag(a["delai_depot_jours"] < short_window_days, a["delai_depot_jours"].notna())
    proc = a["procedure"].astype("string").str.upper().str.strip()
    a["flag_procedure_non_ouverte"] = _flag(proc.isin(["AONR", "AOIR", "GG", "DC", "DCU"]), proc.notna() & (proc != ""))

    if not soum.empty:
        s = soum.dropna(subset=["reference"]).assign(lot=lambda d: d["lot"].fillna("unique"))
        g = s.groupby(["reference", "lot"])
        stats = pd.DataFrame({
            "n_offres": g["entity_id"].nunique(),
            "n_offres_recevables": g["recevable"].apply(lambda x: int((x == True).sum())),  # noqa: E712
            "cv_offres": g["montant_offre_fcfa"].apply(lambda x: x.std() / x.mean() if x.notna().sum() >= 2 and x.mean() else np.nan),
            "ecart_deux_premieres": g["montant_offre_fcfa"].apply(_gap_two_lowest),
        }).reset_index()
        a = a.merge(stats, on=["reference", "lot"], how="left")
        a["flag_offre_unique"] = _flag(a["n_offres"] == 1, a["n_offres"].notna() & (a["n_offres"] > 0))
        a["flag_offres_groupees"] = _flag(a["cv_offres"] < 0.02, a["cv_offres"].notna())
        a["flag_ecart_minime"] = _flag(a["ecart_deux_premieres"] < 0.005, a["ecart_deux_premieres"].notna())
    if not attr.empty:
        w = attr.dropna(subset=["reference"]).assign(lot=lambda d: d["lot"].fillna("unique"))
        w = w[["reference", "lot", "entity_id", "montant_fcfa", "maitre_ouvrage"]]
        w = w.rename(columns={"entity_id": "attributaire_id", "maitre_ouvrage": "mo_attr"})
        a = a.merge(w.drop_duplicates(["reference", "lot"]), on=["reference", "lot"], how="left")
        a["ratio_attribution_estimation"] = a["montant_fcfa"] / a["cout_previsionnel_fcfa"]
        a["flag_proche_estimation"] = _flag(a["ratio_attribution_estimation"].between(0.97, 1.0),
                                            a["ratio_attribution_estimation"].notna())
        a["part_attributaire_chez_mo"] = _leave_one_out_share(a, attr)
        a["flag_attributaire_recurrent"] = _flag(a["part_attributaire_chez_mo"] > 0.5, a["part_attributaire_chez_mo"].notna())
    flag_cols = [c for c in a.columns if c.startswith("flag_")]
    a["n_signaux_calcules"] = a[flag_cols].notna().sum(axis=1)
    a["score_alerte"] = a[flag_cols].astype("Float64").mean(axis=1, skipna=True).astype(float)  # indice composite à la Fazekas
    return a


def _leave_one_out_share(a: pd.DataFrame, attr: pd.DataFrame) -> list[float]:
    """Part des AUTRES marchés du maître d'ouvrage remportés par l'attributaire.

    Le marché courant est exclu (sinon un maître d'ouvrage à un seul marché donne 100 %),
    et la part n'est calculée qu'à partir de MIN_MO_ATTRIBUTIONS attributions chez lui."""
    at = attr.dropna(subset=["entity_id", "maitre_ouvrage"]).assign(lot=lambda d: d["lot"].fillna("unique"))
    at = at.drop_duplicates(["maitre_ouvrage", "reference", "lot", "entity_id"])
    n_mo = at.groupby("maitre_ouvrage").size()
    n_mo_e = at.groupby(["maitre_ouvrage", "entity_id"]).size()
    out = []
    for mo_attr, mo_avis, e in zip(a.get("mo_attr", pd.Series(index=a.index, dtype=object)), a["maitre_ouvrage"], a["attributaire_id"]):
        own = isinstance(mo_attr, str)  # le marché courant figure dans `at` sous ce maître d'ouvrage
        mo = mo_attr if own else mo_avis
        if not isinstance(mo, str) or not isinstance(e, str):
            out.append(np.nan)
            continue
        total, k = int(n_mo.get(mo, 0)), int(n_mo_e.get((mo, e), 0))
        if own:
            total, k = total - 1, k - 1
        out.append(k / total if total >= MIN_MO_ATTRIBUTIONS else np.nan)
    return out


def _gap_two_lowest(x: pd.Series) -> float:
    v = np.sort(x.dropna().to_numpy())
    return (v[1] - v[0]) / v[0] if len(v) >= 2 and v[0] > 0 else np.nan


# --------------------------------------------------------------------------- coûts calibrés
COST_COMPONENTS = ("cout_dossier_fcfa", "cout_opportunite_caution", "cout_preparation")


def relation_costs(t: dict[str, pd.DataFrame], e: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Coût de création (injection) et de suppression de chaque lien, en FCFA.

    co_soumission : coût du dossier + coût d'opportunité de la caution sur la durée de
    validité des offres + préparation de l'offre (part du montant). Les valeurs viennent
    des avis ; les hypothèses non observables sont dans config/sources.yaml (section costs).
    Le coût est NaN dès qu'une composante manque (`composantes_manquantes` dit laquelle) :
    une caution inconnue ne compte pas pour zéro.
    """
    c = settings()["costs"]
    avis = t["avis"].assign(lot=t["avis"]["lot"].fillna("unique")) if not t["avis"].empty else pd.DataFrame()
    per_market = pd.DataFrame()
    if not avis.empty:
        days = avis["validite_offres_jours"].astype(float)
        per_market = avis[["reference", "lot", "cout_dossier_fcfa", "caution_fcfa", "cout_previsionnel_fcfa"]].copy()
        per_market["validite_par_defaut"] = days.isna()
        days = days.fillna(c["caution_days_default"])
        per_market["cout_opportunite_caution"] = avis["caution_fcfa"].astype(float) * c["caution_rate_annual"] * days / 365
        per_market["cout_preparation"] = avis["cout_previsionnel_fcfa"].astype(float) * c["bid_preparation_share"]
        comp = per_market[list(COST_COMPONENTS)].astype(float)
        per_market["cout_injection_cosoumission"] = comp.sum(axis=1, skipna=False)
        per_market["composantes_manquantes"] = [
            ";".join(name for name, v in zip(COST_COMPONENTS, row) if pd.isna(v)) or None for row in comp.to_numpy()
        ]
        per_market["cout_injection_part_marche"] = per_market["cout_injection_cosoumission"] / per_market["cout_previsionnel_fcfa"]

    def q(s: pd.Series) -> dict:
        s = s.dropna()
        return {"n": int(len(s)), "p25": s.quantile(0.25) if len(s) else None,
                "mediane": s.median() if len(s) else None, "p75": s.quantile(0.75) if len(s) else None}

    inj = per_market["cout_injection_cosoumission"] if not per_market.empty else pd.Series(dtype=float)
    table = [
        {"relation": "co_soumission", "creer": "dossier + caution immobilisée + offre", "cacher": "ne pas soumissionner",
         "cout_creation": json.dumps(q(inj), default=float), "cout_suppression_fcfa": 0, "mesure": "avis (observé)"},
        {"relation": "meme_groupement", "creer": "former un groupement", "cacher": "soumissionner séparément",
         "cout_creation": "non mesuré", "cout_suppression_fcfa": None, "mesure": "structurel"},
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
        {"relation": "succession_attribution", "creer": "perdre volontairement", "cacher": "—",
         "cout_creation": "signal", "cout_suppression_fcfa": None, "mesure": "signal, pas une action"},
    ]
    summary = pd.DataFrame(table)
    summary["cout_creation"] = summary["cout_creation"].astype(str)
    summary["cout_suppression_fcfa"] = pd.to_numeric(summary["cout_suppression_fcfa"], errors="coerce")
    if not e.empty and not per_market.empty:
        cos = e[e["relation"] == "co_soumission"]
        if not cos.empty:
            keys = cos["evidence"].str.rsplit("|", n=1, expand=True).set_axis(["reference", "lot"], axis=1)
            n_liens = keys.groupby(["reference", "lot"]).size().rename("n_liens").reset_index()
            per_market = per_market.merge(n_liens, on=["reference", "lot"], how="left")
    return summary, per_market
