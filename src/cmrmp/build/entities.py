"""Résolution d'entités : même entreprise, graphies différentes.

1. Normalisation du nom (majuscules, sans accents, formes juridiques retirées de la clé).
2. Fusion certaine sur identifiant (NIU, RCCM).
3. Fusion floue par blocs (premier jeton significatif) avec rapidfuzz, seuil réglable.
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


def is_groupement(name: str) -> bool:
    return bool(GROUPEMENT_RE.search(normalize_name(name) if "/" not in (name or "") else name))


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
    m = m[m["cle"].str.len() > 1]
    uf = _UF()
    for key in m["cle"].unique():
        uf.find(key)
    for col in ("niu", "rccm"):
        if col in m:
            for _, grp in m.dropna(subset=[col]).groupby(col):
                keys = grp["cle"].unique()
                for k in keys[1:]:
                    uf.union(keys[0], k)

    blocks: dict[str, list[str]] = defaultdict(list)
    for key in m["cle"].unique():
        toks = [t for t in key.split() if t not in STOP] or key.split()
        blocks[toks[0][:4]].append(key)
    pairs = []
    for keys in blocks.values():
        if len(keys) > 3000:  # bloc trop gros : on resserre sur deux jetons
            continue
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                score = fuzz.token_sort_ratio(a, b)
                if score >= threshold and _digits(a) == _digits(b):
                    uf.union(a, b)
                    pairs.append({"cle_a": a, "cle_b": b, "score": score})

    roots = {k: uf.find(k) for k in m["cle"].unique()}
    m["entity_id"] = m["cle"].map(lambda k: "E" + _short_hash(roots[k]))
    return m, pd.DataFrame(pairs)


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
