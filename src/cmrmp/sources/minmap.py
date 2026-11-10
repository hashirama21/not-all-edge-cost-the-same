"""MINMAP : communiqués d'exclusion, de suspension et de résiliation (vérité terrain forte).

Le site est un Joomla : la recherche publique passe par `com_finder`
(`/index.php?option=com_finder&view=search&q=…`). On tente d'abord l'API REST WordPress
(historique) ; si elle ne répond pas, on interroge le moteur Joomla et on suit les articles
`com_content`. Les communiqués sont souvent des PDF ou des images : leurs liens sont mis en
file, et la liste des entreprises est extraite ensuite (regex puis LLM).
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote_plus, urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import settings
from ..extract import fields
from ..extract.documents import html_text
from ..http import PoliteClient
from ..storage import append_jsonl_unique
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
                    # Au-delà de la dernière page, WordPress répond 400 : c'est la fin de la
                    # pagination. Le repli HTML ne sert que si l'API ne répond pas dès la page 1.
                    if page == 1:
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
    """Recherche Joomla `com_finder`, puis lecture des articles `com_content` trouvés."""
    finder = cfg.get("finder_url") or f"{cfg['base_url']}/index.php?option=com_finder&view=search"
    sep = "&" if "?" in finder else "?"
    cap = http.fetch(f"{finder}{sep}q={quote_plus(term)}&lang=fr")
    if cap is None:
        return 0
    tree = HTMLParser(cap.text())
    links, seen = [], set()
    for selector in (".search-results .result-title a[href]", ".com-finder__results .result-title a[href]",
                     ".search-results a[href]", ".com-finder__results a[href]"):
        for a in tree.css(selector):
            href = a.attributes.get("href") or ""
            if not href:
                continue
            url = urljoin(cap.final_url, href)
            if url in seen:
                continue
            if re.search(r"option=com_content.*view=article|[?&]p=\d+|/\d{4}/\d{2}/", url):
                seen.add(url)
                links.append({"url": url, "text": a.text(strip=True)})
    n = 0
    for link in links:
        post = http.fetch(link["url"])
        if post:
            n += _record(link["url"], link["text"], post.text(), None)
    return n


def _article_node(html: str):
    tree = HTMLParser(html)
    for selector in ("article", ".com-content-article__body", ".item-page", "main"):
        node = tree.css_first(selector)
        if node is not None:
            return node
    return tree.body or tree.root


def _record(url: str, title: str, html: str, published: str | None) -> int:
    node = _article_node(html)
    text = node.text(separator="\n", strip=True) if node is not None else html_text(html)
    title_text = HTMLParser(title).text() if title else ""
    # Un communiqué de sanction cite souvent l'appel d'offres ou l'attributaire : on ne
    # demande que la présence de mots-clés de sanction, pas le type « dominant ».
    if not fields.is_sanction(title_text, text):
        return 0
    links = absolute_links(node, url or "")
    docs = document_links(links) + [link for link in links if re.search(r"\.(jpe?g|png)$", link["url"], re.I)]
    duree = RE_DUREE.search(text)
    written = append_jsonl_unique(interim("minmap_sanctions_raw.jsonl"), {
        "source": SOURCE, "url": url, "title": title_text,
        "published": published, "text": text[:50000],
        "duree": " ".join(duree.groups()) if duree else None,
        "entreprises_regex": [m.strip() for m in RE_LIST_ITEM.findall(text)][:2000],
        "documents": docs,
    }, key=lambda r: r["url"])
    queue_documents(SOURCE, url, docs, {"kind": "sanction"})
    return int(written)
