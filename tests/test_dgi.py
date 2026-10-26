"""Parcours d'une consultation ASP.NET simulée (aucun accès réseau)."""

from urllib.parse import parse_qs

import httpx
import pandas as pd

from cmrmp.sources import dgi

URL = "http://fiscalis.dgi.cm/modules/immatriculation/Consultation/listecontribuable.aspx"


def _page(rows, pager="", vs="VS1"):
    trs = "".join(f"<tr><td>{n}</td><td>{r}</td><td>{c}</td><td>{g}</td></tr>" for n, r, c, g in rows)
    return f"""<html><body><form method="post" action="./listecontribuable.aspx" id="form1">
<input type="hidden" name="__VIEWSTATE" value="{vs}"/><input type="hidden" name="__EVENTVALIDATION" value="EV"/>
<input type="hidden" name="__EVENTTARGET" value=""/><input type="hidden" name="__EVENTARGUMENT" value=""/>
<select name="ctl00$ddlCentre"><option value="">-- Tous --</option><option value="CIME_YDE">CIME Yaoundé</option></select>
<input type="text" name="ctl00$txtRaisonSociale" value=""/>
<input type="submit" name="ctl00$btnRechercher" value="Rechercher"/>
<input type="submit" name="ctl00$btnExportExcel" value="Exporter"/>
<table id="gv"><tr><th>NIU</th><th>Raison sociale</th><th>Centre de rattachement</th><th>Régime</th></tr>{trs}
<tr><td colspan="4">{pager}</td></tr></table></form></body></html>"""


PAGE1 = [("M011500012345A", "ETS ALPHA SARL", "CIME YAOUNDE", "REEL")]
PAGE2 = [("P058712345678X", "JEAN DUPONT", "CDI YAOUNDE 1", "LIBERATOIRE")]
PAGER = "<a href=\"javascript:__doPostBack('ctl00$gv','Page$2')\">2</a>"


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    if request.method == "GET":
        return httpx.Response(200, html=_page([]))
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    if "ctl00$btnExportExcel" in form:
        csv = "NIU;Raison sociale;Activité\nM099900011111B;GAMMA SERVICES;BTP\n"
        return httpx.Response(200, content=csv.encode(), headers={"content-type": "text/csv"})
    if form.get("__EVENTARGUMENT") == "Page$2":
        return httpx.Response(200, html=_page(PAGE2, vs="VS3"))
    if form.get("ctl00$ddlCentre") == "CIME_YDE" or form.get("ctl00$txtRaisonSociale") == "E":
        return httpx.Response(200, html=_page(PAGE1, PAGER, vs="VS2"))
    return httpx.Response(200, html=_page([]))


def test_parse_form_and_pager():
    f = dgi.parse_form(_page(PAGE1, PAGER), URL)
    assert f.fields["__VIEWSTATE"] == "VS1"
    assert f.selects["ctl00$ddlCentre"][1] == ("CIME_YDE", "CIME Yaoundé")
    assert "ctl00$txtRaisonSociale" in f.texts
    assert dgi.pager_targets(f) == {"2": ("ctl00$gv", "Page$2")}
    assert f.action == "http://fiscalis.dgi.cm/modules/immatriculation/Consultation/listecontribuable.aspx"


def test_crawl_unify_and_pseudonymize(isolated_data):
    n = dgi.crawl_consultation([URL], min_delay_s=0, jitter_s=0, transport=httpx.MockTransport(handler))
    assert n >= 2
    df = dgi.unify()
    assert set(df["niu"]) == {"M011500012345A", "P058712345678X", "M099900011111B"}
    alpha = df.set_index("niu").loc["M011500012345A"]
    assert alpha["raison_sociale"] == "ETS ALPHA SARL" and alpha["regime"] == "REEL"
    assert df.set_index("niu").loc["M099900011111B", "activite"] == "BTP"

    paths = dgi.write_unified(df, salt="sel")
    pub = pd.read_parquet(paths["parquet"])
    assert "JEAN DUPONT" not in set(pub["raison_sociale"])
    assert pub.loc[pub["type_personne"] == "physique", "niu"].str.startswith("h_").all()
    assert "ETS ALPHA SARL" in set(pub["raison_sociale"])
    assert len(pd.read_csv(paths["csv"])) == 3

    # Reprise : tout vient du cache, aucune requête de plus.
    calls = []
    def counting(req):
        calls.append(req.url.path)
        return handler(req)
    dgi.crawl_consultation([URL], min_delay_s=0, jitter_s=0, transport=httpx.MockTransport(counting))
    assert calls == []
