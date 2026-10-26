"""Interface en ligne de commande : cmrmp <étape>."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, help="Marchés publics camerounais : collecte -> extraction -> graphe -> Hugging Face")
crawl_app = typer.Typer(help="Collecte par source")
agent_app = typer.Typer(help="Agent navigateur (découverte de structure)")
hf_app = typer.Typer(help="Hugging Face (dataset + bucket)")
app.add_typer(crawl_app, name="crawl")
app.add_typer(agent_app, name="agent")
app.add_typer(hf_app, name="hf")


def _years(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    if "-" in spec:
        a, b = spec.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(y) for y in spec.split(",")]


@app.callback()
def main(verbose: bool = typer.Option(False, "-v", "--verbose")):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")


@app.command()
def discover(url: str):
    """Affiche la structure d'une page (motifs d'URL, blocs répétés, formulaires) pour régler les sélecteurs."""
    from .http import PoliteClient
    from .sources.common import describe_page

    with PoliteClient("discover") as http:
        cap = http.fetch(url, refresh=True)
    if cap is None:
        raise typer.Exit("page introuvable")
    typer.echo(json.dumps(describe_page(cap.text(), cap.final_url), ensure_ascii=False, indent=2))


@crawl_app.command("katika")
def crawl_katika(years: str = typer.Option(None, help="ex. 2018-2026 ou 2024,2025"),
                 max_pages: int = typer.Option(None), details: bool = True):
    from .sources import katika

    typer.echo(f"{katika.crawl(_years(years), max_pages, details)} items")


@crawl_app.command("armp-discover")
def crawl_armp_discover():
    from .sources import armp

    typer.echo(json.dumps(armp.discover_types(), indent=2))


@crawl_app.command("armp")
def crawl_armp(types: str = typer.Option(None, help="ex. DEC-ATTR,AAO"), start: int = None, end: int = None):
    from .sources import armp

    typer.echo(f"{armp.crawl_details(types.split(',') if types else None, start, end)} fiches")


@crawl_app.command("pridesoft")
def crawl_pridesoft(types: str = typer.Option(None, help="ex. AO"), start: int = None, end: int = None):
    from .sources import armp

    typer.echo(f"{armp.crawl_pridesoft(types.split(',') if types else None, start, end)} rapports")


@crawl_app.command("jdm")
def crawl_jdm(start: int = None, end: int = None):
    from .sources import armp

    typer.echo(f"{armp.crawl_jdm(start, end)} numéros")


@crawl_app.command("minmap")
def crawl_minmap():
    from .sources import minmap

    typer.echo(f"{minmap.crawl()} communiqués de sanction")


@crawl_app.command("coleps")
def crawl_coleps(sitemap: Path = typer.Option(None), max_pages: int = 500):
    from .sources import coleps

    typer.echo(f"{coleps.crawl(sitemap or coleps.default_sitemap_path(), max_pages)} items")


@crawl_app.command("documents")
def crawl_documents(limit: int = None):
    """Télécharge les documents mis en file par les autres collecteurs."""
    from .sources import documents

    typer.echo(json.dumps(documents.download_all(limit)))


@agent_app.command("explore")
def agent_explore(url: str = typer.Argument(None), out: Path = typer.Option(None), max_steps: int = 60):
    """Cartographie un site avec browser-use + Claude (COLEPS par défaut)."""
    from .agents import browser_agent
    from .config import settings
    from .sources.coleps import default_sitemap_path

    cfg = settings()["coleps"]
    url = url or cfg["start_urls"][0]
    res = browser_agent.run(url, cfg["allowed_domains"], out or default_sitemap_path(), max_steps)
    typer.echo(res.model_dump_json(indent=2) if res else "aucune carte produite : voir le fichier de sortie")


@app.command()
def extract(llm: bool = typer.Option(False, help="extraction structurée par Claude"), workers: int = 4):
    from .extract import pipeline

    df = pipeline.run(llm=llm, workers=workers)
    typer.echo(df["doc_type"].value_counts().to_string() if not df.empty else "aucun document")


@app.command()
def build():
    """Tables, résolution d'entités, graphe, signaux d'alerte, coûts."""
    from .build import graph, tables

    t = tables.build_all()
    t["aretes"] = graph.edges(t)
    t["signaux_alerte"] = graph.red_flags(t)
    t["couts_relations"], t["couts_marches"] = graph.relation_costs(t, t["aretes"])
    out = tables.write(t)
    typer.echo(f"tables écrites dans {out}")


@app.command()
def audit():
    """Audit de couverture : années, maîtres d'ouvrage, part des marchés avec toutes les offres."""
    import pandas as pd

    from .config import data_dir

    p = data_dir("processed")
    report = {}
    for name in ("avis", "soumissions", "attributions", "sanctions", "entreprises", "documents"):
        f = p / f"{name}.parquet"
        report[name] = len(pd.read_parquet(f)) if f.exists() else 0
    if (p / "avis.parquet").exists():
        avis = pd.read_parquet(p / "avis.parquet")
        year = pd.to_datetime(avis["date_publication"], errors="coerce").dt.year
        report["avis_par_annee"] = year.value_counts().sort_index().to_dict()
        report["top_maitres_ouvrage"] = avis["maitre_ouvrage"].value_counts().head(15).to_dict()
        if (p / "soumissions.parquet").exists():
            s = pd.read_parquet(p / "soumissions.parquet")
            refs_pv = set(s["reference"].dropna())
            report["part_avis_avec_pv"] = round(avis["reference"].isin(refs_pv).mean(), 3)
            report["part_offres_avec_montant"] = round(s["montant_offre_fcfa"].notna().mean(), 3)
    if (p / "documents.parquet").exists():
        d = pd.read_parquet(p / "documents.parquet")
        report["documents_par_type"] = d["doc_type"].value_counts().to_dict()
        report["part_pdf_ocr"] = round(d["ocr"].mean(), 3) if "ocr" in d else None
    out = data_dir("reports") / "audit.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))


@hf_app.command("publish")
def hf_publish(repo: str = typer.Option(..., help="utilisateur/nom-du-dataset"),
               bucket: str = typer.Option(None, help="utilisateur/nom-du-bucket"),
               public: bool = typer.Option(False, help="publier en public (après relecture)")):
    from .publish import hf

    hf.publish(repo, bucket, private=not public)
    typer.echo(f"publié : https://huggingface.co/datasets/{repo}")


@hf_app.command("pull")
def hf_pull(bucket: str = typer.Option(...), path: list[str] = typer.Option(None, help="ex. raw/katika (répétable)")):
    """Récupère le cache depuis le bucket (avant une collecte incrémentale)."""
    from .publish import hf

    hf.sync_raw(bucket, direction="down", paths=path or None)


@hf_app.command("push")
def hf_push(bucket: str = typer.Option(...), path: list[str] = typer.Option(None, help="ex. raw/katika (répétable)")):
    """Envoie le cache local vers le bucket."""
    from .publish import hf

    hf.sync_raw(bucket, direction="up", paths=path or None)


if __name__ == "__main__":
    app()
