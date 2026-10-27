"""Résolution d'entités : même entreprise, graphies différentes.

1. Normalisation du nom (majuscules, sans accents, formes juridiques retirées de la clé).
2. Fusion certaine sur identifiant (NIU, RCCM normalisés), sauf si un même identifiant
   couvre plus de MAX_ID_GROUP noms différents (identifiant mal attribué, ex. celui du
   maître d'ouvrage recopié sur chaque ligne) : ce groupe n'est pas fusionné.
3. Fusion floue par blocs (premier jeton significatif, resserré sur deux jetons pour un
   bloc trop gros) avec rapidfuzz, seuil réglable.

Identifiant d'entité stable d'une version à l'autre : hachage du plus petit NIU de la
composante s'il y en a un, sinon de sa clé la plus fréquente (égalité : ordre alphabétique).

Un échantillon de paires fusionnées est exporté pour annotation manuelle : le taux
d'erreur rapporté dans l'article se calcule sur cet échantillon.
"""

from __future__ import annotations

import re
from collections import defaultdict

import pandas as pd
from rapidfuzz import fuzz

from ..extract.fields import strip_accents

LEGAL_FORMS = [
    "SOCIETE A RESPONSABILITE LIMITEE UNIPERSONNELLE", "SOCIETE A RESPONSABILITE LIMITEE",
    "SOCIETE ANONYME", "SOCIETE PAR ACTIONS SIMPLIFIEE", "GROUPEMENT D INTERET ECONOMIQUE",
    "ETABLISSEMENTS", "ETABLISSEMENT", "SARLU", "SARL", "SASU", "SAS", "SA", "GIE", "ETS", "STE",
    "SOCIETE", "ENTREPRISE", "ENTREPRISES", "CIE", "COMPAGNIE", "SCS", "SNC", "SCI", "LTD", "PLC", "INC",
]
_FORM_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, LEGAL_FORMS), key=len, reverse=True)) + r")\b")
GROUPEMENT_RE = re.compile(r"\b(GROUPEMENT|GPT|CONSORTIUM|JV|JOINT VENTURE)\b|/|\s\+\s")
STOP = {"ET", "DE", "DU", "DES", "LA", "LE", "LES", "L", "D", "AND", "THE", "OF", "CAMEROUN", "CAMEROON", "GROUP", "GROUPE"}


def normalize_name(name: str) -> str:
    s = strip_accents(name or "").upper()
    s = s.replace("&", " ET ").replace("’", " ").replace("'", " ")
    s = re.sub(r"\bS\.?\s?A\.?\s?R\.?\s?L\.?\b", "SARL", s)
    s = re.sub(r"\bS\.\s?A\.?\b", "SA", s)
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_key(name: str) -> str:
    """Clé de comparaison : nom normalisé sans forme juridique."""
    s = _FORM_RE.sub(" ", normalize_name(name))
    return re.sub(r"\s+", " ", s).strip()


MAX_ID_GROUP = 5
MAX_BLOCK = 3000


def is_groupement(name: str) -> bool:
    # Sur le nom brut en majuscules : normalize_name retire « / » et « + ».
    return bool(GROUPEMENT_RE.search(strip_accents(name or "").upper()))


def normalize_identifier(value) -> str | None:
    """NIU / RCCM comparables : sans espaces ni ponctuation superflue, en majuscules."""
    if not isinstance(value, str):
        return None
    s = re.sub(r"[\s.\-]", "", strip_accents(value).upper())
    return s or None


def split_groupement(name: str) -> list[str]:
    parts = re.split(r"\s*(?:/|\+|\bET\b(?=\s+[A-Z]{2,}\s+(?:SARL|SA)\b))\s*", re.sub(r"(?i)^\s*(groupement|gpt)\s*", "", name))
    return [p.strip() for p in parts if len(p.strip()) > 2]


class _UF:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def resolve(mentions: pd.DataFrame, threshold: int = 92) -> tuple[pd.DataFrame, pd.DataFrame]:
    """mentions : colonnes nom_brut, et optionnellement niu, rccm.

    Renvoie (mentions + entity_id, paires fusionnées par similarité avec leur score).
    """
    m = mentions.copy()
    m["nom_normalise"] = m["nom_brut"].map(normalize_name)
    m["cle"] = m["nom_brut"].map(name_key)
    m = m[m["cle"].str.len() > 1].copy()
    uf = _UF()
    for key in m["cle"].unique():
        uf.find(key)
    suspect_niu: set[str] = set()
    for col in ("niu", "rccm"):
        if col in m:
            m[col] = m[col].map(normalize_identifier)
            for ident, grp in m.dropna(subset=[col]).groupby(col):
                keys = grp["cle"].unique()
                if len(keys) > MAX_ID_GROUP:
                    if col == "niu":
                        suspect_niu.add(ident)
                    continue  # identifiant partagé par trop de noms : suspect, pas de fusion
                for k in keys[1:]:
                    uf.union(keys[0], k)

    pairs: list[dict] = []
    _fuzzy_blocks(list(m["cle"].unique()), uf, pairs, threshold, depth=1)

    roots = {k: uf.find(k) for k in m["cle"].unique()}
    m["_root"] = m["cle"].map(roots)
    canon: dict[str, str] = {}
    for root, grp in m.groupby("_root"):
        nius = sorted(set(grp["niu"].dropna()) - suspect_niu) if "niu" in grp else []
        if nius:
            canon[root] = "NIU:" + nius[0]
        else:
            counts = grp["cle"].value_counts()
            canon[root] = sorted(counts[counts == counts.max()].index)[0]
    m["entity_id"] = m["_root"].map(lambda r: "E" + _short_hash(canon[r]))
    return m.drop(columns="_root"), pd.DataFrame(pairs, columns=["cle_a", "cle_b", "score"])


def _block_key(key: str, depth: int) -> str:
    toks = [t for t in key.split() if t not in STOP] or key.split()
    return "|".join(t[:4] for t in toks[:depth])


def _fuzzy_blocks(keys: list[str], uf: _UF, pairs: list[dict], threshold: int, depth: int) -> None:
    blocks: dict[str, list[str]] = defaultdict(list)
    for key in keys:
        blocks[_block_key(key, depth)].append(key)
    for bkey, members in blocks.items():
        if len(members) > MAX_BLOCK:
            if depth < 3:  # bloc trop gros : on resserre sur un jeton de plus
                _fuzzy_blocks(members, uf, pairs, threshold, depth + 1)
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                score = fuzz.token_sort_ratio(a, b)
                if score >= threshold and _digits(a) == _digits(b):
                    uf.union(a, b)
                    pairs.append({"cle_a": a, "cle_b": b, "score": score})


def _digits(s: str) -> str:
    return "".join(re.findall(r"\d", s))


def _short_hash(s: str) -> str:
    import hashlib

    return hashlib.sha1(s.encode()).hexdigest()[:12]


def review_sample(pairs: pd.DataFrame, n: int = 200, seed: int = 0) -> pd.DataFrame:
    if pairs.empty:
        return pairs
    s = pairs.sample(min(n, len(pairs)), random_state=seed).copy()
    s["meme_entreprise"] = ""  # à remplir à la main : oui / non
    return s
