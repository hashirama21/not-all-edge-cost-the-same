"""Stockage brut immuable : chaque réponse HTTP est gardée telle quelle avec sa provenance.

Arborescence :
    data/raw/<source>/<h[:2]>/<h>.<ext>      corps de la réponse (h = sha256 de l'URL)
    data/raw/<source>/<h[:2]>/<h>.meta.json  URL, URL finale, statut, en-têtes, date de capture
    data/raw/<source>/manifest.jsonl         journal append-only des captures de la source
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir

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

    def _base(self, url: str) -> Path:
        h = url_hash(url)
        folder = self.root / h[:2]
        folder.mkdir(parents=True, exist_ok=True)
        return folder / h

    def get(self, url: str) -> Capture | None:
        meta = self._base(url).with_suffix(".meta.json")
        if not meta.exists():
            return None
        return Capture(**json.loads(meta.read_text(encoding="utf-8")))

    def put(self, url: str, final_url: str, status: int, headers: dict, body: bytes, extra: dict | None = None) -> Capture:
        content_type = headers.get("content-type", "")
        base = self._base(url)
        path = base.with_suffix(guess_ext(final_url, content_type, headers.get("content-disposition", "")))
        path.write_bytes(body)
        cap = Capture(
            source=self.source,
            url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            content_sha256=hashlib.sha256(body).hexdigest(),
            bytes=len(body),
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            path=str(path),
            etag=headers.get("etag"),
            last_modified=headers.get("last-modified"),
            extra=extra or {},
        )
        record = json.dumps(asdict(cap), ensure_ascii=False)
        base.with_suffix(".meta.json").write_text(record, encoding="utf-8")
        with _lock, self.manifest.open("a", encoding="utf-8") as fh:
            fh.write(record + "\n")
        return cap


def iter_manifest(source: str | None = None):
    """Dernière capture connue par URL (le manifeste est append-only)."""
    pattern = f"{source}/manifest.jsonl" if source else "**/manifest.jsonl"
    latest: dict[str, dict] = {}
    for manifest in sorted(data_dir("raw").glob(pattern)):
        with manifest.open(encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                latest[rec["url"]] = rec
    for rec in latest.values():
        yield Capture(**rec)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
