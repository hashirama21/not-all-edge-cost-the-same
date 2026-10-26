"""MINMAP : communiqués d'exclusion, de suspension et de résiliation (vérité terrain forte).

Le site publie des permaliens WordPress (minmap.cm/?p=19494). On interroge d'abord l'API
REST WordPress (/wp-json/wp/v2/posts?search=…) ; si elle est fermée, on passe par la
recherche HTML (/?s=…). Les communiqués sont souvent des PDF ou des images : leurs liens
sont mis en file, et la liste des entreprises est extraite ensuite (regex puis LLM).
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import settings
from ..extract import fields
from ..extract.documents import html_text
from ..http import PoliteClient
from ..storage import append_jsonl
from .common import absolute_links, document_links, interim, queue_documents

log = logging.getLogger(__name__)
SOURCE = "minmap"

RE_DUREE = re.compile(r"(?:pour\s+une\s+(?:dur[ée]e|p[ée]riode)\s+de|pendant)\s+(\w+)\s*(?:\(\d+\)\s*)?(ans?|mois)", re.IGNORECASE)
RE_LIST_ITEM = re.compile(r"(?m)^\s*(?:\d{1,4}[\s.)\-]+)\s*([A-Z0-9][A-Z0-9&'’.\- ]{3,120}?)(?:\s+(?:SARL|SA|SAS|SARLU|GIE|ETS)\b[^\n]*)?\s*$")


def crawl() -> int:
    cfg = settings()["minmap"]
    n = 0
    with PoliteClient(SOURCE) as http:
        for term in cfg["search_terms"]:
            page = 1
            while True:
                url = f"{cfg['wp_api']}?search={quote_plus(term)}&per_page=50&page={page}&_fields=id,date,link,title,content"
                cap = http.fetch(url)
                if cap is None or "json" not in cap.content_type:
                    n += _crawl_html_search(http, cfg, term)
                    break
                try:
                    posts = json.loads(cap.text())
                except json.JSONDecodeError:
                    break
                if not isinstance(posts, list) or not posts:
                    break
                for post in posts:
                    html = post.get("content", {}).get("rendered", "")
                    n += _record(post.get("link"), post.get("title", {}).get("rendered", ""), html, post.get("date"))
                page += 1
    return n


def _crawl_html_search(http: PoliteClient, cfg: dict, term: str) -> int:
    n = 0
    cap = http.fetch(f"{cfg['base_url']}/?s={quote_plus(term)}")
    if cap is None:
        return 0
    for link in absolute_links(HTMLParser(cap.text()).root, cap.final_url):
        if re.search(r"[?&]p=\d+|/\d{4}/\d{2}/", link["url"]):
            post = http.fetch(link["url"])
            if post:
                n += _record(link["url"], link["text"], post.text(), None)
    return n


def _record(url: str, title: str, html: str, published: str | None) -> int:
    text = html_text(html)
    if fields.classify(title, text) != "sanction":
        return 0
    links = absolute_links(HTMLParser(html).root, url or "")
    docs = document_links(links) + [l for l in links if re.search(r"\.(jpe?g|png)$", l["url"], re.I)]
    duree = RE_DUREE.search(text)
    append_jsonl(interim("minmap_sanctions_raw.jsonl"), {
        "source": SOURCE, "url": url, "title": HTMLParser(title).text() if title else "",
        "published": published, "text": text[:50000],
        "duree": " ".join(duree.groups()) if duree else None,
        "entreprises_regex": [m.strip() for m in RE_LIST_ITEM.findall(text)][:2000],
        "documents": docs,
    })
    queue_documents(SOURCE, url, docs, {"kind": "sanction"})
    return 1
