"""ARMP : moteur /recherche_avancee, /sanctions, /experts, fiches /details et PRIDESOFT.

- /recherche_avancee?recherche_avancee_do=1&type_publication[]=AO&seuil_marches=1&page=N :
  le moteur paginé (10 avis/page, ~146 610 publications, dernier millions communiqué en
  2026). Voir crawl_publications pour les filtres GET.
- /sanctions : table statique #sanctions (exclusions/décisions de l'ARMP).
- /experts et /experts_search : table #experts (âpporteurs, domaines, lieux).
- armp.cm/details?type_publication=DEC-ATTR&id_publication=N : fiche HTML (attributaire,
  montant, délai…). Les identifiants sont séquentiels : on les énumère et on s'arrête
  après une longue série d'identifiants absents (repli crawl_details).
- pridesoft.armp.cm/0903_publications_report_appel_offres/index.php?type_avis=AO&id=N :
  rapport d'avis (HTML si affichehtml=1, sinon document).
- Les documents sources sont sous pridesoft.armp.cm/_lib/file/doc/0903/AAAA/MM/JJ/ENTITE/
  et sont servis par /_dl? Télécharger la pièce d'origine + /0903_dao_dl (page iframe).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import settings
from ..extract import fields
from ..extract.documents import html_tables, html_text
from ..http import PoliteClient
from ..storage import append_jsonl_unique, relative_path
from .common import absolute_links, document_links, interim, queue_documents

log = logging.getLogger(__name__)

EMPTY_MARKERS = re.compile(r"aucune\s+publication|introuvable|not\s+found|n'existe\s+pas", re.IGNORECASE)


def _is_empty(text: str) -> bool:
    return len(text) < 200 or bool(EMPTY_MARKERS.search(text[:2000]))


# Pages ARMP (constat�es le 2026-11-09) :
# - /recherche_avancee?recherche_avancee_do=1&type_publication[]=AO&seuil_marches=1&page=N
#   liste pagin�e (10 avis/page) ; filtres : type_publication[], reference_avis,
#   maitre_ouvrage, region, departement, seuil_marches, rechercher_parmi.
# - /filtres_avancee?filtre=avis&valeur_filtre=0&exercice=2025&type_procedure=5
#   variante tableau de bord filtrable (type_procedure, nature_prestation,
#   type_maitre_ouvrage, region, etat_du_projet, seuil, periode, exercice).
# - /sanctions : tables statiques #sanctions et #recours_home.
# - /experts et /experts_search?region=&ville=&domaines[]=&diplomes_input= :
#   table #experts (5 colonnes d'identit� + 5 domaines + 11 lieux, fa-check/fa-times).
SEARCH_ITEM_SELECTOR = "ul#tout_les_avis > li.list-group-item"
EXPERT_DOMAIN_CODES = ["AG", "SPI", "BEC", "TR", "AI",
                       "S/Cent.", "OU", "LT", "NO", "NW", "SW", "SU", "EN", "AD", "CE", "ES"]


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text(strip=True)) if node is not None else ""


def _iso_date(value: str | None) -> str | None:
    value = (value or "").strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value[:10], fmt)
        except ValueError:
            continue
        return None if dt.year == 1970 else dt.date().isoformat()
    return None


def _iso_datetime(value: str | None) -> str | None:
    value = (value or "").strip()
    try:
        dt = datetime.strptime(value, "%d-%m-%Y %H:%M:%S")
    except ValueError:
        return _iso_date(value)
    return None if dt.year == 1970 else dt.strftime("%Y-%m-%d %H:%M:%S")


def _last_page(html: str) -> int:
    tree = HTMLParser(html)
    pages = [1]
    for a in tree.css(".pagination a[href]"):
        q = parse_qs(urlsplit(a.attributes.get("href") or "").query)
        for value in q.get("page", []):
            if value.isdigit():
                pages.append(int(value))
    return max(pages)


def _listing_meta(item) -> dict[str, str]:
    meta: dict[str, str] = {}
    for row in item.css("div.d-table-row"):
        cells = row.css("div.d-table-cell")
        if len(cells) < 2:
            continue
        key = fields.label_key(_text(cells[0]))
        value = _text(cells[1])
        if key and value:
            meta.setdefault(key, value)
    return meta


def parse_listing(html: str, base_url: str = "https://www.armp.cm/recherche_avancee") -> list[dict]:
    """D�coupe /recherche_avancee en annonces (li.list-group-item)."""
    tree = HTMLParser(html)
    out = []
    for item in tree.css(SEARCH_ITEM_SELECTOR):
        detail = None
        for a in item.css("a[href]"):
            href = (a.attributes.get("href") or "").strip()
            if urlsplit(href).path.rstrip("/") == "/details":  # pas le lien de partage Facebook
                detail = a
                break
        if detail is None:
            continue
        url = urljoin(base_url, detail.attributes.get("href") or "")
        q = parse_qs(urlsplit(url).query)
        type_publication = (q.get("type_publication") or [None])[0]
        id_publication = (q.get("id_publication") or [None])[0]
        title_node = item.css_first("strong[title]") or item.css_first("div.text-left")
        title = _text(title_node)
        if title_node is not None:
            title = re.sub(r"\s+", " ", title_node.attributes.get("title") or title).strip()
        meta = _listing_meta(item)
        links = absolute_links(item, base_url)
        docs = document_links(links)
        seen: set[str] = set()
        docs = [d for d in docs if not (d["url"] in seen or seen.add(d["url"]))]
        type_label = meta.get("type")
        published = _iso_datetime(meta.get("publie_le"))
        out.append({
            "source": "armp", "url": url, "detail_url": url,
            "type_publication": type_publication, "id_publication": id_publication,
            "title": title, "reference": fields.reference(title),
            "doc_type": fields.classify(f"{type_label or ''} {title}", type_label or ""),
            "maitre_ouvrage": meta.get("mo_ac"), "type_procedure": type_label,
            "region": meta.get("region"), "amount_fcfa": fields.parse_fcfa(meta.get("montant", "")),
            "date_publication": published, "date_cloture": _iso_date(meta.get("date_de_cloture")),
            "heure_cloture": meta.get("heure_de_cloture"), "text": title,
            "documents": docs,
        })
    return out


def parse_sanctions(html: str, url: str = "https://www.armp.cm/sanctions") -> list[dict]:
    tree = HTMLParser(html)
    table = tree.css_first("table#sanctions")
    if table is None:
        return []
    rows = table.css("tr")
    out = []
    for tr in rows[1:]:
        cells = [_text(c) for c in tr.css("td")]
        if len(cells) < 6:
            continue
        out.append({
            "source": "armp", "url": url, "prestataire": cells[0],
            "reference_objet": cells[1], "acte": cells[2],
            "date_debut": _iso_date(cells[3]), "date_fin": _iso_date(cells[4]),
            "motif": cells[5],
        })
    return out


def parse_experts(html: str, url: str = "https://www.armp.cm/experts") -> list[dict]:
    tree = HTMLParser(html)
    table = tree.css_first("table#experts")
    if table is None:
        return []
    rows = table.css("tr")
    if len(rows) < 3:
        return []
    header = [_text(c) for c in rows[1].css("th, td")]
    codes = header if len(header) == len(EXPERT_DOMAIN_CODES) else EXPERT_DOMAIN_CODES
    out = []
    for tr in rows[2:]:
        cells = tr.css("td")
        if len(cells) < 5:
            continue
        values = [_text(c) for c in cells[:5]]
        flags = cells[5:5 + len(codes)]
        checked = [codes[i] for i, c in enumerate(flags) if c.css_first("i.fa-check") is not None]
        ville, _, region = values[2].partition("/")
        out.append({
            "source": "armp", "url": url, "nom": values[0], "instance": values[1],
            "ville": ville.strip() or None, "region": region.strip() or None,
            "qualification": values[3], "contacts": values[4],
            "domaines": [d for d in checked if d in EXPERT_DOMAIN_CODES[:5]],
            "lieux": [d for d in checked if d in EXPERT_DOMAIN_CODES[5:]],
        })
    return out


def discover_types() -> dict[str, int]:
    """Compte les types d'avis du tableau de bord ARMP (/filtres, exercice courant)."""
    cfg = settings()["armp"]
    url = cfg.get("discover_url") or f"{cfg['base_url'].rstrip('/')}/filtres?type=avis&val=0&exercice=2025"
    counts: dict[str, int] = {}
    with PoliteClient("armp") as http:
        cap = http.fetch(url, refresh=True)
        if cap is None:
            return counts
        for a in HTMLParser(cap.text()).css("a[href]"):
            href = a.attributes.get("href") or ""
            if "type=avis" not in href:
                continue
            value = (parse_qs(urlsplit(href).query).get("val") or [None])[0]
            if value is None:
                continue
            m = re.search(r"(\d[\d\s]*)$", _text(a))
            counts[value] = int(re.sub(r"\s", "", m.group(1))) if m else counts.get(value, 0) + 1
    return counts


def crawl_publications(types: list[str] | None = None, max_pages: int | None = None,
                        region: str | int | None = None, departement: str | int | None = None,
                        maitre_ouvrage: str | int | None = None, seuil: str | int | None = None,
                        reference: str | None = None, chercher_parmi: str | int | None = None,
                        exercices: list[int] | None = None, codes: list[int | str] | None = None,
                        recent: bool = True, details: bool = False) -> int:
    """Collecte ARMP : rattrapage « trois derniers mois » + couverture 2016→2026.

    - /recherche_avancee : moteur paginé (10 avis/page), 10 dernières années visibles
      mais pages de résultat par défaut limitées aux trois derniers mois.
    - /filtres_avancee?filtre=avis&valeur_filtre=0&exercice=Y&type_procedure=C :
      la seule interface qui couvre les années REELLES ; elle exige au moins un
      filtre actif (sinon 0 publication). Chaque (exercice, type_procedure) est
      paginé ; type_procedure = 1 AON, 5 DC, 12 AMI, 4 AOI, 7 DP, 21 AOR, 6 GAG.
    `codes=[]` ou None = tous les codes ; `recent=False` saute le rattrapage.
    """
    cfg = settings()["armp"]
    types = cfg.get("types") if types is None else types
    max_pages = cfg.get("max_pages", 20) if max_pages is None else max_pages
    year_pages = cfg.get("year_max_pages", 3) if exercices is None else max_pages
    exercices = cfg.get("exercices") if exercices is None else exercices
    codes = cfg.get("procedure_codes") if codes is None else codes
    search_url = cfg.get("search_url") or f"{cfg['base_url'].rstrip('/')}/recherche_avancee"
    # filtres_url peut contenir une query de départ : on ne garde que le chemin, les
    # paramètres (filtre, valeur_filtre) sont toujours reconstruits ci-dessous.
    filtres_url = (cfg.get("filtres_url") or f"{cfg['base_url'].rstrip('/')}/filtres_avancee").split("?")[0]
    seuil = cfg.get("seuil_marches", 1) if seuil is None else seuil

    def ingest(cap, url, page, ctx):
        nonlocal n
        if cap is None:
            return False
        records = parse_listing(cap.text(), cap.final_url)
        if not records:
            return False
        for rec in records:
            rec.update({"listing_url": url, "listing_page": page, "fetched_at": cap.fetched_at, **ctx})
            if append_jsonl_unique(interim("armp_publications.jsonl"), rec, key=lambda r: r["url"]):
                n += 1
            queue_documents("armp", rec["url"], rec["documents"],
                            {"reference": rec.get("reference"), "type_publication": rec.get("type_publication"),
                             **{k: ctx.get(k) for k in ("exercice", "procedure_code") if ctx.get(k)}})
            if details:
                _enrich_detail(http, rec)
        return True

    n = 0
    with PoliteClient("armp") as http:
        if recent:
            extra_filters = {
                "region": region, "departement": departement, "maitre_ouvrage": maitre_ouvrage,
                "recherche_parmi": chercher_parmi,
            }
            params0: list[tuple[str, str]] = [("recherche_avancee_do", "1"), ("seuil_marches", str(seuil))]
            params0 += [("type_publication[]", t) for t in (types or [])]
            params0 += [(k, str(v)) for k, v in extra_filters.items() if v not in (None, "", 0, "0")]
            if reference:
                params0.append(("reference_avis", reference))
            for page in range(1, max_pages + 1):
                params = list(params0)
                if page > 1:
                    params.append(("page", str(page)))
                url = f"{search_url}?{urlencode(params)}"
                cap = http.fetch(url, max_age_s=cfg.get("listing_max_age_s"))
                if not ingest(cap, url, page, {}):
                    break
                if page >= _last_page(cap.text()):
                    break

        for exercice in exercices or []:
            for code in codes or []:
                for page in range(1, year_pages + 1):
                    params = [("filtre", "avis"), ("valeur_filtre", "0"), ("exercice", str(exercice)),
                              ("type_procedure", str(code))]
                    if page > 1:
                        params.append(("page", str(page)))
                    url = f"{filtres_url}?{urlencode(params)}"
                    cap = http.fetch(url, max_age_s=cfg.get("listing_max_age_s"))
                    if not ingest(cap, url, page, {"exercice": exercice, "procedure_code": str(code)}):
                        break
                    cap_last = _last_page(cap.text())
                    if page >= cap_last:
                        break
    return n


def _enrich_detail(http: PoliteClient, rec: dict) -> None:
    cap = http.fetch(rec["url"])
    if cap is None:
        return
    html = cap.text()
    text = html_text(html)
    tree = HTMLParser(html)
    mo = _text(tree.css_first(".ac_mo"))
    objet = _text(tree.css_first(".entete_avis"))
    financement = _text(tree.css_first(".source-financement"))
    docs = document_links(absolute_links(tree.root, cap.final_url))
    detail = {
        **rec, "text": text[:30000], "label_values": fields.label_values(text),
        "tables": html_tables(html), "maitre_ouvrage": rec.get("maitre_ouvrage") or mo or None,
        "objet": objet or rec.get("title"), "financement": financement or None,
        "documents": docs or rec.get("documents", []), "fetched_at": cap.fetched_at,
    }
    append_jsonl_unique(interim("armp_details.jsonl"), detail, key=lambda r: r["url"])
    queue_documents("armp", rec["url"], detail["documents"],
                    {"reference": detail.get("reference"), "type_publication": detail.get("type_publication")})


def crawl_sanctions() -> int:
    """Tables statiques de /sanctions : #sanctions (exclusions) et #recours_home (recours)."""
    cfg = settings()["armp"]
    url = cfg.get("sanctions_url") or f"{cfg['base_url'].rstrip('/')}/sanctions"
    ns = 0
    with PoliteClient("armp") as http:
        cap = http.fetch(url)
        if cap is None:
            return 0
        records = parse_sanctions(cap.text(), cap.final_url)
        for rec in records:
            rec["fetched_at"] = cap.fetched_at
            append_jsonl_unique(interim("armp_sanctions.jsonl"), rec,
                                key=lambda r: (r["prestataire"], r["acte"], r["date_debut"]))
        ns = len(records)
        ns += crawl_recours(cap.text(), cap.final_url, cap.fetched_at)
    return ns


def crawl_recours(html: str | None = None, url: str = "https://www.armp.cm/sanctions",
                  fetched_at: str = None) -> int:
    """Table #recours_home (881 recours : code, prestataire, état, destinataire, objet).

    Appelé par crawl_sanctions (même page) ; arg `html` pour les tests.
    """
    tree = HTMLParser(html) if html else None
    table = tree.css_first("table#recours_home") if tree else None
    if table is None:
        return 0
    n = 0
    for tr in table.css("tr")[1:]:
        cells = [_text(c) for c in tr.css("td")]
        if len(cells) < 5:
            continue
        rec = {"source": "armp", "url": url, "code": cells[0], "prestataire": cells[1],
               "etat": cells[2], "destinataire": cells[3], "objet": cells[4],
               "fetched_at": fetched_at}
        if append_jsonl_unique(interim("armp_recours.jsonl"), rec,
                               key=lambda r: r["code"] or (r["prestataire"], r["objet"])):
            n += 1
    return n


def crawl_experts(region: str | None = None, ville: str | None = None,
                  domaines: list[str] | None = None, diplomes: str | None = None) -> int:
    """Table #experts (1 681 experts) ou /experts_search filtr�e."""
    cfg = settings()["armp"]
    params: list[tuple[str, str]] = []
    if region and region != "0":
        params.append(("region", region))
    if ville and ville != "0":
        params.append(("ville", ville))
    params += [("domaines[]", d) for d in (domaines or [])]
    if diplomes:
        params.append(("diplomes_input", diplomes))
    url = cfg.get("experts_search_url") if params else cfg.get("experts_url")
    url = url or f"{cfg['base_url'].rstrip('/')}/experts"
    if params:
        url = f"{url}?{urlencode(params)}"
    with PoliteClient("armp") as http:
        cap = http.fetch(url)
        if cap is None:
            return 0
        records = parse_experts(cap.text(), cap.final_url)
        for rec in records:
            rec["fetched_at"] = cap.fetched_at
            append_jsonl_unique(interim("armp_experts.jsonl"), rec,
                                key=lambda r: (r["nom"], r["instance"], r["ville"], r["qualification"]))
    return len(records)


def crawl_details(types: list[str] | None = None, start: int | None = None, end: int | None = None) -> int:
    cfg = settings()["armp"]
    types = types or cfg["types"]
    lo, hi = cfg["id_range"]
    start, end = start or lo, end or hi
    n = 0
    with PoliteClient("armp") as http:
        for type_ in types:
            missing = 0
            for pid in range(start, end + 1):
                url = cfg["detail_url"].format(type=type_, id=pid)
                cap = http.fetch(url)
                text = html_text(cap.text()) if cap else ""
                if cap is None or _is_empty(text):
                    missing += 1
                    if missing >= cfg["stop_after_consecutive_missing"]:
                        log.info("armp %s : arrêt à id=%s (%s absents de suite)", type_, pid, missing)
                        break
                    continue
                missing = 0
                html = cap.text()
                links = absolute_links(HTMLParser(html).root, cap.final_url)
                docs = document_links(links)
                rec = {
                    "source": "armp", "type_publication": type_, "id_publication": pid, "url": url,
                    "text": text[:30000], "label_values": fields.label_values(text),
                    "tables": html_tables(html), "reference": fields.reference(text),
                    "doc_type": fields.classify(type_ + " " + text[:500], text),
                    "amounts_fcfa": [a[0] for a in fields.amounts(text)][:50],
                    "documents": docs, "fetched_at": cap.fetched_at,
                }
                append_jsonl_unique(interim("armp_details.jsonl"), rec, key=lambda r: r["url"])
                queue_documents("armp", url, docs, {"reference": rec["reference"], "type_publication": type_})
                n += 1
    return n


def crawl_pridesoft(types: list[str] | None = None, start: int | None = None, end: int | None = None) -> int:
    cfg = settings()["pridesoft"]
    types = types or cfg["types"]
    lo, hi = cfg["id_range"]
    start, end = start or lo, end or hi
    n = 0
    with PoliteClient("pridesoft") as http:
        for type_ in types:
            missing = 0
            for rid in range(start, end + 1):
                url = cfg["report_url"].format(type=type_, id=rid, html=1)
                cap = http.fetch(url)
                text = html_text(cap.text()) if cap and "html" in cap.content_type else ""
                if cap is None or (not text and "pdf" not in cap.content_type) or (text and _is_empty(text)):
                    missing += 1
                    if missing >= cfg["stop_after_consecutive_missing"]:
                        break
                    continue
                missing = 0
                docs = []
                if text:
                    links = absolute_links(HTMLParser(cap.text()).root, cap.final_url)
                    docs = document_links(links)
                rec = {
                    "source": "pridesoft", "type_avis": type_, "id": rid, "url": url,
                    "content_type": cap.content_type, "path": relative_path(cap.path), "text": text[:30000],
                    "label_values": fields.label_values(text) if text else {},
                    "tables": html_tables(cap.text()) if text else [],
                    "reference": fields.reference(text) if text else None,
                    "documents": docs, "fetched_at": cap.fetched_at,
                }
                append_jsonl_unique(interim("pridesoft_reports.jsonl"), rec, key=lambda r: r["url"])
                queue_documents("pridesoft", url, docs, {"reference": rec["reference"]})
                n += 1
    return n


def pridesoft_path_meta(url: str) -> dict:
    """Date de dépôt et code de l'entité depuis le chemin /_lib/file/doc/0903/AAAA/MM/JJ/ENTITE/."""
    m = re.search(settings()["pridesoft"]["document_path_regex"], url)
    if not m:
        return {}
    return {"upload_date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "entity_code": m.group(4)}


def crawl_jdm(start: int | None = None, end: int | None = None) -> int:
    cfg = settings()["jdm"]
    if not cfg.get("enabled"):
        raise RuntimeError("JDM désactivé : activez jdm.enabled seulement avec l'accord de l'ARMP ou un abonnement.")
    lo, hi = cfg["num_range"]
    n = 0
    with PoliteClient("jdm") as http:
        for num in range(start or lo, (end or hi) + 1):
            cap = http.fetch(cfg["pdf_url"].format(num=num))
            if cap and "pdf" in cap.content_type:
                append_jsonl_unique(interim("jdm_documents_queue.jsonl"), {"source": "jdm", "url": cap.url, "title": f"JDM {num}", "referer": ""}, key=lambda r: r["url"])
                n += 1
    return n
