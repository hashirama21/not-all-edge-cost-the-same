"""Client HTTP poli : robots.txt, délai par hôte, reprises exponentielles, cache disque.

Règles appliquées :
- un User-Agent identifiable avec un contact (CMRMP_CONTACT) ;
- robots.txt respecté (Crawl-delay compris) ;
- au plus une requête toutes les `min_delay_s` secondes par hôte, avec gigue ;
- reprise sur 429/5xx et erreurs réseau, en respectant Retry-After ;
- chaque réponse est archivée ; une URL déjà capturée n'est pas re-téléchargée
  (sauf `refresh=True`, qui envoie If-None-Match / If-Modified-Since).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from . import __version__
from .config import settings
from .storage import Capture, RawStore

log = logging.getLogger(__name__)


class RetryableStatus(Exception):
    def __init__(self, status: int, retry_after: float | None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class RobotsDisallowed(Exception):
    pass


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (RetryableStatus, httpx.TransportError))


class PoliteClient:
    def __init__(self, source: str, *, min_delay_s: float | None = None):
        cfg = settings()["politeness"]
        contact = settings()["contact"] or "contact-non-renseigne"
        self.source = source
        self.store = RawStore(source)
        self.min_delay = min_delay_s if min_delay_s is not None else cfg["min_delay_s"]
        self.jitter = cfg["jitter_s"]
        self.respect_robots = cfg["respect_robots"]
        self.max_retries = cfg["max_retries"]
        self.user_agent = f"cmrmp/{__version__} (recherche académique; +mailto:{contact})"
        self.client = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept-Language": "fr,en;q=0.8"},
            timeout=cfg["timeout_s"],
            follow_redirects=True,
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- robots.txt ---------------------------------------------------------
    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            rp = RobotFileParser()
            try:
                resp = self.client.get(f"{host}/robots.txt")
                # RFC 9309 : 4xx = pas de restriction ; 5xx ou injoignable = tout interdit.
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                elif resp.status_code >= 500:
                    rp.parse(["User-agent: *", "Disallow: /"])
                else:
                    rp = None
            except httpx.HTTPError:
                rp.parse(["User-agent: *", "Disallow: /"])
            self._robots[host] = rp
        return self._robots[host]

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        return rp is None or rp.can_fetch(self.user_agent, url)

    # -- cadence ------------------------------------------------------------
    def _wait_turn(self, url: str) -> None:
        host = urlsplit(url).netloc
        delay = self.min_delay
        rp = self._robots.get(f"{urlsplit(url).scheme}://{host}")
        if rp is not None:
            crawl_delay = rp.crawl_delay(self.user_agent)
            if crawl_delay:
                delay = max(delay, float(crawl_delay))
        with self._lock:
            elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
            pause = delay + random.uniform(0, self.jitter) - elapsed
            if pause > 0:
                time.sleep(pause)
            self._last_hit[host] = time.monotonic()

    # -- requêtes -----------------------------------------------------------
    def fetch(self, url: str, *, refresh: bool = False, extra: dict | None = None,
              method: str = "GET", data: dict | None = None) -> Capture | None:
        """Renvoie la capture (depuis le cache si présente). None si 404/410."""
        cached = self.store.get(url) if method == "GET" else None
        if cached and not refresh:
            return cached
        if not self.allowed(url):
            raise RobotsDisallowed(url)

        headers = {}
        if cached:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified

        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential_jitter(initial=5, max=300),
            reraise=True,
        )
        def _do() -> httpx.Response:
            self._wait_turn(url)
            resp = self.client.request(method, url, headers=headers, data=data)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else None
                if wait:
                    log.warning("Retry-After %ss sur %s", wait, url)
                    time.sleep(min(wait, 900))
                raise RetryableStatus(resp.status_code, wait)
            return resp

        resp = _do()
        if resp.status_code == 304 and cached:
            return cached
        if resp.status_code in (404, 410):
            return None
        if resp.status_code >= 400:
            log.warning("HTTP %s sur %s", resp.status_code, url)
            return None
        return self.store.put(url, str(resp.url), resp.status_code, dict(resp.headers), resp.content, extra)

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
