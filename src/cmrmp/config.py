"""Chargement de la configuration (YAML + variables d'environnement)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@lru_cache
def settings() -> dict:
    _load_dotenv(ROOT / ".env")
    cfg_path = Path(os.environ.get("CMRMP_CONFIG", ROOT / "config" / "sources.yaml"))
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["data_dir"] = Path(os.environ.get("CMRMP_DATA_DIR", ROOT / "data")).resolve()
    cfg["contact"] = os.environ.get("CMRMP_CONTACT", "")
    return cfg


def data_dir(*parts: str) -> Path:
    path = settings()["data_dir"].joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
