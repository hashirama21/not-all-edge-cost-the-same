"""Extraction déterministe (regex) des champs des documents de marché camerounais.

Ces règles servent de première passe et de contrôle croisé de l'extraction par LLM :
une valeur est jugée fiable quand les deux concordent.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

MOIS = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "décembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

# Codes de procédure rencontrés dans les références (N°025/AONO/CAYI/2026).
PROCEDURES = {
    "AONO": "appel d'offres national ouvert",
    "AONR": "appel d'offres national restreint",
    "AOIO": "appel d'offres international ouvert",
    "AOIR": "appel d'offres international restreint",
    "AONOU": "appel d'offres national ouvert en urgence",
    "DC": "demande de cotation",
    "DCU": "demande de cotation en urgence",
    "AMI": "appel à manifestation d'intérêt",
    "DP": "demande de propositions",
    "AOO": "appel d'offres ouvert",
    "AOR": "appel d'offres restreint",
    "GG": "gré à gré",
}

_SPACE = r"[\s  ]"
_AMOUNT = rf"(\d{{1,3}}(?:(?:{_SPACE}|\.|,)?\d{{3}})+|\d+)"
_CURRENCY = r"(?:F\.?\s?CFA|FCFA|XAF|francs?\s+CFA|F\s?CFA|FRS?\s?CFA)"

RE_REFERENCE = re.compile(
    r"N[°ºo]\.?\s*(\d{1,5}[A-Z]?(?:\s*/\s*[A-Z0-9][A-Z0-9_\-\.\s]*?){2,8}/\s*(?:19|20)\d{2})",
    re.IGNORECASE,
)
RE_NIU = re.compile(r"\b([MP]\s?\d{12}\s?[A-Z])\b")
RE_RCCM = re.compile(r"\b(RC\s*/\s*[A-Z]{3,4}\s*/\s*(?:19|20)\d{2}\s*/\s*[A-Z]\s*/\s*\d{1,6})\b", re.IGNORECASE)
RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
RE_PHONE = re.compile(r"(?<!\d)(?:\(?\+?237\)?[\s.\-]?)?([26]\d{2}(?:[\s.\-]?\d{2}){3})(?!\d)")
RE_DATE_FR = re.compile(
    r"\b(\d{1,2})(?:er)?\s+(janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|"
    r"octobre|novembre|d[ée]cembre|january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+((?:19|20)\d{2})",
    re.IGNORECASE,
)
RE_DATE_NUM = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-]((?:19|20)\d{2})\b")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def parse_fcfa(raw: str) -> int | None:
    """'3 100 500' -> 3100500 ; '22.800.000' -> 22800000 ; '1,5 million' -> 1500000."""
    if raw is None:
        return None
    s = raw.strip().lower()
    mult = 1
    if re.search(r"milliards?", s):
        mult = 1_000_000_000
    elif re.search(r"millions?", s):
        mult = 1_000_000
    s = re.sub(r"(milliards?|millions?).*", "", s).strip()
    if mult > 1 and re.fullmatch(r"\d+[,.]\d+", s):
        return int(round(float(s.replace(",", ".")) * mult))
    digits = re.sub(r"\D", "", s)
    return int(digits) * mult if digits else None


def amounts(text: str) -> list[tuple[int, int, int]]:
    """Tous les montants en FCFA : (valeur, début, fin)."""
    out = []
    for m in re.finditer(rf"{_AMOUNT}{_SPACE}*(?:\)?{_SPACE}*)?{_CURRENCY}", text, re.IGNORECASE):
        val = parse_fcfa(m.group(1))
        if val:
            out.append((val, m.start(), m.end()))
    return out


def _amount_near(text: str, keywords: str, window: int = 260) -> int | None:
    """Premier montant FCFA situé après un mot-clé, dans une fenêtre de `window` caractères."""
    flat = re.sub(r"\s+", " ", text)
    for km in re.finditer(keywords, flat, re.IGNORECASE):
        chunk = flat[km.end(): km.end() + window]
        found = amounts(chunk)
        if found:
            return found[0][0]
    return None


def cout_dossier(text: str) -> int | None:
    return _amount_near(
        text,
        r"(?:acquisition|achat|retrait|co[uû]t|frais)\s+(?:du\s+)?(?:dossier|DAO)|"
        r"dossier[^.]{0,80}?(?:contre|moyennant|paiement|versement)|non\s+remboursable",
    )


def caution_soumission(text: str) -> int | None:
    return _amount_near(text, r"caution(?:nement)?\s+(?:de\s+|provisoire\s+de\s+)?soumission|caution\s+provisoire|garantie\s+de\s+soumission|bid\s+bond")


def capacite_financiere(text: str) -> int | None:
    return _amount_near(text, r"capacit[ée]\s+financi[eè]re|attestation\s+de\s+capacit[ée]|ligne\s+de\s+cr[ée]dit|financial\s+capacity")


def cout_previsionnel(text: str) -> int | None:
    return _amount_near(text, r"co[uû]t\s+pr[ée]visionnel|montant\s+pr[ée]visionnel|estimation\s+(?:du\s+co[uû]t|pr[ée]visionnelle)|budget\s+pr[ée]visionnel|enveloppe|estimated\s+cost")


def parse_date_fr(text: str) -> date | None:
    m = RE_DATE_FR.search(text)
    if m:
        try:
            return date(int(m.group(3)), MOIS[strip_accents(m.group(2).lower())], int(m.group(1)))
        except (KeyError, ValueError):
            pass
    m = RE_DATE_NUM.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def date_near(text: str, keywords: str, window: int = 200) -> date | None:
    flat = re.sub(r"\s+", " ", text)
    for km in re.finditer(keywords, flat, re.IGNORECASE):
        d = parse_date_fr(flat[km.end(): km.end() + window])
        if d:
            return d
    return None


def date_depot(text: str) -> date | None:
    return date_near(text, r"(?:date\s+limite|d[ée]p[oô]t)\s+des\s+offres|d[ée]pos[ée]es\s+au\s+plus\s+tard|deadline")


def date_ouverture(text: str) -> date | None:
    return date_near(text, r"ouverture\s+des\s+plis|ouverture\s+des\s+offres|bid\s+opening")


def reference(text: str) -> str | None:
    m = RE_REFERENCE.search(text)
    return normalize_reference(m.group(1)) if m else None


def normalize_reference(ref: str) -> str:
    ref = strip_accents(ref).upper()
    ref = re.sub(r"\s*/\s*", "/", ref)
    return re.sub(r"\s+", "", ref)


def procedure_from_reference(ref: str | None) -> str | None:
    if not ref:
        return None
    tokens = ref.upper().split("/")
    for tok in tokens:
        if tok in PROCEDURES:
            return tok
    for tok in tokens:  # ex. « AONO-U »
        clean = re.sub(r"[^A-Z]", "", tok)
        if clean in PROCEDURES:
            return clean
    return None


def niu_list(text: str) -> list[str]:
    return sorted({re.sub(r"\s", "", m) for m in RE_NIU.findall(text)})


def rccm_list(text: str) -> list[str]:
    return sorted({re.sub(r"\s", "", m).upper() for m in RE_RCCM.findall(text)})


def phone_list(text: str) -> list[str]:
    return sorted({re.sub(r"\D", "", m) for m in RE_PHONE.findall(text)})


def email_list(text: str) -> list[str]:
    return sorted({m.lower() for m in RE_EMAIL.findall(text)})


DOC_TYPES = [
    ("additif", r"\badd?itif\b|\brectificatif\b|\bcorrigendum\b|\baddendum\b"),
    ("annulation", r"\bannulation\b|\bannul[ée]\b|\bcancell?ation\b"),
    ("infructueux", r"\binfructueu"),
    ("decision_attribution", r"d[ée]cision\s+(?:d['’]\s*)?attribution|attributaire|award\s+decision|communiqu[ée]\s+(?:de\s+)?r[ée]sultats?"),
    ("pv_ouverture", r"proc[eè]s[\s\-]verbal\s+(?:d['’]\s*)?ouverture|\bpv\b.{0,30}ouverture|bid\s+opening\s+report"),
    ("pv_jugement", r"proc[eè]s[\s\-]verbal\s+(?:de\s+)?(?:jugement|[ée]valuation|analyse)|rapport\s+d['’]analyse"),
    ("dao", r"dossier\s+d['’]\s*appel\s+d['’]\s*offres|\bDAO\b|tender\s+file"),
    ("avis", r"avis\s+d['’]\s*appel|appel\s+d['’]\s*offres|demande\s+de\s+cotation|manifestation\s+d['’]int[ée]r[eê]t|invitation\s+to\s+tender"),
    ("sanction", r"exclu(?:sion|es?)\s+(?:de\s+la\s+)?commande\s+publique|suspen(?:sion|dues?)|mise\s+en\s+demeure|r[ée]siliation"),
]


def classify(title: str, text: str = "") -> str:
    head = f"{title}\n{text[:3000]}"
    for label, pattern in DOC_TYPES:
        if re.search(pattern, head, re.IGNORECASE):
            return label
    return "autre"


def label_values(text: str) -> dict[str, str]:
    """Paires « LIBELLÉ : valeur » (fiches ARMP : ATTRIBUTAIRE : X, MONTANT : Y, DELAI : Z)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"(?m)^\s*([A-ZÉÈÀÂÎÔÛÇ][A-ZÉÈÀÂÎÔÛÇ '’/()\-]{2,60}?)\s*:\s*(.+?)\s*$", text):
        key = strip_accents(m.group(1)).strip().lower().replace(" ", "_")
        out.setdefault(key, m.group(2).strip())
    return out


_COL_NAME = re.compile(r"soumissionnaire|entreprise|raison\s+sociale|candidat|bidder|nom", re.I)
_COL_AMOUNT = re.compile(r"montant|offre\s+financi|prix|ttc|htva|amount", re.I)
_COL_RANK = re.compile(r"rang|classement|rank", re.I)
_COL_STATUS = re.compile(r"observations?|recevab|conform|d[ée]cision|statut", re.I)


def bids_from_tables(tables: list[dict]) -> list[dict]:
    """Soumissions lues dans les tableaux PyMuPDF d'un PV (sans LLM).

    Une table est retenue si l'en-tête a une colonne de nom et une colonne de montant.
    """
    out = []
    for t in tables:
        header = [str(h or "") for h in t.get("header") or []]
        rows = t.get("rows") or []
        if not header or not any(_COL_NAME.search(h) for h in header):
            header, rows = (rows[0], rows[1:]) if rows else ([], [])
        idx = lambda rx: next((i for i, h in enumerate(header) if rx.search(str(h or ""))), None)
        i_name, i_amount, i_rank, i_status = idx(_COL_NAME), idx(_COL_AMOUNT), idx(_COL_RANK), idx(_COL_STATUS)
        if i_name is None or i_amount is None:
            continue
        for r in rows:
            if len(r) <= max(i_name, i_amount) or not r[i_name] or r[i_name] == header[i_name]:
                continue
            status = r[i_status] if i_status is not None and i_status < len(r) else ""
            rank = re.sub(r"\D", "", r[i_rank]) if i_rank is not None and i_rank < len(r) else ""
            out.append({
                "page": t.get("page"), "soumissionnaire": re.sub(r"\s+", " ", r[i_name]).strip(),
                "montant_offre_fcfa": parse_fcfa(r[i_amount]), "rang": int(rank) if rank else None,
                "observation": status or None,
                "recevable": None if not status else not re.search(r"non\s+(?:recevable|conforme)|rejet|[ée]limin", status, re.I),
            })
    return out


def avis_fields(text: str) -> dict:
    ref = reference(text)
    return {
        "reference": ref,
        "procedure": procedure_from_reference(ref),
        "cout_dossier_fcfa": cout_dossier(text),
        "caution_fcfa": caution_soumission(text),
        "capacite_financiere_fcfa": capacite_financiere(text),
        "cout_previsionnel_fcfa": cout_previsionnel(text),
        "date_depot": date_depot(text),
        "date_ouverture": date_ouverture(text),
        "date_document": parse_date_fr(text[-1500:]) or parse_date_fr(text),
    }


def today_iso() -> str:
    return datetime.now().date().isoformat()
