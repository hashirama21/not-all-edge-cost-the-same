"""COLEPS (marchespublics.cm / publiccontracts.cm) : collecte déterministe par Playwright.

Le portail est rendu en JavaScript ; la carte du site (sélecteurs, pagination) est
produite par `cmrmp agent explore` (browser-use + modèle local llama.cpp), puis relue.
Ce module valide la carte (schéma, domaines autorisés) et la rejoue, page par page,
avec les mêmes délais de politesse que le client HTTP.
Les requêtes XHR JSON observées sont aussi archivées : si une API publique existe,
préférez-la ensuite au rendu HTML.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..config import data_dir, settings
from ..storage import RawStore, append_jsonl
from .common import interim, queue_documents

log = logging.getLogger(__name__)
SOURCE = "coleps"

# Balises qu'on ne prend pas pour un nom de paramètre d'URL (« button » est un sélecteur).
_HTML_TAGS = {"a", "button", "li", "span", "div", "nav", "input", "ul", "i", "img"}


def allowed_hosts() -> set[str]:
    return {d.lstrip("*.").lower() for d in settings()["coleps"]["allowed_domains"]}


def host_allowed(url: str, allowed: set[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def page_param(pagination: str) -> str | None:
    """Nom du paramètre d'URL si la pagination en est un (« ?page= », « &p=2 », « page »), sinon None."""
    pag = (pagination or "").strip()
    m = re.fullmatch(r"[?&]?([A-Za-z_][\w\-]*)(?:=\S*)?", pag)
    if not m:
        return None
    if pag[0] in "?&" or "=" in pag or m.group(1).lower() not in _HTML_TAGS:
        return m.group(1)
    return None


def next_page_url(url: str, param: str, page_no: int) -> str:
    """URL de la page `page_no`, en remplaçant le paramètre s'il est déjà présent."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, str(page_no)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def load_sitemap(path: Path) -> dict:
    """Carte validée par le schéma de l'agent ; les listes hors domaines autorisés sont écartées."""
    from pydantic import ValidationError

    from ..agents.browser_agent import SiteMap

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        sitemap = SiteMap.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"carte COLEPS invalide ({path}) : l'agent n'a pas produit de carte conforme. {exc}") from exc
    allowed = allowed_hosts()
    listings = []
    for listing in sitemap.listings:
        if host_allowed(listing.url, allowed):
            listings.append(listing.model_dump())
        else:
            log.warning("liste écartée, domaine non autorisé : %s", listing.url)
    return {**sitemap.model_dump(), "listings": listings}


def crawl(sitemap_path: Path, max_pages: int = 500) -> int:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    sitemap = load_sitemap(sitemap_path)
    pol = settings()["politeness"]
    store = RawStore(SOURCE)
    allowed = allowed_hosts()
    n = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=f"cmrmp/0.1 (+mailto:{settings()['contact']})", accept_downloads=False)
        page = ctx.new_page()

        def on_response(resp):
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype and host_allowed(resp.url, allowed):
                try:
                    store.put(resp.url, resp.url, resp.status, dict(resp.headers), resp.body(), {"xhr": True})
                except Exception:
                    pass

        page.on("response", on_response)
        for listing in sitemap["listings"]:
            try:
                n += _crawl_listing(page, listing, store, pol, max_pages)
            except PlaywrightError as exc:  # délai dépassé, page cassée : on passe à la liste suivante
                log.error("coleps : liste %s abandonnée : %s", listing["url"], exc)
        browser.close()
    log.info("coleps : %s items", n)
    return n


def _crawl_listing(page, listing: dict, store: RawStore, pol: dict, max_pages: int) -> int:
    url = listing["url"]
    param = page_param(listing["pagination"])
    page.goto(url, wait_until="networkidle")
    n, previous = 0, None
    for page_no in range(1, max_pages + 1):
        html = page.content()
        digest = hashlib.sha256(html.encode()).hexdigest()
        if digest == previous:  # le site ignore le paramètre de page, ou fin de liste
            break
        previous = digest
        store.put(f"{url}#page={page_no}", page.url, 200, {"content-type": "text/html"}, html.encode())
        items = page.query_selector_all(listing["item_selector"])
        if not items:
            break
        for item in items:
            rec = {"source": SOURCE, "listing": listing["description"], "page": page_no, "url": page.url}
            cells = [c.inner_text().strip() for c in item.query_selector_all("td")]
            if cells:
                rec["cells"] = cells
            elif not item.query_selector("a[href]"):  # ligne d'en-tête (th) ou ligne vide
                continue
            for i, f in enumerate(listing["fields"]):
                # Sans sélecteur (l'agent n'en a pas toujours trouvé), la cellule de même rang.
                if (f.get("css") or "").strip():
                    el = item.query_selector(f["css"])
                    rec[f["name"]] = el.inner_text().strip() if el else None
                else:
                    rec[f["name"]] = cells[i] if i < len(cells) else None
            for key, sel in (("detail_url", listing.get("detail_link_selector")),
                             ("document_url", listing.get("document_link_selector"))):
                el = item.query_selector(sel) if sel else None
                href = el.get_attribute("href") if el else None
                rec[key] = urljoin(page.url, href) if href else None
            append_jsonl(interim("coleps_items.jsonl"), rec)
            if rec.get("document_url"):
                queue_documents(SOURCE, page.url, [{"url": rec["document_url"], "text": rec.get("reference") or ""}])
            n += 1
        time.sleep(pol["min_delay_s"] + random.uniform(0, pol["jitter_s"]))
        if param:
            page.goto(next_page_url(url, param, page_no + 1), wait_until="networkidle")
        else:
            nxt = page.query_selector(listing["pagination"])
            if not nxt or not nxt.is_enabled():
                break
            nxt.click()
            page.wait_for_load_state("networkidle")
    return n


def default_sitemap_path() -> Path:
    """Carte relue et versionnée dans le dépôt si elle existe, sinon la dernière produite par l'agent."""
    from ..config import ROOT

    reviewed = ROOT / "config" / "sitemaps" / "coleps.json"
    return reviewed if reviewed.exists() else data_dir("sitemaps") / "coleps.json"
