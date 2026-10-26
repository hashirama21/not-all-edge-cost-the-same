"""DGI : fichier des contribuables (NIU, raison sociale, activité, centre, régime).

Deux voies, fusionnées dans un seul fichier `data/dgi/contribuables.parquet` (+ CSV gzip) :

1. Consultation publique `listecontribuable.aspx` (ASP.NET WebForms). Le module lit le
   formulaire (champs cachés __VIEWSTATE, __EVENTVALIDATION…, listes déroulantes, boutons),
   soumet chaque option de liste déroulante ou, à défaut, des préfixes de raison sociale
   raffinés tant que les résultats paraissent tronqués, suit la pagination GridView
   (`__doPostBack(..., 'Page$N')`) et déclenche les boutons d'export (Excel, CSV, PDF).
   Chaque réponse est archivée ; le parcours reprend là où il s'est arrêté.
2. Listes officielles publiées en PDF ou Excel (contribuables inactifs, fichier de la
   dépense publique, liens trouvés sur impots.cm), dont les tableaux sont lus par PyMuPDF.

Le NIU commence par M (personne morale) ou P (personne physique). Les personnes
physiques sont pseudonymisées à la publication (loi n° 2024/017).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import data_dir, settings
from ..extract.documents import html_tables, pdf_text
from ..extract.fields import RE_NIU, strip_accents
from ..http import PoliteClient, RobotsDisallowed
from ..storage import append_jsonl
from .common import absolute_links, document_links

log = logging.getLogger(__name__)
SOURCE = "dgi"

RE_POSTBACK = re.compile(r"__doPostBack\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)")
RE_PAGE = re.compile(r"^Page\$(\d+|Next|Last)$")
RE_COUNT = re.compile(r"(\d[\d\s. ]*)\s*(?:contribuables?|r[ée]sultats?|enregistrements?|lignes?)\s*(?:trouv|retourn)", re.I)

# Colonnes du fichier unifié, et motifs pour reconnaître les en-têtes des sources.
COLUMNS = {
    "niu": r"^n[°o]?\s*(?:contribuable|niu|nui)|^niu$|identifiant|^nui$",
    "raison_sociale": r"raison|d[ée]nomination|nom\b|^nom|contribuable$|nom.*pr[ée]nom",
    "sigle": r"sigle|enseigne|nom\s+commercial",
    "activite": r"activit",
    "centre": r"centre|cri|structure|rattachement|^cdi|^cime",
    "regime": r"r[ée]gime",
    "localisation": r"localisation|ville|adresse|quartier|r[ée]gion",
    "statut": r"statut|[ée]tat|situation",
    "type_contribuable": r"^type",
}


# --------------------------------------------------------------------------- formulaire
@dataclass
class Form:
    action: str
    fields: dict[str, str]
    selects: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    texts: list[str] = field(default_factory=list)
    submits: list[tuple[str, str]] = field(default_factory=list)
    postbacks: list[tuple[str, str, str]] = field(default_factory=list)  # (cible, argument, libellé)

    def payload(self, **overrides: str) -> dict[str, str]:
        data = dict(self.fields)
        data.update(overrides)
        return data


def parse_form(html: str, base_url: str) -> Form | None:
    tree = HTMLParser(html)
    node = tree.css_first("form")
    if node is None:
        return None
    fields_: dict[str, str] = {}
    selects: dict[str, list[tuple[str, str]]] = {}
    texts, submits = [], []
    for inp in node.css("input"):
        name = inp.attributes.get("name")
        if not name:
            continue
        typ = (inp.attributes.get("type") or "text").lower()
        value = inp.attributes.get("value") or ""
        if typ in ("submit", "image", "button"):
            submits.append((name, value))
        elif typ in ("checkbox", "radio"):
            if "checked" in inp.attributes:
                fields_[name] = value or "on"
        else:
            fields_[name] = value
            if typ in ("text", "search"):
                texts.append(name)
    for sel in node.css("select"):
        name = sel.attributes.get("name")
        if not name:
            continue
        opts = [((o.attributes.get("value") or o.text(strip=True)), o.text(strip=True)) for o in sel.css("option")]
        selects[name] = opts
        chosen = next((o for o in sel.css("option") if "selected" in o.attributes), None)
        fields_[name] = (chosen.attributes.get("value") if chosen else (opts[0][0] if opts else "")) or ""
    for ta in node.css("textarea"):
        if ta.attributes.get("name"):
            fields_[ta.attributes["name"]] = ta.text()
    postbacks = []
    for a in tree.css("a[href], input[onclick], a[onclick]"):
        js = (a.attributes.get("href") or "") + " " + (a.attributes.get("onclick") or "")
        for target, arg in RE_POSTBACK.findall(js):
            postbacks.append((target, arg, a.text(strip=True) or a.attributes.get("value") or ""))
    action = urljoin(base_url, node.attributes.get("action") or base_url)
    return Form(action, fields_, selects, texts, submits, postbacks)


def pager_targets(form: Form) -> dict[str, tuple[str, str]]:
    """Liens de pagination GridView : {'2': (cible, 'Page$2'), 'Next': …}."""
    out = {}
    for target, arg, _ in form.postbacks:
        m = RE_PAGE.match(arg)
        if m:
            out[m.group(1)] = (target, arg)
    return out


def _state_free(data: dict[str, str]) -> str:
    """Empreinte d'une requête sans l'état ASP.NET (qui change à chaque réponse)."""
    stable = {k: v for k, v in sorted(data.items()) if not k.startswith("__VIEWSTATE") and k != "__EVENTVALIDATION"}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False).encode()).hexdigest()[:20]


# --------------------------------------------------------------------------- lecture des résultats
def normalize_header(h: str) -> str | None:
    h = strip_accents(str(h or "")).lower().strip()
    for col, rx in COLUMNS.items():
        if re.search(rx, h):
            return col
    return None


def rows_from_records(records: list[dict], provenance: dict) -> list[dict]:
    out = []
    for rec in records:
        row: dict = {}
        for key, value in rec.items():
            col = normalize_header(key)
            value = re.sub(r"\s+", " ", str(value or "")).strip()
            if col and value and col not in row:
                row[col] = value
        if "niu" not in row:  # NIU repéré dans une cellule quelconque
            found = next((RE_NIU.search(str(v)) for v in rec.values() if RE_NIU.search(str(v or ""))), None)
            if found:
                row["niu"] = found.group(1)
        if row.get("niu"):
            row["niu"] = re.sub(r"\s", "", row["niu"]).upper()
            out.append({**row, **provenance})
    return out


def result_rows(html: str, provenance: dict) -> list[dict]:
    rows = []
    for table in html_tables(html):
        rows += rows_from_records(table, provenance)
    return rows


def announced_count(html: str) -> int | None:
    m = RE_COUNT.search(HTMLParser(html).text())
    return int(re.sub(r"\D", "", m.group(1))) if m else None


# --------------------------------------------------------------------------- parcours
class Consultation:
    def __init__(self, http: PoliteClient, url: str, cfg: dict):
        self.http, self.url, self.cfg = http, url, cfg
        self.out = data_dir("interim", "dgi") / "dgi_consultation_rows.jsonl"
        self.exports: list[str] = []

    def _post(self, form: Form, data: dict, tag: str):
        key = f"{form.action}#post={_state_free(data)}"
        return self.http.fetch(form.action, method="POST", data=data, cache_key=key, extra={"query": tag})

    def _paginate(self, cap, tag: str) -> int:
        """Lit la page courante puis suit 'Page$N' jusqu'à la dernière ; renvoie le nombre de lignes."""
        n, page_no, seen_pages = 0, 1, set()
        while cap is not None and page_no <= self.cfg["max_pages_per_query"]:
            html = cap.text()
            rows = result_rows(html, {"source": "consultation", "url": self.url, "requete": tag,
                                      "page": page_no, "fetched_at": cap.fetched_at})
            for r in rows:
                append_jsonl(self.out, r)
            n += len(rows)
            self._exports(cap, html, tag)
            form = parse_form(html, cap.final_url)
            if form is None:
                break
            pages = pager_targets(form)
            nxt = pages.get(str(page_no + 1)) or pages.get("Next")
            if nxt is None:
                # Pager par blocs (« ... ») : le lien suivant vise la page du bloc suivant.
                higher = sorted((int(k), v) for k, v in pages.items() if k.isdigit() and int(k) > page_no)
                nxt = higher[0][1] if higher else None
            if nxt is None or nxt in seen_pages:
                break
            seen_pages.add(nxt)
            data = form.payload(__EVENTTARGET=nxt[0], __EVENTARGUMENT=nxt[1])
            for name, _ in form.submits:
                data.pop(name, None)
            cap = self._post(form, data, f"{tag}|{nxt[1]}")
            page_no += 1
        return n

    def _exports(self, cap, html: str, tag: str) -> None:
        """Déclenche une fois par requête les boutons ou liens d'export (Excel, CSV, PDF)."""
        rx = re.compile(self.cfg["export_regex"], re.I)
        form = parse_form(html, cap.final_url)
        for link in document_links(absolute_links(HTMLParser(html).root, cap.final_url)):
            self._save_export(self.http.fetch(link["url"]), tag)
        if form is None:
            return
        for name, value in form.submits:
            if rx.search(name) or rx.search(value):
                data = form.payload(**{name: value})
                self._save_export(self._post(form, data, f"{tag}|export:{name}"), tag)
        for target, arg, label in form.postbacks:
            if rx.search(target) or rx.search(label):
                data = form.payload(__EVENTTARGET=target, __EVENTARGUMENT=arg)
                self._save_export(self._post(form, data, f"{tag}|export:{target}"), tag)

    def _save_export(self, cap, tag: str) -> None:
        if cap is None or "html" in cap.content_type:
            return
        self.exports.append(cap.path)
        append_jsonl(data_dir("interim", "dgi") / "dgi_exports.jsonl",
                     {"path": cap.path, "url": cap.final_url, "content_type": cap.content_type, "requete": tag})

    def run(self) -> int:
        cap = self.http.fetch(self.url)
        if cap is None:
            log.warning("consultation inaccessible : %s", self.url)
            return 0
        form = parse_form(cap.text(), cap.final_url)
        if form is None:
            return 0
        rx_submit = re.compile(self.cfg["submit_regex"], re.I)
        submit = next(((n, v) for n, v in form.submits if rx_submit.search(n) or rx_submit.search(v)),
                      form.submits[0] if form.submits else None)
        total = 0
        # 1) Listes déroulantes (centre, régime, activité…) : une requête par option non vide.
        for name, options in form.selects.items():
            for value, label in options:
                if not value or value in ("0", "-1"):
                    continue
                data = form.payload(**{name: value}, **({submit[0]: submit[1]} if submit else {}))
                total += self._paginate(self._post(form, data, f"{name}={label}"), f"{name}={label}")
        # 2) Préfixes de raison sociale, raffinés si le résultat semble tronqué.
        rx_search = re.compile(self.cfg["search_field_regex"], re.I)
        field_name = next((t for t in form.texts if rx_search.search(t)), form.texts[0] if form.texts else None)
        if field_name:
            queue = list(self.cfg["alphabet"])
            while queue:
                prefix = queue.pop(0)
                data = form.payload(**{field_name: prefix}, **({submit[0]: submit[1]} if submit else {}))
                first = self._post(form, data, f"{field_name}={prefix}")
                n = self._paginate(first, f"{field_name}={prefix}")
                total += n
                count = announced_count(first.text()) if first else None
                truncated = (count or n) >= self.cfg["result_cap_hint"] and (count is None or n < count)
                if truncated and len(prefix) < self.cfg["max_prefix_len"]:
                    queue.extend(prefix + c for c in self.cfg["alphabet"])
        return total


def crawl_consultation(urls: list[str] | None = None, **client_kw) -> int:
    cfg = settings()["dgi"]
    total = 0
    with PoliteClient(SOURCE, **client_kw) as http:
        for url in urls or cfg["consultation_urls"]:
            try:
                total += Consultation(http, url, cfg).run()
            except RobotsDisallowed:
                log.warning("robots.txt interdit %s", url)
    return total


# --------------------------------------------------------------------------- fichiers officiels
def rows_from_file(path: Path, provenance: dict) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        _, meta = pdf_text(path, ocr=True)
        rows = []
        for t in meta.get("tables") or []:
            records = [dict(zip(t["header"], r)) for r in t["rows"] if len(r) == len(t["header"])]
            rows += rows_from_records(records, {**provenance, "page": t["page"]})
        return rows
    if suffix in (".xls", ".xlsx", ".csv"):
        if suffix == ".csv":
            frames = {"csv": pd.read_csv(path, dtype=str, sep=None, engine="python", encoding_errors="replace")}
        else:
            frames = pd.read_excel(path, dtype=str, sheet_name=None)
        rows = []
        for sheet, df in frames.items():
            rows += rows_from_records(df.fillna("").to_dict("records"), {**provenance, "feuille": sheet})
        return rows
    if suffix in (".html", ".htm"):
        return result_rows(path.read_text(encoding="utf-8", errors="replace"), provenance)
    return []


def crawl_official(**client_kw) -> int:
    cfg = settings()["dgi"]
    out = data_dir("interim", "dgi") / "dgi_official_rows.jsonl"
    files = list(cfg["official_files"])
    n = 0
    with PoliteClient(SOURCE, **client_kw) as http:
        for page in cfg.get("official_pages", []):  # nouvelles listes publiées sur impots.cm
            cap = http.fetch(page, refresh=True)
            if cap is None:
                continue
            for link in document_links(absolute_links(HTMLParser(cap.text()).root, cap.final_url)):
                if re.search(r"contribuable|inactif|actif|fichier|radi", link["url"] + link["text"], re.I):
                    if all(f["url"] != link["url"] for f in files):
                        files.append({"url": link["url"], "statut": _statut_from(link["text"] + link["url"]), "date_reference": None})
        for f in files:
            cap = http.fetch(f["url"])
            if cap is None:
                continue
            rows = rows_from_file(Path(cap.path), {"source": "fichier_officiel", "url": f["url"], "statut_liste": f["statut"],
                                                   "date_reference": f.get("date_reference"), "fetched_at": cap.fetched_at})
            for r in rows:
                append_jsonl(out, r)
            n += len(rows)
            log.info("%s : %s lignes", f["url"], len(rows))
    return n


def _statut_from(text: str) -> str:
    t = strip_accents(text).lower()
    return "inactif" if "inactif" in t else "radie" if "radi" in t else "actif" if "actif" in t else "inconnu"


# --------------------------------------------------------------------------- fichier unifié
def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def unify() -> pd.DataFrame:
    """Un NIU = une ligne. Valeurs prises en priorité dans la consultation (plus récente),
    puis dans les fichiers officiels ; toutes les provenances sont gardées."""
    base = data_dir("interim", "dgi")
    rows = _read_jsonl(base / "dgi_consultation_rows.jsonl") + _read_jsonl(base / "dgi_official_rows.jsonl")
    for exp in _read_jsonl(base / "dgi_exports.jsonl"):
        rows += rows_from_file(Path(exp["path"]), {"source": "export_consultation", "url": exp["url"], "requete": exp["requete"]})
    cols = list(COLUMNS)
    if not rows:
        return pd.DataFrame(columns=["niu", *cols[1:], "type_personne", "statut_listes", "sources", "n_sources"])
    df = pd.DataFrame(rows)
    for c in cols + ["statut_liste", "fetched_at"]:
        if c not in df:
            df[c] = None
    df = df[df["niu"].str.fullmatch(r"[MP]\d{12}[A-Z]", na=False)]
    df["_prio"] = df["source"].map({"consultation": 0, "export_consultation": 1}).fillna(2)
    df = df.sort_values(["niu", "_prio", "fetched_at"], ascending=[True, True, False])
    first = lambda s: next((v for v in s if isinstance(v, str) and v), None)
    agg = {c: first for c in cols if c != "niu"}
    agg.update({"statut_liste": lambda s: sorted({v for v in s if isinstance(v, str)}),
                "url": lambda s: sorted(set(s.dropna()))[:20], "source": lambda s: sorted(set(s)),
                "fetched_at": first})
    out = df.groupby("niu", as_index=False).agg(agg)
    out = out.rename(columns={"statut_liste": "statut_listes", "url": "sources", "source": "voies", "fetched_at": "capture_la_plus_recente"})
    out["type_personne"] = out["niu"].str[0].map({"M": "morale", "P": "physique"})
    out["n_sources"] = out["sources"].map(len)
    return out


def pseudonymize_natural_persons(df: pd.DataFrame, salt: str) -> pd.DataFrame:
    """Pour les NIU en P (personnes physiques) : nom, sigle et localisation hachés, NIU haché."""
    df = df.copy()
    mask = df["type_personne"] == "physique"
    h = lambda v: None if not isinstance(v, str) else "h_" + hashlib.sha256(f"{salt}|{v}".encode()).hexdigest()[:16]
    for col in ("niu", "raison_sociale", "sigle", "localisation"):
        df.loc[mask, col] = df.loc[mask, col].map(h)
    return df


def write_unified(df: pd.DataFrame, salt: str | None, keep_natural_persons: bool | None = None) -> dict[str, Path]:
    keep = settings()["dgi"]["keep_natural_persons"] if keep_natural_persons is None else keep_natural_persons
    out_dir = data_dir("dgi")
    if not keep:
        if not salt:
            raise RuntimeError("CMRMP_SALT manquant : nécessaire pour pseudonymiser les personnes physiques.")
        df = pseudonymize_natural_persons(df, salt)
    paths = {"parquet": out_dir / "contribuables.parquet", "csv": out_dir / "contribuables.csv.gz"}
    df.to_parquet(paths["parquet"], index=False)
    flat = df.copy()
    for col in ("statut_listes", "sources", "voies"):
        if col in flat:
            flat[col] = flat[col].map(lambda v: ";".join(v) if isinstance(v, (list, tuple)) else v)
    flat.to_csv(paths["csv"], index=False, compression="gzip")
    summary = {"n_niu": int(len(df)), "par_type": df["type_personne"].value_counts().to_dict() if len(df) else {},
               "pseudonymise": not keep}
    (out_dir / "contribuables.meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def publish_bucket(bucket_id: str, private: bool = True) -> None:
    """Envoie data/dgi (fichier unifié) et le brut DGI dans le bucket Hugging Face."""
    from ..publish.hf import sync_raw

    sync_raw(bucket_id, direction="up", private=private, paths=["dgi", "raw/dgi", "interim/dgi"])
