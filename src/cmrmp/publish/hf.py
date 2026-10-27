"""Publication Hugging Face : tables Parquet dans un dépôt dataset, documents bruts dans un bucket.

- Dépôt dataset (versionné, citable) : une configuration par table, carte de jeu de
  données avec licence, provenance, limites et avertissement sur les données personnelles.
- Bucket de stockage (S3-like, dédupliqué Xet, sans historique Git) : PDF et HTML bruts
  et manifeste de capture, synchronisés avec `sync_bucket`. Sert aussi de cache
  persistant entre deux exécutions GitHub Actions.

Données personnelles (loi n° 2024/017, en vigueur depuis le 23 juin 2026) : noms de
dirigeants, téléphones et e-mails sont remplacés par un hachage salé avant publication
publique ; le sel (CMRMP_SALT) reste secret, ce qui garde les liens « même dirigeant »
calculables sans exposer les personnes.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..config import data_dir
from ..time import utc_now

PERSONAL_COLS = {"representant", "telephone", "email", "adresse",
                 "representants", "telephones", "emails", "adresses"}
# Arêtes dont l'`evidence` est une donnée personnelle (un entrepreneur individuel est domicilié
# à son adresse personnelle).
PERSONAL_RELATIONS = ["meme_dirigeant", "meme_telephone", "meme_email", "meme_adresse"]

TABLE_DESCRIPTIONS = {
    "publications": "Index brut des publications (Katika237, ARMP, PRIDESOFT) : une ligne par item listé.",
    "avis": "Avis d'appel d'offres, une ligne par lot : procédure, coût du dossier, caution, capacité financière, dates.",
    "soumissions": "Toutes les offres lues dans les PV d'ouverture et de jugement : soumissionnaire, montant, recevabilité, rang.",
    "attributions": "Décisions d'attribution : attributaire, montant, délai, statut (attribué, infructueux, annulé).",
    "entreprises": "Entreprises résolues (entity_id) avec graphies, NIU, RCCM et contacts pseudonymisés.",
    "sanctions": "Exclusions, suspensions et résiliations publiées (MINMAP, ARMP).",
    "aretes": "Arêtes du graphe hétérogène (co-soumission, mêmes coordonnées, historique, succession).",
    "signaux_alerte": "Signaux d'alerte par marché et lot (étiquettes faibles, à ne pas utiliser comme variables).",
    "couts_relations": "Coûts de création et de suppression de chaque type de relation (FCFA).",
    "couts_marches": "Coûts d'injection d'une co-soumission par marché, en FCFA et en part du montant.",
    "documents": "Inventaire des documents : URL, type, date de capture, empreinte SHA-256.",
}


def pseudonymize(df: pd.DataFrame, salt: str) -> pd.DataFrame:
    df = df.copy()
    h = lambda v: None if v is None or (isinstance(v, float) and pd.isna(v)) else "h_" + hashlib.sha256(f"{salt}|{v}".encode()).hexdigest()[:16]
    for col in PERSONAL_COLS & set(df.columns):
        def anon(v):
            if isinstance(v, str) and v.startswith("["):
                try:
                    return json.dumps([h(x) for x in json.loads(v)])
                except json.JSONDecodeError:
                    pass
            return h(v)
        df[col] = df[col].map(anon)
    if {"relation", "evidence"} <= set(df.columns):
        personal = df["relation"].isin(PERSONAL_RELATIONS)
        df.loc[personal, "evidence"] = df.loc[personal, "evidence"].map(h)
    return df


def dataset_card(repo_id: str, tables: dict[str, int]) -> str:
    configs = "\n".join(f"- config_name: {t}\n  data_files: data/{t}.parquet" for t in tables)
    rows = "\n".join(f"| `{t}` | {n:,} | {TABLE_DESCRIPTIONS.get(t, '')} |" for t, n in tables.items())
    now = utc_now().strftime("%Y-%m-%d")
    return f"""---
language: [fr, en]
license: cc-by-4.0
pretty_name: Marchés publics camerounais (graphe hétérogène)
task_categories: [tabular-classification, graph-ml]
tags: [public-procurement, cameroon, collusion, red-flags, ocds, graph]
size_categories: [10K<n<1M]
configs:
{configs}
---

# Marchés publics camerounais — graphe hétérogène et coûts de manipulation

Jeu de données construit à partir des publications publiques de l'ARMP (armp.cm, PRIDESOFT),
du portail COLEPS, du miroir Katika237 et des communiqués du MINMAP. Dernière mise à jour : {now}.

| Table | Lignes | Contenu |
| --- | ---: | --- |
{rows}

## Provenance
Chaque ligne garde l'URL source et la date de capture ; les documents bruts et le manifeste de
capture (URL, statut HTTP, SHA-256) sont dans le bucket associé. Les champs extraits par LLM
sont accompagnés de la citation source et, quand c'est possible, d'un contrôle par expression
régulière (`*_regex_concorde`).

## Limites
- Couverture inégale selon les années et les maîtres d'ouvrage ; tous les PV ne listent pas toutes les offres.
- La résolution d'entités est approximative ; un échantillon annoté est fourni (`er_paires_a_verifier`).
- Les sanctions publiées concernent surtout des défaillances d'exécution, pas la collusion prouvée.
- Les signaux d'alerte sont des indices, pas des preuves : aucune entreprise ne doit être qualifiée
  de fraudeuse sur la seule base de ces données.

## Données personnelles
Noms de dirigeants, téléphones et e-mails sont pseudonymisés (hachage salé) conformément à la
loi camerounaise n° 2024/017 sur la protection des données à caractère personnel. Les raisons
sociales et identifiants d'entreprises (NIU, RCCM) publiés par l'administration sont conservés.

## Citation
Voir le dépôt de code `{repo_id}` et l'article associé.
"""


def publish(repo_id: str, bucket_id: str | None = None, private: bool = True, salt: str | None = None) -> None:
    from huggingface_hub import HfApi

    api = HfApi()
    salt = salt or os.environ.get("CMRMP_SALT")
    if not salt:
        raise RuntimeError("CMRMP_SALT manquant : nécessaire pour pseudonymiser avant publication.")
    processed = data_dir("processed")
    stage = data_dir("hf_stage", "data")
    counts = {}
    for path in sorted(processed.glob("*.parquet")):
        if path.stem in ("mentions",):
            continue  # table de travail, non publiée
        df = pseudonymize(pd.read_parquet(path), salt)
        df.to_parquet(stage / path.name, index=False)
        counts[path.stem] = len(df)
    (stage.parent / "README.md").write_text(dataset_card(repo_id, counts), encoding="utf-8")

    api.create_repo(repo_id, repo_type="dataset", private=private, exist_ok=True)
    api.upload_folder(repo_id=repo_id, repo_type="dataset", folder_path=str(stage.parent),
                      commit_message=f"Mise à jour {utc_now():%Y-%m-%d}")
    if bucket_id:
        # Seul le dataset pseudonymisé peut devenir public ; le brut reste privé.
        sync_raw(bucket_id, direction="up")


def sync_raw(bucket_id: str, direction: str = "up", private: bool = True, paths: list[str] | None = None) -> None:
    """Synchronise des sous-dossiers de data/ avec le bucket ('up' ou 'down').

    `paths` vaut par défaut tout le cache (raw, interim, text, llm_cache). Une tâche de
    collecte ne synchronise que ses dossiers (raw/<source>, interim/<source>) : plusieurs
    tâches peuvent tourner en parallèle sans s'écraser.

    Le bucket est toujours créé privé : il contient le brut non pseudonymisé (PDF avec
    téléphones et dirigeants, pages DGI de personnes physiques, cache LLM). `private=False`
    est ignoré.
    """
    from huggingface_hub import HfApi

    api = HfApi()
    if direction == "up":
        if not private:
            print("bucket brut : publication publique refusée, il reste privé (données personnelles non pseudonymisées)")
        api.create_bucket(bucket_id, private=True, exist_ok=True)
    for sub in paths or ["raw", "interim", "text", "llm_cache"]:
        local = str(data_dir(*sub.split("/")))
        remote = f"hf://buckets/{bucket_id}/{sub}"
        if direction == "up":
            api.sync_bucket(local, remote, quiet=True)
        else:
            try:
                api.sync_bucket(remote, local, quiet=True)
            except Exception as exc:  # premier lancement : bucket vide ou absent
                print(f"sync down {sub} ignoré : {exc}")
