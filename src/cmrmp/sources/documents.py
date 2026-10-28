"""Téléchargement de la file de documents (PDF, DOCX…) avec déduplication par URL.

Les liens PRIDESOFT (« …_dl?type_publication=… ») renvoient une page HTML qui affiche le
fichier dans une iframe : on suit l'iframe. Une iframe vers un dossier (« …/C_BERTOUA_I/ »)
signifie qu'aucun document n'est publié.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from urllib.parse import urljoin, urlsplit

from ..config import data_dir
from ..http import PoliteClient, RobotsDisallowed
from ..storage import Capture, read_jsonl
from .common import DOC_RE

log = logging.getLogger(__name__)

# Le nom du fichier peut contenir une apostrophe (« Décision d'attribution.pdf ») : on lit
# jusqu'au même guillemet que celui qui ouvre l'attribut.
_FRAME_SRC = re.compile(r"<(?:iframe|embed|object)\b[^>]*?\b(?:src|data)\s*=\s*([\"'])(.+?)\1", re.IGNORECASE)


def framed_document(cap: Capture) -> str | None:
    """URL du fichier affiché dans l'iframe d'une page enveloppe, None s'il n'y en a pas."""
    if "html" not in cap.content_type or cap.bytes > 20_000:
        return None
    m = _FRAME_SRC.search(cap.text())
    if not m:
        return None
    url = urljoin(cap.final_url, m.group(2).strip())
    return url if DOC_RE.search(url) and not url.endswith("/") else None


def pending() -> list[dict]:
    seen, out = set(), []
    for path in sorted(data_dir("interim").glob("*/*_documents_queue.jsonl")):
        for rec in read_jsonl(path):
            if rec.get("url") and rec["url"] not in seen:
                seen.add(rec["url"])
                out.append(rec)
    return out


def download_all(limit: int | None = None) -> dict[str, int]:
    """Un client par hôte : le délai de politesse s'applique hôte par hôte."""
    by_host: dict[str, list[dict]] = defaultdict(list)
    for rec in pending()[:limit]:
        by_host[urlsplit(rec["url"]).netloc].append(rec)
    stats = {"ok": 0, "missing": 0, "disallowed": 0, "error": 0, "sans_fichier": 0}
    for host, recs in by_host.items():
        with PoliteClient(f"docs/{host}") as http:
            for rec in recs:
                try:
                    extra = {k: v for k, v in rec.items() if k != "url"}
                    cap = http.fetch(rec["url"], extra=extra)
                    if cap and "html" in cap.content_type and cap.bytes <= 20_000:
                        target = framed_document(cap)
                        if target is None:  # page enveloppe sans fichier publié
                            stats["sans_fichier"] += 1
                            continue
                        cap = http.fetch(target, extra={**extra, "via": rec["url"]})
                    stats["ok" if cap else "missing"] += 1
                except RobotsDisallowed:
                    stats["disallowed"] += 1
                except Exception as exc:  # on journalise et on continue
                    log.warning("échec %s : %s", rec["url"], exc)
                    stats["error"] += 1
    return stats
