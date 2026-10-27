"""Katika237 : miroir des avis ARMP (≈ 143 800 entrées annoncées en octobre 2026).

Liste paginée par année, région et type :
    /tender/?o=&page={page}&r={region}&sort=date&type={type}&y={year}
Chaque item est gardé brut (texte + liens) ; les fiches détail et documents liés sont
mis en file pour téléchargement. Katika étant un miroir, toute ligne est rattachée à
sa source ARMP quand le lien d'origine est présent (champ `armp_links`).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import quote_plus

from ..config import settings
from ..extract import fields
from ..http import PoliteClient
from ..storage import append_jsonl_unique
from .common import document_links, interim, items_from_listing, queue_documents

log = logging.getLogger(__name__)
SOURCE = "katika"

# Les listes de l'année en cours bougent (nouveaux avis en tête) : on les revalide après ce délai.
REFRESH_CURRENT_YEAR_S = 6 * 86400
# Pages en échec d'affilée avant d'abandonner une combinaison année/région/type.
MAX_FAILED_PAGES = 2


def item_key(rec: dict) -> str:
    return rec["text"][:300]


def list_url(cfg: dict, page: int, year: int | str, region: str = "", type_: str = "") -> str:
    return cfg["list_url"].format(page=page, year=year, region=quote_plus(region), type=quote_plus(type_))


def crawl(years: list[int] | None = None, max_pages: int | None = None, details: bool = True) -> int:
    cfg = settings()["katika"]
    years = years or cfg["years"]
    max_pages = max_pages or cfg["max_pages"]
    detail_rx = re.compile(cfg["detail_link_regex"])
    out = interim("katika_items.jsonl")
    n_items = 0
    seen: set[str] = set()
    this_year = date.today().year
    with PoliteClient(SOURCE) as http:
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
                        items = items_from_listing(cap.text(), cap.final_url, cfg["item_selectors"])
                        fresh = 0
                        for it in items:
                            key = it["text"][:300]
                            if key in seen:
                                continue
                            seen.add(key)
                            fresh += 1
                            detail = next((l["url"] for l in it["links"] if detail_rx.search(l["url"])), None)
                            docs = document_links(it["links"], cfg["document_link_regex"])
                            rec = {
                                "source": SOURCE, "list_url": url, "year": year, "region": region,
                                "type_filter": type_, "page": page, "text": it["text"], "cells": it["cells"],
                                "detail_url": detail, "documents": docs,
                                "armp_links": [l["url"] for l in it["links"] if "armp.cm" in l["url"]],
                                "reference": fields.reference(it["text"]),
                                "date": str(fields.parse_date_fr(it["text"]) or ""),
                                "doc_type": fields.classify(it["text"]),
                                "fetched_at": cap.fetched_at,
                            }
                            if append_jsonl_unique(out, rec, key=item_key):
                                n_items += 1
                            queue_documents(SOURCE, url, docs, {"reference": rec["reference"]})
                            if details and detail:
                                _crawl_detail(http, detail, rec["reference"], cfg)
                        log.info("katika %s p%s : %s nouveaux items", year, page, fresh)
                        if fresh == 0:  # fin de pagination (page vide ou répétée)
                            break
    return n_items


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
        "armp_links": [l["url"] for l in links if "armp.cm" in l["url"]], "fetched_at": cap.fetched_at,
    }, key=lambda r: r["url"])
    queue_documents(SOURCE, url, docs, {"reference": reference})
