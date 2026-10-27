"""Stockage brut immuable : chaque réponse HTTP est gardée telle quelle avec sa provenance.

Arborescence :
    data/raw/<source>/<h[:2]>/<h>.<c>.<ext>   corps de la réponse (h = sha256 de l'URL, c = début du
                                              sha256 du corps : un rafraîchissement n'écrase rien)
    data/raw/<source>/<h[:2]>/<h>.meta.json   dernière capture : URL, URL finale, statut, en-têtes, date
    data/raw/<source>/manifest.jsonl          journal append-only des captures de la source

Les chemins sont écrits relativement à data/ et résolus à la lecture : le cache reste valable
quand data/ change de place (autre runner, `hf pull` en local). Les anciens chemins absolus
sont ré-enracinés sur le data/ courant.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir, settings
from .time import utc_now_iso

log = logging.getLogger(__name__)

_lock = threading.Lock()

_EXT_BY_TYPE = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/json": ".json",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/csv": ".csv",
}


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def guess_ext(url: str, content_type: str, disposition: str = "") -> str:
    """Extension : nom de fichier de Content-Disposition, puis type MIME, puis URL.

    Un export ASP.NET arrive souvent sur une URL en .aspx : l'URL vient donc en dernier."""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition or "", re.I)
    if m and Path(m.group(1)).suffix:
        return Path(m.group(1)).suffix.lower()
    ctype = content_type.split(";")[0].strip().lower()
    if ctype in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ctype]
    if ctype and ctype not in ("application/octet-stream", "text/plain", "application/force-download"):
        ext = mimetypes.guess_extension(ctype)
        if ext:
            return ext
    path_ext = Path(url.split("?")[0]).suffix.lower()
    if path_ext and len(path_ext) <= 5:
        return path_ext
    return mimetypes.guess_extension(ctype) or ".bin"


# --------------------------------------------------------------------------- chemins portables
def relative_path(path: str | Path) -> str:
    """Chemin relatif à data/ (avec des /) ; inchangé s'il est hors de data/."""
    p = Path(path)
    try:
        return p.resolve().relative_to(settings()["data_dir"]).as_posix()
    except ValueError:
        return str(path)


def resolve_path(path: str | Path) -> str:
    """Chemin absolu sur le data/ courant, pour un chemin relatif ou un ancien chemin absolu."""
    root = settings()["data_dir"]
    p = Path(path)
    # « /home/runner/… » n'est pas absolu pour Windows (pas de lecteur) : c'est quand même
    # un ancien chemin absolu d'un autre système.
    if not p.is_absolute() and not str(path).startswith(("/", "\\")):
        return str(root / p)
    if p.exists():
        return str(p)
    parts = p.parts
    for anchor in ("raw", "interim", "text", "dgi"):  # ancien chemin d'un autre runner
        if anchor in parts:
            i = len(parts) - 1 - parts[::-1].index(anchor)
            candidate = root.joinpath(*parts[i:])
            if candidate.exists():
                return str(candidate)
    return str(p)


@dataclass
class Capture:
    source: str
    url: str
    final_url: str
    status: int
    content_type: str
    content_sha256: str
    bytes: int
    fetched_at: str
    path: str
    etag: str | None = None
    last_modified: str | None = None
    extra: dict = field(default_factory=dict)
    fetched_at_utc: str | None = None
    url_hash: str | None = None
    content_sha256_body: str | None = None

    @classmethod
    def from_record(cls, rec: dict) -> "Capture":
        known = {k: v for k, v in rec.items() if k in cls.__dataclass_fields__}
        cap = cls(**known)
        cap.path = resolve_path(cap.path)
        return cap

    def record(self) -> dict:
        rec = asdict(self)
        rec["path"] = relative_path(self.path)
        return rec

    def age_seconds(self) -> float | None:
        try:
            ts = datetime.fromisoformat(self.fetched_at_utc or self.fetched_at)
        except (TypeError, ValueError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def body(self) -> bytes:
        return Path(self.path).read_bytes()

    def text(self) -> str:
        raw = self.body()
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")


class RawStore:
    def __init__(self, source: str):
        self.source = source
        self.root = data_dir("raw", source)
        self.manifest = self.root / "manifest.jsonl"

    def _base(self, url: str, create: bool = False) -> Path:
        h = url_hash(url)
        folder = self.root / h[:2]
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return folder / h

    def get(self, url: str) -> Capture | None:
        meta = self._base(url).with_suffix(".meta.json")
        if not meta.exists():
            return None
        try:
            return Capture.from_record(json.loads(meta.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("métadonnées illisibles %s : %s", meta, exc)
            return None

    def put(self, url: str, final_url: str, status: int, headers: dict, body: bytes, extra: dict | None = None) -> Capture:
        content_type = headers.get("content-type", "")
        base = self._base(url, create=True)
        body_sha = hashlib.sha256(body).hexdigest()
        ext = guess_ext(final_url, content_type, headers.get("content-disposition", ""))
        path = base.parent / f"{base.name}.{body_sha[:12]}{ext}"
        if not path.exists():
            path.write_bytes(body)
        now = utc_now_iso("seconds")
        cap = Capture(
            source=self.source,
            url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            content_sha256=body_sha,
            bytes=len(body),
            fetched_at=now,
            fetched_at_utc=now,
            path=str(path),
            etag=headers.get("etag"),
            last_modified=headers.get("last-modified"),
            extra=extra or {},
            url_hash=url_hash(url),
            content_sha256_body=body_sha,
        )
        record = json.dumps(cap.record(), ensure_ascii=False)
        base.with_suffix(".meta.json").write_text(record, encoding="utf-8")
        with _lock, self.manifest.open("a", encoding="utf-8") as fh:
            fh.write(record + "\n")
        return cap


def read_jsonl(path: Path) -> list[dict]:
    """Lignes JSON d'un fichier ; une ligne tronquée (tâche interrompue) est ignorée."""
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("ligne JSON illisible ignorée : %s:%s", path, n)
    return out


def iter_manifest(source: str | None = None):
    """Dernière capture connue par URL (le manifeste est append-only)."""
    pattern = f"{source}/manifest.jsonl" if source else "**/manifest.jsonl"
    latest: dict[str, dict] = {}
    for manifest in sorted(data_dir("raw").glob(pattern)):
        for rec in read_jsonl(manifest):
            latest[rec["url"]] = rec
    for rec in latest.values():
        yield Capture.from_record(rec)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


_seen_keys: dict[str, set[str]] = {}


def _key_str(key) -> str:
    return json.dumps(key, ensure_ascii=False, default=str, sort_keys=True)


def append_jsonl_unique(path: Path, record: dict, key) -> bool:
    """Ajoute `record` sauf si une ligne de même clé existe déjà (relance d'une collecte).

    `key` est une fonction record -> valeur sérialisable. L'index des clés présentes est lu
    une fois par fichier. Renvoie True si la ligne a été écrite."""
    k = _key_str(key(record))
    p = str(Path(path).resolve())
    with _lock:
        if p not in _seen_keys:
            keys = set()
            for rec in read_jsonl(Path(path)):
                try:
                    keys.add(_key_str(key(rec)))
                except (KeyError, TypeError):
                    continue
            _seen_keys[p] = keys
        if k in _seen_keys[p]:
            return False
        _seen_keys[p].add(k)
    append_jsonl(path, record)
    return True


def reset_jsonl_index() -> None:
    """Oublie les index de clés (tests, changement de data/)."""
    with _lock:
        _seen_keys.clear()
