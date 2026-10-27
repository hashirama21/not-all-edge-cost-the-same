import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CMRMP_DATA_DIR", str(tmp_path / "data"))
    from cmrmp import config, storage

    config.settings.cache_clear()
    storage.reset_jsonl_index()
    yield tmp_path / "data"
    config.settings.cache_clear()
    storage.reset_jsonl_index()
