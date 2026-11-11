"""Interface en ligne de commande : cmrmp <étape>."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import typer

from .time import utc_now_iso

app = typer.Typer(add_completion=False, help="Marchés publics camerounais : collecte -> extraction -> graphe -> Hugging Face")
crawl_app = typer.Typer(help="Collecte par source")
agent_app = typer.Typer(help="Agent navigateur (découverte de structure)")
hf_app = typer.Typer(help="Hugging Face (dataset + bucket)")
dgi_app = typer.Typer(help="DGI : fichier des contribuables (NIU)")
app.add_typer(crawl_app, name="crawl")
app.add_typer(agent_app, name="agent")
app.add_typer(hf_app, name="hf")
app.add_typer(dgi_app, name="dgi")


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
                 max_pages: int = typer.Option(None), details: bool = True,
                 rebuild: bool = typer.Option(False, help="reparsage BeautifulSoup des pages de liste déjà capturées")):
    from .sources import katika

    if rebuild:
        typer.echo(f"{katika.rebuild_items()} items reparsés")
    typer.echo(f"{katika.crawl(_years(years), max_pages, details)} items")


@crawl_app.command("katika-map")
def crawl_katika_map(years: str = typer.Option(None, help="années sondées, ex. 2018-2026")):
    """Cartographie katika237.com/tender/ : filtres, volumes par type et par année."""
    from .sources import katika

    typer.echo(json.dumps(katika.cartography(_years(years)), ensure_ascii=False, indent=2))


@crawl_app.command("armp-discover")
def crawl_armp_discover():
    from .sources import armp

    typer.echo(json.dumps(armp.discover_types(), indent=2))


@crawl_app.command("armp")
def crawl_armp(types: str = typer.Option(None, help="codes type_publication, ex. AO,DC"),
               pages: int = typer.Option(None, help="pages du rattrapage « 3 derniers mois »"),
               exercices: str = typer.Option(None, help="années, ex. 2021,2026 (défaut : config)"),
               codes: str = typer.Option(None, help="codes type_procedure, ex. 1,5"),
               no_recent: bool = typer.Option(False),
               region: str = typer.Option(None), departement: str = typer.Option(None),
               maitre_ouvrage: str = typer.Option(None), seuil: int = typer.Option(None),
               reference: str = typer.Option(None), details: bool = typer.Option(False)):
    """Couverture ARMP : rattrapage récent + /filtres_avancee 2016→2026."""
    from .sources import armp

    n = armp.crawl_publications(
        types.split(",") if types else None, pages, region, departement, maitre_ouvrage,
        seuil, reference,
        exercices=[int(x) for x in exercices.split(",")] if exercices else None,
        codes=[int(x) for x in codes.split(",")] if codes else None,
        recent=not no_recent, details=details)
    typer.echo(f"{n} publications")


@crawl_app.command("armp-ids")
def crawl_armp_ids(types: str = typer.Option(None, help="ex. DEC-ATTR"), start: int = None, end: int = None):
    """Repli : énumération numérique des fiches /details."""
    from .sources import armp

    typer.echo(f"{armp.crawl_details(types.split(',') if types else None, start, end)} fiches")


@crawl_app.command("armp-sanctions")
def crawl_armp_sanctions():
    from .sources import armp

    typer.echo(f"{armp.crawl_sanctions()} sanctions")


@crawl_app.command("armp-experts")
def crawl_armp_experts(region: str = typer.Option(None), ville: str = typer.Option(None),
                       domaines: str = typer.Option(None, help="ex. AG,BEC")):
    from .sources import armp

    typer.echo(f"{armp.crawl_experts(region, ville, domaines.split(',') if domaines else None)} experts")


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
def crawl_coleps(sitemap: Path = typer.Option(None), max_pages: int = 500,
                 browser: bool = typer.Option(False, help="rejouer une carte avec Playwright au lieu du parcours HTTP")):
    from .sources import coleps

    if not browser:
        typer.echo(f"{coleps.crawl_http(max_pages)} items")
        return
    path = sitemap or coleps.default_sitemap_path()
    if not path.exists():
        typer.echo(f"carte absente : {path} (lancez `cmrmp agent explore`)", err=True)
        raise typer.Exit(1)
    try:
        typer.echo(f"{coleps.crawl(path, max_pages)} items")
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@crawl_app.command("documents")
def crawl_documents(limit: int = None):
    """Télécharge les documents mis en file par les autres collecteurs."""
    from .sources import documents

    typer.echo(json.dumps(documents.download_all(limit)))


@agent_app.command("explore")
def agent_explore(url: str = typer.Argument(None), out: Path = typer.Option(None), max_steps: int = 60):
    """Cartographie un site avec browser-use (modèle llama.cpp local ou Claude ; COLEPS par défaut)."""
    from .agents import browser_agent
    from .config import data_dir, settings

    cfg = settings()["coleps"]
    url = url or cfg["start_urls"][0]
    # Sortie par défaut : data/sitemaps (la carte relue de config/sitemaps n'est jamais écrasée).
    res = browser_agent.run(url, cfg["allowed_domains"], out or data_dir("sitemaps") / "coleps.json", max_steps)
    if not res:
        typer.echo("aucune carte produite : voir le fichier de sortie", err=True)
        raise typer.Exit(1)
    typer.echo(res.model_dump_json(indent=2))


@agent_app.command("check")
def agent_check(base_url: str = typer.Option(None, help="ex. http://127.0.0.1:8080/v1")):
    """Vérifie le serveur llama.cpp : santé et sortie structurée (schéma JSON)."""
    from .agents import browser_agent

    url = base_url or browser_agent.agent_settings()["base_url"]
    try:
        typer.echo(json.dumps(browser_agent.check_llm(url), ensure_ascii=False))
    except Exception as exc:
        typer.echo(f"llama-server inutilisable sur {url} : {exc}", err=True)
        raise typer.Exit(1)


@app.command()
def extract(llm: bool = typer.Option(False, help="extraction structurée par Claude"), workers: int = 4):
    from .extract import pipeline

    df = pipeline.run(llm=llm, workers=workers)
    typer.echo(df["doc_type"].value_counts().to_string() if not df.empty else "aucun document")


@app.command()
def build():
    """Tables, résolution d'entités, graphe, signaux d'alerte, coûts."""
    from .build import graph, tables
    from .time import run_meta, utc_now_iso

    meta = run_meta()
    t = tables.build_all()
    t["aretes"] = graph.edges(t)
    t["signaux_alerte"] = graph.red_flags(t)
    t["couts_relations"], t["couts_marches"] = graph.relation_costs(t, t["aretes"])
    # Add build metadata to a lightweight manifest
    try:
        import json
        from .config import data_dir
        manifest = data_dir("processed") / "build_meta.json"
        existing = {}
        if manifest.exists():
            try:
                existing = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        meta_out = {**existing, **meta, "built_at_utc": utc_now_iso("seconds")}
        manifest.write_text(json.dumps(meta_out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
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
    # Une table vide est écrite sans colonnes : on ne lit que les colonnes présentes.
    if (p / "avis.parquet").exists() and report["avis"]:
        avis = pd.read_parquet(p / "avis.parquet")
        year = pd.to_datetime(avis["date_publication"], errors="coerce").dt.year
        report["avis_par_annee"] = year.value_counts().sort_index().to_dict()
        report["top_maitres_ouvrage"] = avis["maitre_ouvrage"].value_counts().head(15).to_dict()
        if (p / "soumissions.parquet").exists() and report["soumissions"]:
            s = pd.read_parquet(p / "soumissions.parquet")
            refs_pv = set(s["reference"].dropna())
            report["part_avis_avec_pv"] = round(avis["reference"].isin(refs_pv).mean(), 3)
            report["part_offres_avec_montant"] = round(s["montant_offre_fcfa"].notna().mean(), 3)
    if (p / "documents.parquet").exists() and report["documents"]:
        d = pd.read_parquet(p / "documents.parquet")
        report["documents_par_type"] = d["doc_type"].value_counts().to_dict()
        report["part_pdf_ocr"] = round(d["ocr"].mean(), 3) if "ocr" in d else None
    out = data_dir("reports") / "audit.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    # Also run provenance audit
    try:
        from . import audit_provenance
        prov = audit_provenance.audit_provenance()
        typer.echo(json.dumps(prov, ensure_ascii=False, indent=2))
    except Exception as e:
        typer.echo(f"provenance audit failed: {e}", err=True)


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


@hf_app.command("watch")
def hf_watch(bucket: str = typer.Option(...),
             interval_s: int = typer.Option(1200, help="secondes entre deux envois incrémentaux"),
             path: list[str] = typer.Option(None, help="dossiers à synchroniser (défaut : tout le cache)")):
    """Envoi en temps réel (run long) : synchronisation incrémentale répétée vers le bucket.

    sync_bucket ne renvoie que les fichiers nouveaux ou modifiés : un push régulier
    ne re-transfère pas le brut déjà envoyé. Nécessite HF_TOKEN (dans .env ou
    l'environnement)."""
    import time as _time

    from .publish import hf

    settings()  # charge .env (HF_TOKEN)
    if not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")):
        typer.echo("HF_TOKEN absent : créez un fichier .env avec HF_TOKEN=hf_xxx", err=True)
        raise typer.Exit(1)
    paths = path or ["raw", "interim", "text", "llm_cache", "reports", "processed"]
    n = 0
    while True:
        t0 = utc_now_iso("seconds")
        try:
            hf.sync_raw(bucket, direction="up", paths=paths)
            typer.echo(f"{t0} push #{n + 1} ok")
        except Exception as exc:
            typer.echo(f"{t0} push échoué : {exc}", err=True)
        n += 1
        _time.sleep(interval_s)


@dgi_app.command("crawl")
def dgi_crawl(url: list[str] = typer.Option(None, help="URL de consultation (répétable)"),
              official: bool = typer.Option(True, help="listes officielles PDF/Excel aussi")):
    """Parcourt les formulaires de consultation et télécharge les listes et exports."""
    from .sources import dgi

    n = dgi.crawl_consultation(url or None)
    m = dgi.crawl_official() if official else 0
    typer.echo(f"consultation : {n} lignes ; fichiers officiels : {m} lignes")


@dgi_app.command("unify")
def dgi_unify(keep_natural_persons: bool = typer.Option(None, help="ne pas pseudonymiser les NIU en P")):
    """Fusionne toutes les sources en un seul fichier (un NIU par ligne)."""
    import os

    from .sources import dgi

    df = dgi.unify()
    paths = dgi.write_unified(df, os.environ.get("CMRMP_SALT"), keep_natural_persons)
    typer.echo(f"{len(df)} NIU -> {paths['parquet']} et {paths['csv']}")


@dgi_app.command("publish")
def dgi_publish(bucket: str = typer.Option(..., help="utilisateur/nom-du-bucket"),
                public: bool = typer.Option(False)):
    """Envoie le fichier unifié et le brut DGI dans le bucket Hugging Face."""
    from .sources import dgi

    dgi.publish_bucket(bucket, private=not public)
    typer.echo(f"envoyé : hf://buckets/{bucket}/dgi")


@hf_app.command("check")
def hf_check(bucket: str = typer.Option(None)):
    """Vérifie le jeton Hugging Face (et l'accès au bucket) avant de lancer une tâche longue."""
    import os

    from huggingface_hub import HfApi

    if not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")):
        typer.echo("HF_TOKEN absent : créez le secret GitHub HF_TOKEN (gh secret set HF_TOKEN)")
        raise typer.Exit(1)
    api = HfApi()
    try:
        me = api.whoami()
    except Exception as exc:
        typer.echo(f"jeton Hugging Face refusé : {exc}")
        raise typer.Exit(1)
    typer.echo(f"connecté en tant que {me.get('name')}")
    if bucket:
        try:
            api.create_bucket(bucket, private=True, exist_ok=True)
            typer.echo(f"bucket prêt : {bucket}")
        except Exception as exc:
            typer.echo(f"bucket {bucket} inaccessible : {exc}")
            raise typer.Exit(1)


def run():
    """Point d'entrée : un site injoignable ou interdit par robots.txt donne le code 3
    (avertissement), pas un plantage ; la suite du pipeline continue."""
    from .http import HostUnreachable, RobotsDisallowed

    try:
        app()
    except (HostUnreachable, RobotsDisallowed) as exc:
        typer.echo(f"SOURCE INDISPONIBLE : {type(exc).__name__}: {exc}", err=True)
        raise SystemExit(3)


if __name__ == "__main__":
    run()
