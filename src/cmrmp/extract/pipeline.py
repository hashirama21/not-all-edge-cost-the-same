"""Étape « extract » : texte + champs regex pour chaque document, puis LLM optionnel."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from ..config import data_dir
from ..sources.common import interim
from ..storage import iter_manifest
from . import documents, fields

log = logging.getLogger(__name__)

DOC_SUFFIXES = {".pdf", ".docx", ".doc", ".html", ".htm"}


def _text_path(sha: str) -> Path:
    return data_dir("text", sha[:2]) / f"{sha}.txt"


def _one(cap) -> dict | None:
    path = Path(cap.path)
    if path.suffix.lower() not in DOC_SUFFIXES or not path.exists():
        return None
    if not cap.source.startswith("docs/") and path.suffix == ".html":
        return None  # pages de liste/fiche : déjà traitées par les collecteurs
    tp = _text_path(cap.content_sha256)
    mp = tp.with_suffix(".meta.json")
    if tp.exists() and mp.exists():
        text, meta = tp.read_text(encoding="utf-8"), json.loads(mp.read_text(encoding="utf-8"))
    else:
        text, meta = documents.document_text(path, cap.content_type)
        tp.write_text(text, encoding="utf-8")
        mp.write_text(json.dumps(meta, ensure_ascii=False, default=str), encoding="utf-8")
    title = cap.extra.get("title", "") or Path(cap.url.split("?")[0]).name
    kind = cap.extra.get("kind") or fields.classify(title, text)
    row = {
        "doc_id": cap.content_sha256, "source": cap.source, "url": cap.url, "referer": cap.extra.get("referer"),
        "title": title, "doc_type": kind, "content_type": cap.content_type, "bytes": cap.bytes,
        "fetched_at": cap.fetched_at, "raw_path": cap.path, "text_path": str(tp), "n_chars": len(text),
        "meta_path": str(mp), "pages": meta.get("pages"), "ocr": meta.get("ocr", False),
        "ocr_pages": meta.get("ocr_pages"), "n_tables": len(meta.get("tables") or []),
        "pdf_creation": (meta.get("pdf_metadata") or {}).get("creationDate"),
        "pdf_producer": (meta.get("pdf_metadata") or {}).get("producer"),
        "reference_hint": cap.extra.get("reference"),
    }
    if kind in ("avis", "dao", "additif"):
        row.update({f"rx_{k}": v for k, v in fields.avis_fields(text).items()})
    else:
        row["rx_reference"] = fields.reference(text)
    row["rx_niu"] = fields.niu_list(text)
    row["rx_rccm"] = fields.rccm_list(text)
    if kind in ("pv_ouverture", "pv_jugement"):
        row["tab_soumissions"] = fields.bids_from_tables(meta.get("tables") or [])
    return row


def run(llm: bool = False, workers: int = 4, llm_types: tuple[str, ...] = ("avis", "dao", "pv_ouverture", "pv_jugement", "decision_attribution", "sanction")) -> pd.DataFrame:
    caps = [c for c in iter_manifest() if c.status == 200]
    with ThreadPoolExecutor(workers) as pool:
        rows = [r for r in pool.map(_one, caps) if r]
    if not rows:
        log.warning("aucun document à extraire")
        return pd.DataFrame(columns=["doc_id", "doc_type"])
    df = pd.DataFrame(rows).drop_duplicates("doc_id")
    for col in [c for c in df.columns if c.startswith("rx_date")]:
        df[col] = df[col].astype("string")
    bids = [{"doc_id": r["doc_id"], "reference": r.get("rx_reference"), **b}
            for r in rows for b in (r.get("tab_soumissions") or [])]
    pd.DataFrame(bids).to_parquet(interim("extract_table_bids.parquet"), index=False)
    df = df.drop(columns=["tab_soumissions"], errors="ignore")
    out = interim("extract_documents.parquet")
    df.to_parquet(out, index=False)
    log.info("%s documents -> %s", len(df), out)

    if llm:
        from . import llm as llm_mod

        dest = interim("extract_llm.jsonl")
        todo = df[df["doc_type"].isin(llm_types)]

        def call(row):
            text = Path(row.text_path).read_text(encoding="utf-8")
            meta = json.loads(Path(row.meta_path).read_text(encoding="utf-8"))
            text = documents.pdf_markdown(text, meta)
            pdf = Path(row.raw_path) if row.raw_path.endswith(".pdf") else None
            return row.doc_id, llm_mod.extract(row.doc_id, row.doc_type, text, pdf)

        with ThreadPoolExecutor(max(1, workers // 2)) as pool, dest.open("w", encoding="utf-8") as fh:
            for doc_id, res in pool.map(call, todo.itertuples()):
                if res:
                    fh.write(json.dumps({"doc_id": doc_id, **res}, ensure_ascii=False) + "\n")
    return df
