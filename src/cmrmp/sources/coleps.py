"""COLEPS (marchespublics.cm / publiccontracts.cm) : collecte déterministe par Playwright.

Le portail est rendu en JavaScript ; la carte du site (sélecteurs, pagination) est
produite une fois par `cmrmp agent explore` puis relue à la main. Ce module rejoue la
carte, page par page, avec les mêmes règles de politesse que le client HTTP.
Les requêtes XHR JSON observées sont aussi archivées : si une API publique existe,
préférez-la ensuite au rendu HTML.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..config import data_dir, settings
from ..storage import RawStore, append_jsonl
from .common import interim, queue_documents

log = logging.getLogger(__name__)
SOURCE = "coleps"


def crawl(sitemap_path: Path, max_pages: int = 500) -> int:
    from playwright.sync_api import sync_playwright

    sitemap = json.loads(Path(sitemap_path).read_text(encoding="utf-8"))
    pol = settings()["politeness"]
    store = RawStore(SOURCE)
    allowed = {d.lstrip("*.") for d in settings()["coleps"]["allowed_domains"]}
    n = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=f"cmrmp/0.1 (+mailto:{settings()['contact']})", accept_downloads=False)
        page = ctx.new_page()

        def on_response(resp):
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype and any(urlsplit(resp.url).netloc.endswith(d) for d in allowed):
                try:
                    store.put(resp.url, resp.url, resp.status, dict(resp.headers), resp.body(), {"xhr": True})
                except Exception:
                    pass

        page.on("response", on_response)
        for listing in sitemap.get("listings", []):
            url = listing["url"]
            page.goto(url, wait_until="networkidle")
            for page_no in range(1, max_pages + 1):
                html = page.content()
                store.put(f"{url}#page={page_no}", page.url, 200, {"content-type": "text/html"}, html.encode())
                items = page.query_selector_all(listing["item_selector"])
                if not items:
                    break
                for item in items:
                    rec = {"source": SOURCE, "listing": listing["description"], "page": page_no, "url": page.url}
                    for f in listing["fields"]:
                        el = item.query_selector(f["css"])
                        rec[f["name"]] = el.inner_text().strip() if el else None
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
                pag = listing["pagination"]
                if pag.startswith(("?", "&")) or "=" in pag:
                    # paramètre d'URL : ex. "page"
                    param = pag.strip("?&").split("=")[0]
                    sep = "&" if "?" in url else "?"
                    page.goto(f"{url}{sep}{param}={page_no + 1}", wait_until="networkidle")
                else:
                    nxt = page.query_selector(pag)
                    if not nxt or not nxt.is_enabled():
                        break
                    nxt.click()
                    page.wait_for_load_state("networkidle")
        browser.close()
    log.info("coleps : %s items", n)
    return n


def default_sitemap_path() -> Path:
    """Carte relue et versionnée dans le dépôt si elle existe, sinon la dernière produite par l'agent."""
    from ..config import ROOT

    reviewed = ROOT / "config" / "sitemaps" / "coleps.json"
    return reviewed if reviewed.exists() else data_dir("sitemaps") / "coleps.json"
