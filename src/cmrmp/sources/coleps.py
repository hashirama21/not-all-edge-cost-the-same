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
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import data_dir, settings
from ..extract import fields
from ..http import PoliteClient
from ..storage import RawStore, append_jsonl, append_jsonl_unique
from .common import interim, queue_documents

log = logging.getLogger(__name__)
SOURCE = "coleps"

# Balises qu'on ne prend pas pour un nom de paramètre d'URL (« button » est un sélecteur).
_HTML_TAGS = {"a", "button", "li", "span", "div", "nav", "input", "ul", "i", "img"}
RE_ATTACH = re.compile(
    r"util_downloadFileByFullPath\(\s*[\"']([^\"']*)[\"']\s*,\s*[\"']([^\"']*)[\"']\s*,\s*[\"']([^\"']*)[\"']\s*\)"
)
RE_SEQNO = re.compile(r"fn_selectPtNoticeOpt\(\s*['\"](\d+)['\"]\s*\)")


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


# --------------------------------------------------------------------------- HTTP déterministe
def _parse_form(html: str, base_url: str) -> tuple[str | None, dict[str, str], object]:
    """Champs cachés + listes du formulaire JSP, et action absolue."""
    tree = HTMLParser(html)
    form = tree.css_first("form")
    if form is None:
        return None, {}, tree
    action = urljoin(base_url, form.attributes.get("action") or base_url)
    data: dict[str, str] = {}
    for inp in form.css("input[name]"):
        typ = (inp.attributes.get("type") or "text").lower()
        if typ in ("submit", "button", "image"):
            continue
        data[inp.attributes["name"]] = inp.attributes.get("value") or ""
    for sel in form.css("select[name]"):
        name = sel.attributes["name"]
        chosen = next((o for o in sel.css("option") if "selected" in o.attributes), None) or sel.css_first("option")
        data[name] = (chosen.attributes.get("value") if chosen else "") or ""
    return action, data, tree


def _with_path(action: str, path: str) -> str:
    parts = urlsplit(action)
    return urlunsplit(parts._replace(path=path))


def _list_rows(html: str) -> list[dict]:
    tree = HTMLParser(html)
    table = tree.css_first("table.data") or tree.css_first("table")
    if table is None:
        return []
    rows = []
    for tr in table.css("tr"):
        cells = [c.text(strip=True) for c in tr.css("td")]
        if len(cells) < 5:
            continue
        seq = RE_SEQNO.search(" ".join(a.attributes.get("onclick") or "" for a in tr.css("a[onclick]")))
        if not seq:
            continue
        rows.append({"seqno": seq.group(1), "categorie": cells[1], "objet": cells[2],
                     "date_redaction": cells[3], "consultations": cells[4]})
    return rows


def _detail_fields(html: str) -> dict:
    tree = HTMLParser(html)
    table = tree.css_first("table.data")
    out = {}
    if table is None:
        return out
    for tr in table.css("tr"):
        cells = tr.css("th, td")
        if len(cells) < 2 or cells[0].tag != "th":
            continue
        for i in range(0, len(cells) - 1, 2):
            if cells[i].tag != "th" or cells[i + 1].tag != "td":
                continue
            label = re.sub(r"[^a-z0-9]+", "_", fields.strip_accents(cells[i].text(strip=True)).lower()).strip("_")
            if label:
                out[label] = cells[i + 1].text(separator="\n", strip=True)
    return out


def _download_url(base_url: str, download_path: str, path: str, name: str) -> str:
    parts = urlsplit(base_url)
    root = f"{parts.scheme}://{parts.netloc}"
    return f"{root}{download_path}?fullFilePath={quote(path, safe='/')}&realFileName={quote(name)}"


def _attachments(html: str, base_url: str, download_path: str) -> list[dict]:
    tree = HTMLParser(html)
    out, seen = [], set()
    for a in tree.css("a[onclick]"):
        m = RE_ATTACH.search(a.attributes.get("onclick") or "")
        if not m:
            continue
        path, name = m.group(2), m.group(3)
        if path in seen:
            continue
        seen.add(path)
        out.append({"url": _download_url(base_url, download_path, path, name), "text": name})
    for inp in tree.css("input[name=deleteFilePath]"):
        path = inp.attributes.get("value") or ""
        if not path or path in seen:
            continue
        seen.add(path)
        name = Path(path).name
        out.append({"url": _download_url(base_url, download_path, path, name), "text": name})
    return out


def crawl_http(max_pages: int = 500, categories: list[str] | None = None) -> int:
    """Parcourt le portail COLEPS par ses formulaires JSP, sans navigateur.

    La recherche est un POST `selectListPagePtNoticeOpt.do` (catégorie, page, taille),
    chaque ligne porte un `seqno` ; le détail est un POST `selectPtNoticeOpt.do` et les
    pièces jointes passent par `co/file/downloadFile.do`. Les captures sont mises en
    cache, donc une relance reprend sans re-télécharger.
    """
    cfg = settings()["coleps"]
    page_size = int(cfg.get("page_size", 50))
    cats = categories or cfg.get("categories") or [""]
    n = 0
    with PoliteClient(SOURCE) as http:
        cap = http.fetch(cfg["notice_form_url"], refresh=True)
        if cap is None:
            log.warning("COLEPS : formulaire inaccessible")
            return 0
        action, base_data, _ = _parse_form(cap.text(), cap.final_url)
        if action is None:
            log.warning("COLEPS : formulaire absent de la page")
            return 0
        list_action = _with_path(action, cfg.get("list_path") or "/pt/notice/selectListPagePtNoticeOpt.do")
        detail_action = _with_path(action, cfg.get("detail_path") or "/pt/notice/selectPtNoticeOpt.do")
        download_path = cfg.get("download_path") or "/co/file/downloadFile.do"
        out = interim("coleps_items.jsonl")
        for cat in cats:
            page = 1
            while page <= max_pages:
                data = dict(base_data)
                data.update({"currentPageNo": str(page), "searchCategory": cat or "", "searchKeyword": "",
                             "searchCondition": "", "recordCountPerPage": str(page_size)})
                listing = http.fetch(list_action, method="POST", data=data,
                                     cache_key=f"coleps:list:{cat}:{page}",
                                     extra={"source": SOURCE, "category": cat, "page": page})
                if listing is None:
                    break
                rows = _list_rows(listing.text())
                if not rows:
                    break
                for row in rows:
                    detail_data = dict(base_data)
                    detail_data.update({"seqno": row["seqno"], "isBack": "Y", "currentPageNo": str(page),
                                        "searchCategory": cat or "", "searchKeyword": "",
                                        "searchCondition": "", "recordCountPerPage": str(page_size)})
                    detail_cap = http.fetch(detail_action, method="POST", data=detail_data,
                                            cache_key=f"coleps:detail:{row['seqno']}")
                    detail, docs, detail_url = {}, [], None
                    if detail_cap is not None:
                        detail_url = detail_cap.final_url
                        detail = _detail_fields(detail_cap.text())
                        docs = _attachments(detail_cap.text(), detail_url, download_path)
                    text = "\n".join(f"{k}: {v}" for k, v in detail.items() if v)
                    rec = {**row, "source": SOURCE, "category_code": cat, "page": page,
                           "detail_url": detail_url, "detail": detail, "documents": docs,
                           "text": text[:50000] or None, "reference": fields.reference(text) if text else None}
                    append_jsonl_unique(out, rec, key=lambda x: x["seqno"])
                    if docs:
                        queue_documents(SOURCE, detail_url or cfg["notice_form_url"], docs,
                                        {"kind": "coleps_notice", "seqno": row["seqno"]})
                    n += 1
                if len(rows) < page_size:
                    break
                page += 1
    log.info("COLEPS : %s annonces", n)
    return n


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
