"""ARMP : fiches de publication (armp.cm/details) et rapports PRIDESOFT.

- armp.cm/details?type_publication=DEC-ATTR&id_publication=N : fiche HTML (attributaire,
  montant, délai…). Les identifiants sont séquentiels : on les énumère et on s'arrête
  après une longue série d'identifiants absents.
- pridesoft.armp.cm/0903_publications_report_appel_offres/index.php?type_avis=AO&id=N :
  rapport d'avis (HTML si affichehtml=1, sinon document).
- Les documents sources sont sous pridesoft.armp.cm/_lib/file/doc/0903/AAAA/MM/JJ/ENTITE/.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlsplit

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


def discover_types() -> dict[str, int]:
    """Relève les codes type_publication présents sur les pages de liste ARMP."""
    cfg = settings()["armp"]
    counts: dict[str, int] = {}
    with PoliteClient("armp") as http:
        for url in cfg["listing_urls"]:
            cap = http.fetch(url, refresh=True)
            if cap is None:
                continue
            for link in absolute_links(HTMLParser(cap.text()).root, cap.final_url):
                q = parse_qs(urlsplit(link["url"]).query)
                for t in q.get("type_publication", []):
                    counts[t] = counts.get(t, 0) + 1
    return counts


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
