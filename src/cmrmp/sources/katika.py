"""Katika237 : miroir des avis ARMP (≈ 143 800 entrées annoncées en octobre 2026).

Liste paginée par année, région et type :
    /tender/?o=&page={page}&r={region}&sort={sort}&type={type}&y={year}
    (« o » filtre par maître d'ouvrage, identifiant interne du site)
Chaque item est gardé brut (texte + liens) ; les fiches détail et documents liés sont
mis en file pour téléchargement. Katika étant un miroir, toute ligne est rattachée à
sa source ARMP quand le lien d'origine est présent (champ `armp_links`).

Parsing : le site sert des blocs sémantiques `div.tender-entry` (span.date, a.owner
avec le nom complet en attribut title, a.region, span.type, span.cost dont l'attribut
title porte le montant exact en entier). Le parser BeautifulSoup (`parse_entries_bs4`)
récupère tous ces champs ; s'il ne trouve rien (site modifié), repli sur le découpage
générique `items_from_listing`. `cmrmp crawl katika --rebuild` reparse les pages de
liste déjà capturées pour uniformiser le fichier d'items, `cmrmp crawl katika-map`
produit une cartographie du site (filtres disponibles, volumes par filtre).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit

from ..config import settings
from ..extract import fields
from ..http import PoliteClient
from ..storage import append_jsonl, append_jsonl_unique, iter_manifest
from ..time import utc_now_iso
from .common import document_links, interim, items_from_listing, queue_documents

log = logging.getLogger(__name__)
SOURCE = "katika"

# Les listes de l'année en cours bougent (nouveaux avis en tête) : on les revalide après ce délai.
REFRESH_CURRENT_YEAR_S = 6 * 86400
# Pages en échec d'affilée avant d'abandonner une combinaison année/région/type.
MAX_FAILED_PAGES = 3

# Mois anglais des dates du site (« Nov 10, 2026 ») ; indépendant de la locale (pas de %b).
_MONTHS_EN = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
RE_SITE_DATE = re.compile(r"\b([A-Z][a-z]{2})\s+(\d{1,2}),\s*((?:19|20)\d{2})\b")


def parse_site_date(text: str) -> str:
    """« Nov 10, 2026 » -> « 2026-11-10 » (ISO), sinon « »."""
    m = RE_SITE_DATE.search(text or "")
    if not m:
        return ""
    month = _MONTHS_EN.get(m.group(1).lower())
    if not month:
        return ""
    try:
        return date(int(m.group(3)), month, int(m.group(2))).isoformat()
    except ValueError:
        return ""


def item_key(rec: dict) -> str:
    """Clé de déduplication : l'URL de fiche ARMP (stable), sinon le texte."""
    return rec.get("detail_url") or rec["text"][:300]


def list_url(cfg: dict, page: int, year: int | str, region: str = "", type_: str = "",
             sort: str = "date") -> str:
    return cfg["list_url"].format(page=page, year=year, region=quote_plus(region),
                                  type=quote_plus(type_), sort=quote_plus(sort))


# --------------------------------------------------------------------------- parser BeautifulSoup
def _txt(node) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""


def parse_site_meta(html: str) -> dict:
    """Métadonnées d'une page /tender/ : total, dernière page, filtres proposés."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    types, regions, sorts, pages = set(), set(), set(), []
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if "/tender/" not in href:
            continue
        q = parse_qs(urlsplit(href).query)
        if q.get("type"):
            types.update(q["type"])
        if q.get("r"):
            regions.update(q["r"])
        if q.get("sort"):
            sorts.update(q["sort"])
        if q.get("page", [""])[0].isdigit():
            pages.append(int(q["page"][0]))
    count_node = soup.select_one("div.row.total span.result-count")
    count = None
    if count_node:
        digits = re.sub(r"\D", "", count_node.get_text(strip=True))
        count = int(digits) if digits else None
    return {
        "result_count": count,
        "last_page": max(pages) if pages else None,
        "types": sorted(types),
        "regions": sorted(regions),
        "sorts": sorted(sorts),
    }


def parse_entries_bs4(html: str, base_url: str) -> tuple[list[dict], dict]:
    """Items structurés d'une page /tender/ via BeautifulSoup.

    Renvoie (items, meta) où chaque item porte les champs du bloc `div.tender-entry` :
    date (ISO), maître d'ouvrage (code + nom complet + identifiant site), région, type
    d'avis, montant exact (attribut title de span.cost), titre, liens source/documents.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    meta = parse_site_meta(html)
    out = []
    for entry in soup.select("div.tender-entry"):
        date_txt = _txt(entry.select_one("span.date"))
        owner = entry.select_one("a.owner") or entry.select_one("span.owner")
        owner_code = _txt(owner)
        owner_full = (owner.get("title") or "").strip() if owner is not None else ""
        owner_id = None
        if owner is not None:
            m = re.search(r"[?&]o=(\d*)", owner.get("href") or "")
            owner_id = m.group(1) if (m and m.group(1)) else None
        region = _txt(entry.select_one("a.region"))
        type_ = _txt(entry.select_one("span.type"))
        cost_node = entry.select_one("span.cost")
        cost = None
        if cost_node is not None:
            raw = (cost_node.get("title") or "").strip()
            if raw.isdigit():
                cost = int(raw)
            else:
                cost = fields.parse_fcfa(_txt(cost_node) or raw)
        title = _txt(entry.select_one("div.row.title"))
        links = [{"url": urljoin(base_url, a.get("href") or ""), "text": _txt(a)}
                 for a in entry.select("a[href]") if (a.get("href") or "").strip()]
        detail = next((l["url"] for l in links if "/details?" in l["url"]), None)
        docs = document_links(links, settings()["katika"]["document_link_regex"])
        out.append({
            "date": parse_site_date(date_txt),
            "maitre_ouvrage": owner_code or None,
            "maitre_ouvrage_complet": owner_full or None,
            "owner_id": owner_id,
            "region": region or None,
            "type_avis": type_ or None,
            "montant_fcfa": cost,
            "title": title,
            "reference": fields.reference(title),
            "doc_type": fields.classify(f"{type_} {title}", title),
            "detail_url": detail,
            "documents": docs,
            "armp_links": [l["url"] for l in links if "armp.cm" in l["url"]],
            "text": " | ".join(x for x in (date_txt, type_, title) if x),
            "links": links,
        })
    return out, meta


def crawl(years: list[int] | None = None, max_pages: int | None = None, details: bool = True) -> int:
    cfg = settings()["katika"]
    pol = settings()["politeness"]
    years = years or cfg["years"]
    max_pages = max_pages or cfg["max_pages"]
    detail_rx = re.compile(cfg["detail_link_regex"])
    out = interim("katika_items.jsonl")
    n_items = 0
    seen: set[str] = set()
    this_year = date.today().year
    with PoliteClient(SOURCE, min_delay_s=cfg.get("min_delay_s", pol["min_delay_s"]),
                      jitter_s=cfg.get("jitter_s", pol["jitter_s"])) as http:
        for year in years:
            max_age = REFRESH_CURRENT_YEAR_S if int(year) >= this_year else None
            for region in cfg["regions"]:
                for type_ in cfg["types"]:
                    failed = 0
                    for page in range(1, max_pages + 1):
                        url = list_url(cfg, page, year, region, type_)
                        cap = http.fetch(url, max_age_s=max_age)
                        if cap is None:
                            failed += 1
                            log.warning("katika %s p%s : page indisponible (%s/%s)", year, page, failed, MAX_FAILED_PAGES)
                            if failed >= MAX_FAILED_PAGES:
                                break
                            continue
                        failed = 0
                        items, meta = parse_entries_bs4(cap.text(), cap.final_url)
                        if meta.get("result_count"):
                            log.info("katika %s p%s : %s entrées annoncées (dernière page %s)",
                                     year, page, meta["result_count"], meta.get("last_page"))
                        recs = []
                        if items:
                            for it in items:
                                recs.append({
                                    "source": SOURCE, "list_url": url, "year": year,
                                    "region_filter": region, "type_filter": type_, "page": page,
                                    "fetched_at": cap.fetched_at, **it,
                                })
                        else:  # repli : découpage générique (site modifié, page atypique)
                            for it in items_from_listing(cap.text(), cap.final_url, cfg["item_selectors"]):
                                detail = next((link["url"] for link in it["links"]
                                               if detail_rx.search(link["url"])), None)
                                if not detail:
                                    detail = next((link["url"] for link in it["links"]
                                                   if "armp.cm/details" in link["url"]), None)
                                docs = document_links(it["links"], cfg["document_link_regex"])
                                recs.append({
                                    "source": SOURCE, "list_url": url, "year": year,
                                    "region_filter": region, "type_filter": type_, "page": page,
                                    "text": it["text"], "cells": it["cells"],
                                    "detail_url": detail, "documents": docs,
                                    "armp_links": [link["url"] for link in it["links"] if "armp.cm" in link["url"]],
                                    "reference": fields.reference(it["text"]),
                                    "date": str(fields.parse_date_fr(it["text"]) or ""),
                                    "doc_type": fields.classify(it["text"]),
                                    "fetched_at": cap.fetched_at,
                                })
                        fresh = 0
                        for rec in recs:
                            key = item_key(rec)
                            if key in seen:
                                continue
                            seen.add(key)
                            fresh += 1
                            if append_jsonl_unique(out, rec, key=item_key):
                                n_items += 1
                            queue_documents(SOURCE, url, rec.get("documents") or [],
                                            {"reference": rec.get("reference")})
                            if details and rec.get("detail_url"):
                                _crawl_detail(http, rec["detail_url"], rec.get("reference"), cfg)
                        log.info("katika %s p%s : %s nouveaux items", year, page, fresh)
                        if fresh == 0:  # fin de pagination (page vide ou répétée)
                            break
                        if meta.get("last_page") and page >= meta["last_page"]:
                            break  # pagination réelle du site atteinte
    return n_items


def rebuild_items() -> int:
    """Reparsage BeautifulSoup des pages de liste déjà capturées : réécrit katika_items.jsonl.

    Uniformise les items collectés avec l'ancien parser générique, sans re-télécharger
    (cache disque). L'ancien fichier est gardé en .bak ; les documents sont remis en file
    (dédupliqués par URL)."""
    cfg = settings()["katika"]
    detail_rx = re.compile(cfg["detail_link_regex"])
    items: dict[str, dict] = {}
    for cap in iter_manifest(SOURCE):
        if urlsplit(cap.url).path.rstrip("/") != "/tender":
            continue
        try:
            entries, _meta = parse_entries_bs4(cap.text(), cap.final_url)
        except Exception as exc:  # page corrompue : on garde les autres
            log.warning("rebuild %s : %s", cap.url, exc)
            continue
        for it in entries:
            key = it.get("detail_url") or it["text"][:300]
            if key not in items:
                q = {k: v for k, v in parse_qs(urlsplit(cap.url).query).items()}
                it.update({
                    "source": SOURCE, "list_url": cap.url,
                    "year": int((q.get("y") or [0])[0] or 0),
                    "region_filter": (q.get("r") or [""])[0], "type_filter": (q.get("type") or [""])[0],
                    "page": int((q.get("page") or [0])[0] or 0),
                    "fetched_at": cap.fetched_at,
                })
                items[key] = it
    out = interim("katika_items.jsonl")
    backup = out.with_suffix(".jsonl.bak")
    if out.exists():
        if backup.exists():
            backup.unlink()
        out.rename(backup)
    for rec in items.values():
        append_jsonl(out, rec)
        queue_documents(SOURCE, rec["list_url"], rec.get("documents") or [],
                        {"reference": rec.get("reference")})
    log.info("katika rebuild : %s items (ancien fichier : %s)", len(items), backup.name)
    return len(items)


def cartography(years: list[int] | None = None) -> dict:
    """Cartographie katika237.com/tender/ : filtres disponibles et volumes par filtre.

    Lit les menus de la page (types, régions, tris), le compteur global, puis sonde
    page 1 de chaque type et de chaque année (result_count). Écrit dans
    data/reports/katika_cartography.json via la commande `cmrmp crawl katika-map`."""
    cfg = settings()["katika"]
    pol = settings()["politeness"]
    years = years or cfg["years"]
    this_year = date.today().year
    report: dict = {"source": "katika237.com/tender/", "generated_at_utc": utc_now_iso()}
    with PoliteClient(SOURCE, min_delay_s=cfg.get("min_delay_s", pol["min_delay_s"]),
                      jitter_s=cfg.get("jitter_s", pol["jitter_s"])) as http:
        url = list_url(cfg, 1, this_year)
        cap = http.fetch(url, refresh=True)
        if cap is None:
            report["erreur"] = "page /tender/ inaccessible"
            return report
        meta = parse_site_meta(cap.text())
        report.update({
            "annee_sondee": this_year,
            "total_entries": meta["result_count"],
            "derniere_page": meta["last_page"],
            "types": meta["types"],
            "regions_visibles": meta["regions"],
            "tris": meta["sorts"],
            "items_par_page": None,
        })
        entries, _ = parse_entries_bs4(cap.text(), cap.final_url)
        if entries:
            report["items_par_page"] = len(entries)
        counts: dict[str, int | None] = {}
        for t in meta["types"]:
            cap_t = http.fetch(list_url(cfg, 1, this_year, type_=t))
            m = parse_site_meta(cap_t.text()) if cap_t else {}
            counts[t] = m.get("result_count")
        for y in years:
            cap_y = http.fetch(list_url(cfg, 1, y))
            m = parse_site_meta(cap_y.text()) if cap_y else {}
            counts[str(y)] = m.get("result_count")
        report["result_count_par_filtre"] = counts
    import json
    from ..config import data_dir

    out = data_dir("reports") / "katika_cartography.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _crawl_detail(http: PoliteClient, url: str, reference: str | None, cfg: dict) -> None:
    cap = http.fetch(url)
    if cap is None:
        return
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
    from .common import absolute_links
    from ..extract.documents import html_text

    html = cap.text()
    text = html_text(html)
    links = absolute_links(HTMLParser(html).root, cap.final_url)
    docs = document_links(links, cfg["document_link_regex"])
    append_jsonl_unique(interim("katika_details.jsonl"), {
        "source": SOURCE, "url": url, "reference": reference or fields.reference(text),
        "text": text[:20000], "label_values": fields.label_values(text), "documents": docs,
        "armp_links": [link["url"] for link in links if "armp.cm" in link["url"]], "fetched_at": cap.fetched_at,
    }, key=lambda r: r["url"])
    queue_documents(SOURCE, url, docs, {"reference": reference})
