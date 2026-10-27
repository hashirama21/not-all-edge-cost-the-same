"""Client HTTP poli : robots.txt, délai par hôte, reprises exponentielles, cache disque.

Règles appliquées :
- un User-Agent identifiable avec un contact (CMRMP_CONTACT) ;
- robots.txt respecté (Crawl-delay compris) ;
- au plus une requête toutes les `min_delay_s` secondes par hôte, avec gigue ;
- reprise sur 429/5xx et erreurs réseau, en respectant Retry-After ;
- chaque réponse est archivée ; une URL déjà capturée n'est pas re-téléchargée
  (sauf `refresh=True` ou capture plus vieille que `max_age_s`, qui envoient
  If-None-Match / If-Modified-Since).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
from .time import utc_now_iso

log = logging.getLogger(__name__)


class RetryableStatus(Exception):
    def __init__(self, status: int, retry_after: float | None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class RobotsDisallowed(Exception):
    pass


class HostUnreachable(Exception):
    """L'hôte ne répond plus (erreurs réseau répétées après reprises) : on arrête la source."""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (RetryableStatus, httpx.TransportError))


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After en secondes ou en date HTTP (RFC 9110)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class PoliteClient:
    def __init__(self, source: str, *, min_delay_s: float | None = None, jitter_s: float | None = None,
                 transport: httpx.BaseTransport | None = None):
        cfg = settings()["politeness"]
        contact = settings()["contact"] or "contact-non-renseigne"
        self.source = source
        self.store = RawStore(source)
        self.min_delay = min_delay_s if min_delay_s is not None else cfg["min_delay_s"]
        self.jitter = jitter_s if jitter_s is not None else cfg["jitter_s"]
        self.respect_robots = cfg["respect_robots"]
        self.max_retries = cfg["max_retries"]
        self.user_agent = f"cmrmp/{__version__} (academic research; +mailto:{contact})"
        self.client = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept-Language": "fr,en;q=0.8"},
            timeout=cfg["timeout_s"],
            follow_redirects=True,
            transport=transport,
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()
        self._consecutive_failures: dict[str, int] = {}
        self.max_consecutive_failures = 5
        # Pauses avant de redemander robots.txt après un 5xx ou une erreur réseau.
        self.robots_backoff_s: list[float] = [5.0, 30.0]

    # -- robots.txt ---------------------------------------------------------
    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            rp = RobotFileParser()
            # RFC 9309 : 4xx = pas de restriction ; 5xx ou injoignable = tout interdit,
            # mais seulement après quelques reprises (une panne passagère ne bloque pas la source).
            outcome = "unreachable"
            for attempt, pause in enumerate([0.0, *self.robots_backoff_s]):
                if pause:
                    time.sleep(pause)
                try:
                    self._wait_turn(f"{host}/robots.txt")
                    resp = self.client.get(f"{host}/robots.txt")
                except httpx.HTTPError as exc:
                    log.warning("robots.txt %s injoignable (essai %s) : %s", host, attempt + 1, exc)
                    continue
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                    outcome = "parsed"
                    break
                if resp.status_code < 500:
                    outcome = "none"
                    break
                log.warning("robots.txt %s : HTTP %s (essai %s)", host, resp.status_code, attempt + 1)
            if outcome == "none":
                rp = None
            elif outcome == "unreachable":
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
              method: str = "GET", data: dict | None = None, cache_key: str | None = None,
              max_age_s: float | None = None) -> Capture | None:
        """Renvoie la capture (depuis le cache si présente). None si 404/410.

        Un POST n'est mis en cache que si `cache_key` est fourni (ex. URL + paramètres du
        formulaire hors __VIEWSTATE), ce qui rend reprenable un parcours de formulaires.
        `max_age_s` : une capture plus ancienne est revalidée (pages de liste qui bougent).
        """
        key = cache_key or url
        cached = self.store.get(key) if (method == "GET" or cache_key) else None
        if cached and max_age_s is not None:
            age = cached.age_seconds()
            refresh = refresh or age is None or age > max_age_s
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
            wait=wait_exponential_jitter(multiplier=5, max=300),
            reraise=True,
        )
        def _do() -> httpx.Response:
            self._wait_turn(url)
            resp = self.client.request(method, url, headers=headers, data=data)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = parse_retry_after(resp.headers.get("retry-after"))
                if wait:
                    log.warning("Retry-After %ss sur %s", wait, url)
                    time.sleep(min(wait, 900))
                raise RetryableStatus(resp.status_code, wait)
            return resp

        host = urlsplit(url).netloc
        try:
            resp = _do()
        except (httpx.TransportError, RetryableStatus) as exc:
            n = self._consecutive_failures.get(host, 0) + 1
            self._consecutive_failures[host] = n
            log.error("échec définitif %s : %s", url, exc)
            if n >= self.max_consecutive_failures:
                raise HostUnreachable(f"{host} injoignable ({n} échecs de suite, dernier : {exc})") from exc
            return None
        self._consecutive_failures[host] = 0
        if resp.status_code == 304 and cached:
            return cached
        if resp.status_code in (404, 410):
            return None
        if resp.status_code >= 400:
            log.warning("HTTP %s sur %s", resp.status_code, url)
            return None
        extra_full = dict(extra or {})
        extra_full.setdefault("request_at_utc", utc_now_iso("seconds"))
        extra_full.setdefault("host", host)
        return self.store.put(key, str(resp.url), resp.status_code, dict(resp.headers), resp.content, extra_full)

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
