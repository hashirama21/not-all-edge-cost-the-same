"""Agent browser-use sur llama.cpp : configuration, vérification du serveur, carte COLEPS."""

import json

import httpx
import pytest

from cmrmp.agents import browser_agent
from cmrmp.sources import coleps


def test_agent_settings_defaults_and_env(monkeypatch):
    cfg = browser_agent.agent_settings()
    assert cfg["backend"] == "llamacpp" and cfg["base_url"].endswith("/v1") and cfg["use_vision"] is False
    monkeypatch.setenv("CMRMP_AGENT_BACKEND", "anthropic")
    monkeypatch.setenv("CMRMP_AGENT_VISION", "true")
    cfg = browser_agent.agent_settings()
    assert cfg["backend"] == "anthropic" and cfg["use_vision"] is True
    monkeypatch.setenv("CMRMP_AGENT_BACKEND", "inconnu")
    with pytest.raises(ValueError):
        browser_agent.agent_settings()


def _server(content: str, health: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(health, json={"status": "ok"})
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(200, json={"model": "local", "choices": [{"message": {"content": content}}]})
    return httpx.MockTransport(handler)


def test_check_llm():
    res = browser_agent.check_llm("http://127.0.0.1:8080/v1", transport=_server('{"ok": true, "site": "test"}'))
    assert res["structured_output"] == {"ok": True, "site": "test"}
    with pytest.raises(RuntimeError):
        browser_agent.check_llm("http://127.0.0.1:8080/v1", transport=_server('{"ok": "oui"}'))
    with pytest.raises(RuntimeError):
        browser_agent.check_llm("http://127.0.0.1:8080/v1", transport=_server("{}", health=503))


def _listing(url, pagination="?page="):
    return {"description": "avis", "url": url, "pagination": pagination, "item_selector": "tr",
            "fields": [{"name": "reference", "css": "td", "example": "N°1"}]}


def test_load_sitemap_validates_and_filters(tmp_path):
    path = tmp_path / "coleps.json"
    path.write_text(json.dumps({"final_result": None, "errors": ["timeout"]}), encoding="utf-8")
    with pytest.raises(ValueError):
        coleps.load_sitemap(path)
    path.write_text(json.dumps({
        "site": "marchespublics.cm", "requires_login_for": [], "robots_or_terms_notes": "", "notes": "",
        "listings": [_listing("https://www.marchespublics.cm/avis"), _listing("https://evilmarchespublics.cm/x"),
                     _listing("https://exemple.org/avis")],
    }), encoding="utf-8")
    sm = coleps.load_sitemap(path)
    assert [l["url"] for l in sm["listings"]] == ["https://www.marchespublics.cm/avis"]


def test_pagination_helpers():
    assert coleps.page_param("?page=") == "page"
    assert coleps.page_param("&p=2") == "p"
    assert coleps.page_param("page") == "page"
    assert coleps.page_param("button") is None
    assert coleps.page_param("li.next > a") is None
    assert coleps.next_page_url("https://x.cm/avis?type=AO&page=1", "page", 3) == "https://x.cm/avis?type=AO&page=3"
