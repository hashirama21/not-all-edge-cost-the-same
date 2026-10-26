"""Outils communs aux collecteurs : extraction générique d'items, liens, files de documents."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

from ..config import data_dir
from ..storage import append_jsonl

DOC_RE = re.compile(r"\.(pdf|docx?|xlsx?|zip)(\?|#|$)", re.IGNORECASE)


def interim(name: str) -> Path:
    """data/interim/<source>/<name>, la source étant le préfixe du nom (katika_items.jsonl -> katika)."""
    return data_dir("interim", name.split("_", 1)[0]) / name


def absolute_links(node: Node, base_url: str) -> list[dict]:
    out = []
    for a in node.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        out.append({"url": urljoin(base_url, href), "text": a.text(strip=True)})
    return out


def items_from_listing(html: str, base_url: str, selectors: list[str]) -> list[dict]:
    """Découpe une page de liste en items, avec le premier sélecteur qui donne ≥ 2 nœuds.

    Sans sélecteur valide, on cherche le motif répété le plus fréquent (balise.classe)
    parmi les nœuds qui contiennent au moins un lien.
    """
    tree = HTMLParser(html)
    nodes: list[Node] = []
    for sel in selectors:
        nodes = [n for n in tree.css(sel) if n.css_first("a[href]") is not None]
        if len(nodes) >= 2:
            break
    if len(nodes) < 2:
        nodes = _repeated_nodes(tree)
    items = []
    for n in nodes:
        cells = [c.text(strip=True) for c in n.css("td")] if n.tag == "tr" else []
        items.append({
            "text": n.text(separator=" | ", strip=True),
            "cells": cells,
            "links": absolute_links(n, base_url),
        })
    return items


def _repeated_nodes(tree: HTMLParser) -> list[Node]:
    signature: Counter[str] = Counter()
    by_sig: dict[str, list[Node]] = {}
    body = tree.body
    if body is None:
        return []
    for n in body.traverse():
        if n.tag in ("a", "span", "i", "b", "strong", "em", "body", "html") or n.css_first("a[href]") is None:
            continue
        sig = f"{n.tag}.{n.attributes.get('class') or ''}"
        signature[sig] += 1
        by_sig.setdefault(sig, []).append(n)
    for sig, count in signature.most_common():
        if count >= 3:
            return by_sig[sig]
    return []


def document_links(links: list[dict], pattern: str | None = None) -> list[dict]:
    rx = re.compile(pattern, re.IGNORECASE) if pattern else DOC_RE
    return [l for l in links if rx.search(l["url"])]


def queue_documents(source: str, referer: str, links: list[dict], context: dict | None = None) -> None:
    for link in links:
        append_jsonl(interim(f"{source}_documents_queue.jsonl"), {
            "source": source, "url": link["url"], "title": link.get("text", ""),
            "referer": referer, **(context or {}),
        })


def describe_page(html: str, base_url: str) -> dict:
    """Résumé structurel d'une page, pour ajuster les sélecteurs (commande `discover`)."""
    tree = HTMLParser(html)
    links = absolute_links(tree.root, base_url)
    host = urlsplit(base_url).netloc
    internal = [l["url"] for l in links if urlsplit(l["url"]).netloc == host]
    patterns = Counter(re.sub(r"\d+", "{n}", urlsplit(u).path + ("?" + urlsplit(u).query if urlsplit(u).query else "")) for u in internal)
    classes = Counter(
        f"{n.tag}.{n.attributes.get('class')}" for n in tree.root.traverse()
        if n.attributes.get("class") and n.css_first("a[href]") is not None
    )
    forms = [{
        "action": urljoin(base_url, f.attributes.get("action") or ""),
        "method": (f.attributes.get("method") or "get").lower(),
        "fields": [{"name": i.attributes.get("name"), "type": i.attributes.get("type") or i.tag,
                    "options": [o.attributes.get("value") for o in i.css("option")][:50]}
                   for i in f.css("input, select, textarea") if i.attributes.get("name")],
    } for f in tree.css("form")]
    return {
        "title": (tree.css_first("title").text(strip=True) if tree.css_first("title") else ""),
        "n_links": len(links),
        "url_patterns": patterns.most_common(30),
        "repeated_blocks": classes.most_common(20),
        "tables": len(tree.css("table")),
        "forms": forms,
        "documents": [l["url"] for l in document_links(links)][:30],
    }
