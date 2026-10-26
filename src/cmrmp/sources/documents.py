"""Téléchargement de la file de documents (PDF, DOCX…) avec déduplication par URL."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from urllib.parse import urlsplit

from ..http import PoliteClient, RobotsDisallowed
from ..config import data_dir

log = logging.getLogger(__name__)


def pending() -> list[dict]:
    seen, out = set(), []
    for path in sorted(data_dir("interim").glob("*/*_documents_queue.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec["url"] not in seen:
                    seen.add(rec["url"])
                    out.append(rec)
    return out


def download_all(limit: int | None = None) -> dict[str, int]:
    """Un client par hôte : le délai de politesse s'applique hôte par hôte."""
    by_host: dict[str, list[dict]] = defaultdict(list)
    for rec in pending()[:limit]:
        by_host[urlsplit(rec["url"]).netloc].append(rec)
    stats = {"ok": 0, "missing": 0, "disallowed": 0, "error": 0}
    for host, recs in by_host.items():
        with PoliteClient(f"docs/{host}") as http:
            for rec in recs:
                try:
                    cap = http.fetch(rec["url"], extra={k: v for k, v in rec.items() if k != "url"})
                    stats["ok" if cap else "missing"] += 1
                except RobotsDisallowed:
                    stats["disallowed"] += 1
                except Exception as exc:  # on journalise et on continue
                    log.warning("échec %s : %s", rec["url"], exc)
                    stats["error"] += 1
    return stats
