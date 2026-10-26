# cmrmp : données des marchés publics camerounais

Code de collecte, d'extraction et de publication du jeu de données de l'article EAAI
*Not all procurement ties are equally cheap to fake*. Il construit un graphe hétérogène
(avis, offres, attributions, sanctions, entreprises), ses signaux d'alerte, et les coûts de
manipulation de chaque relation mesurés en FCFA, puis publie le tout sur Hugging Face.

```
collecte (httpx poli, Playwright)      extraction                    construction                 publication
katika237 ─┐                           texte PDF/DOCX/HTML (+OCR)     tables par lot               dataset HF (Parquet,
armp.cm ───┤                           regex : FCFA, réf., NIU,       résolution d'entités          une config par table)
pridesoft ─┼─> data/raw (immuable) ─>  RCCM, dates                ─>  graphe (arêtes typées)  ─>  bucket HF (PDF bruts,
minmap ────┤   + manifeste SHA-256     Claude : schémas Pydantic      signaux d'alerte             manifeste, caches)
coleps ────┘   par source              (avis, PV, décision, sanction) coûts calibrés FCFA
```

## Sources

| Source | Contenu | Accès | Module |
| --- | --- | --- | --- |
| [Katika237](https://katika237.com/tender/) | miroir des avis ARMP (≈ 143 800 entrées), filtres année/région/type | HTML paginé `?page=&r=&type=&y=` | `sources/katika.py` |
| [armp.cm](https://www.armp.cm/) | fiches `details?type_publication=DEC-ATTR&id_publication=N` (attributaire, montant, délai) | identifiants séquentiels | `sources/armp.py` |
| [PRIDESOFT](https://armp.cm/pridesoft) | rapports d'avis `index.php?type_avis=AO&id=N`, documents `/_lib/file/doc/0903/AAAA/MM/JJ/ENTITE/` | identifiants séquentiels | `sources/armp.py` |
| JDM (ARMP) | Journal des marchés en PDF | **espace abonnés : désactivé par défaut** | `sources/armp.py` |
| [MINMAP](https://minmap.cm/?p=19494) | communiqués d'exclusion, de suspension, de résiliation | API REST WordPress, sinon recherche HTML | `sources/minmap.py` |
| COLEPS (marchespublics.cm) | plateforme électronique, obligatoire depuis l'exercice 2025 | JavaScript : carte par agent puis Playwright | `agents/`, `sources/coleps.py` |

Les gabarits d'URL et les sélecteurs sont dans `config/sources.yaml`. Ils viennent d'URL
publiques relevées en octobre 2026, **sans test sur les sites en direct** (inaccessibles depuis
l'environnement de développement). Avant une collecte massive, vérifiez chaque source :

```bash
cmrmp discover "https://katika237.com/tender/?page=1&y=2025"   # motifs d'URL, blocs répétés, formulaires
cmrmp crawl armp-discover                                        # codes type_publication présents sur armp.cm
cmrmp crawl katika --years 2025 --max-pages 2                    # petit essai, puis inspecter data/interim/katika/
```

## Installation

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[hf,llm,browser,dev]"
cp .env.example .env        # renseigner CMRMP_CONTACT, ANTHROPIC_API_KEY, HF_TOKEN, CMRMP_SALT
pytest -q
```

L'agent navigateur s'installe à part, car `browser-use` épingle une version de `click`
incompatible avec `huggingface_hub` récent :

```bash
python -m venv .venv-agent && .venv-agent/bin/pip install -e ".[agent,browser]"
.venv-agent/bin/python -m playwright install chromium
```

## Utilisation de bout en bout

```bash
cmrmp crawl katika --years 2018-2026
cmrmp crawl armp --types DEC-ATTR
cmrmp crawl pridesoft --types AO
cmrmp crawl minmap
.venv-agent/bin/cmrmp agent explore https://www.marchespublics.cm   # une fois ; relire la carte
cp data/sitemaps/coleps.json config/sitemaps/coleps.json            # carte relue, versionnée
cmrmp crawl coleps
cmrmp crawl documents          # PDF/DOCX mis en file par les collecteurs
cmrmp extract --llm            # PyMuPDF (texte, OCR, tableaux) + regex + Claude (cache par SHA-256)
cmrmp build                    # tables, entités, graphe, signaux d'alerte, coûts
cmrmp audit                    # couverture : années, maîtres d'ouvrage, part des marchés avec PV
cmrmp hf publish --repo utilisateur/cmr-marches-publics --bucket utilisateur/cmr-marches-brut
```

Toutes les étapes sont relançables : une URL déjà capturée n'est pas re-téléchargée, une
extraction LLM déjà faite n'est pas refacturée.

## Tables produites (`data/processed/`, publiées sur Hugging Face)

| Table | Grain | Champs principaux |
| --- | --- | --- |
| `avis` | référence × lot | maître d'ouvrage, région, objet, secteur, procédure, coût prévisionnel, coût du dossier, caution, capacité financière, dates, contrôle regex |
| `soumissions` | offre | soumissionnaire brut, `entity_id`, montant, recevabilité, rang, motif de rejet, NIU, RCCM, contacts |
| `attributions` | lot attribué | attributaire, montant, délai, statut |
| `sanctions` | entreprise sanctionnée | autorité, date, type, durée, motif |
| `entreprises` | entité résolue | graphies, NIU, RCCM, adresses, contacts (pseudonymisés) |
| `aretes` | arête typée | co_soumission, meme_adresse, meme_telephone, meme_email, meme_dirigeant, meme_niu, meme_rccm, historique_maitre_ouvrage, succession_attribution |
| `signaux_alerte` | référence × lot | offre unique, délai court, procédure non ouverte, offres groupées, écart minime, proche de l'estimation, attributaire récurrent, score composite |
| `couts_relations`, `couts_marches` | relation, marché | coût de création et de suppression (FCFA), coût d'injection d'une co-soumission en part du marché |
| `publications`, `documents` | item, document | URL, type, date de capture, SHA-256 |
| `er_paires_a_verifier` | paire fusionnée | échantillon à annoter pour le taux d'erreur de la résolution d'entités |

Le coût d'injection d'une co-soumission vaut : coût du dossier + caution × taux annuel ×
validité des offres / 365 + part de préparation × coût prévisionnel. Le taux, la part de
préparation et les frais de création ou de modification de société sont des hypothèses dans
`config/sources.yaml` (section `costs`), à remplacer par des valeurs sourcées.

## GitHub Actions

| Workflow | Déclencheur | Rôle |
| --- | --- | --- |
| `ci.yml` | push, PR | tests |
| `pipeline.yml` | chaque dimanche 01:17 UTC, ou manuel | tests → collecte (une tâche par source, en parallèle) → documents → extraction, graphe, audit → publication HF |
| `agent-explore.yml` | manuel | cartographie de COLEPS par l'agent ; la carte sort en artefact pour relecture |

Le bucket Hugging Face sert de cache persistant entre deux lancements : chaque tâche
récupère ses dossiers (`raw/<source>`, `interim/<source>`), collecte au plus 320 minutes, puis
renvoie ce qu'elle a capturé même si le délai est atteint. La collecte reprend au lancement suivant.

À configurer dans *Settings → Secrets and variables → Actions* :

| Nom | Type | Valeur |
| --- | --- | --- |
| `HF_TOKEN` | secret | jeton Hugging Face en écriture |
| `ANTHROPIC_API_KEY` | secret | clé API Claude (extraction et agent) |
| `CMRMP_SALT` | secret | sel aléatoire long pour la pseudonymisation |
| `CMRMP_CONTACT` | variable | e-mail de contact placé dans le User-Agent |
| `HF_DATASET` | variable | ex. `utilisateur/cmr-marches-publics` |
| `HF_BUCKET` | variable | ex. `utilisateur/cmr-marches-brut` |
| `RUNNER` | variable (facultative) | runner auto-hébergé, si les sites bloquent ou ralentissent les IP des runners GitHub |

## Pratiques retenues

- **Politesse** : User-Agent identifiable avec contact, robots.txt suivi (Crawl-delay compris,
  règles d'erreur de la RFC 9309), une requête toutes les 3 à 4,5 secondes par hôte, reprise
  exponentielle sur 429/5xx en suivant `Retry-After`, requêtes conditionnelles (ETag).
  Ces réglages suivent les recommandations AWS pour les sites publics de taille moyenne et le
  projet IETF de mars 2026 sur la collecte responsable.
- **Brut immuable et provenance** : chaque réponse est stockée telle quelle avec URL, date,
  statut et SHA-256. Les tables gardent l'URL source. On peut tout recalculer sans re-collecter.
- **Agent pour découvrir, code pour collecter** : un agent LLM qui raisonne à chaque étape est
  plus résilient mais lent et coûteux. On l'utilise une fois par site, avec domaines autorisés,
  aucune connexion, étapes bornées et sortie validée par schéma, et le contenu des pages est traité
  comme non fiable (injection de prompt). Un humain relit la carte, puis Playwright collecte.
- **PDF avec PyMuPDF** : texte trié par ordre de lecture ; pages scannées passées à l'OCR
  intégré de PyMuPDF (Tesseract `fra+eng`, 300 dpi), avec ocrmypdf en repli ; tableaux détectés
  par `find_tables` (lignes tracées, puis alignement du texte), dont le texte des cellules est
  recomposé à partir des mots ; métadonnées du PDF (date de création, logiciel). Les tableaux des
  PV donnent une table `soumissions` sans LLM (`modele_extraction = pymupdf_tables`), et le LLM
  reçoit le texte suivi des tableaux en Markdown. Un scan trop lourd est envoyé page par page
  sous forme d'images rendues par PyMuPDF.
- **LLM sous contrôle** : schémas Pydantic stricts, champs absents laissés à null, citation source
  pour chaque montant, contrôle croisé par regex, cache par empreinte, repli serveur en cas de refus.
- **Anti-circularité** : les signaux d'alerte sont des étiquettes faibles, publiées à part. Ils ne
  doivent pas servir de variables du modèle évalué.
- **Données personnelles** : la loi n° 2024/017 est en vigueur depuis le 23 juin 2026. Les
  dirigeants, téléphones et e-mails sont hachés avec un sel secret avant publication, ce qui garde
  les arêtes « même dirigeant » sans exposer les personnes. Le dataset est privé par défaut.
- **Droit d'accès** : le JDM est derrière un espace abonnés et reste désactivé. COLEPS n'est
  collecté que sur ses pages publiques. Lisez les conditions d'utilisation de chaque site, et
  demandez si possible un export à l'ARMP (support@armp.cm), ce qui vaut mieux que du scraping.

## Limites connues

- Sélecteurs et gabarits non validés sur les sites réels : `cmrmp discover` sert à les ajuster.
- L'énumération des identifiants ARMP et PRIDESOFT suppose des identifiants séquentiels, et la
  détection des pages vides repose sur des marqueurs textuels à confirmer.
- La résolution d'entités ne sépare les groupements que sur `/` et `+`.
- La génération de collusion synthétique et les modèles (CARS-D, gating) restent dans le code du
  preprint ICML ; ce dépôt fournit le graphe réel et les coûts qui les alimentent.
