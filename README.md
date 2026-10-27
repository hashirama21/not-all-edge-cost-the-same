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
| DGI (fiscalis.dgi.cm) | fichier des contribuables : NIU, raison sociale, activité, centre, régime ; listes officielles (inactifs…) | formulaire ASP.NET `listecontribuable.aspx` + PDF/Excel | `sources/dgi.py` |
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

## Agent browser-use sur modèle local (llama.cpp)

L'agent [browser-use](https://github.com/browser-use/browser-use) cartographie COLEPS avec
un modèle servi en local par `llama-server` (llama.cpp, API compatible OpenAI). browser-use
le pilote via `ChatOpenAI(base_url=…)` ; la carte est rendue au format `SiteMap` grâce à
`response_format: json_schema`, que llama.cpp convertit en grammaire.

```bash
bash scripts/llama_server.sh                 # Qwen3.5-9B en Docker, attend /health
.venv-agent/bin/cmrmp agent check            # santé du serveur + sortie structurée conforme
.venv-agent/bin/cmrmp agent explore          # carte dans data/sitemaps/coleps.json
cmrmp crawl coleps                           # carte validée (schéma, domaines autorisés), puis Playwright
```

Réglages dans `config/sources.yaml` (section `agent`) ou par variables : `CMRMP_AGENT_BACKEND`
(`llamacpp` par défaut, `anthropic` pour Claude), `LLAMA_BASE_URL`, `CMRMP_AGENT_MODEL`,
`CMRMP_AGENT_VISION` (avec `LLAMA_VISION=true` côté serveur pour charger le `mmproj`).

| Matériel | Modèle (`LLAMA_MODEL`) | Taille |
| --- | --- | --- |
| Runner GitHub standard (4 vCPU, 16 Go, sans GPU) : **par défaut** | `unsloth/Qwen3.5-9B-GGUF:Q4_K_M` | 5,7 Go |
| Même runner, pour aller plus vite | `unsloth/Qwen3.5-4B-GGUF:Q4_K_M` | plus léger, moins précis |
| Runner GPU ≥ 24 Go (`RUNNER` + `LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server-cuda`) | `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M` | 16,5 Go |

Qwen3.8 (août 2026) est la dernière génération de Qwen, mais son plus petit modèle fait 27B :
sur CPU, un pas d'agent prendrait des dizaines de minutes. Qwen3.5-9B est le plus récent des
petits Qwen ; il est multimodal et son raisonnement est coupé par défaut
(`LLAMA_THINKING=false`). Même ainsi, sur CPU, un pas d'agent prend plusieurs minutes, car les
prompts de browser-use font plusieurs milliers de jetons. L'agent tourne donc en `flash_mode`,
sans vision, avec 40 étapes au plus.

## Utilisation de bout en bout

```bash
cmrmp crawl katika --years 2018-2026
cmrmp crawl armp --types DEC-ATTR
cmrmp crawl pridesoft --types AO
cmrmp crawl minmap
bash scripts/llama_server.sh                                        # modèle local (voir plus bas)
.venv-agent/bin/cmrmp agent explore https://www.marchespublics.cm   # une fois ; relire la carte
cp data/sitemaps/coleps.json config/sitemaps/coleps.json            # carte relue, versionnée (prioritaire)
cmrmp crawl coleps
cmrmp crawl documents          # PDF/DOCX mis en file par les collecteurs
cmrmp extract --llm            # PyMuPDF (texte, OCR, tableaux) + regex + Claude (cache par SHA-256)
cmrmp build                    # tables, entités, graphe, signaux d'alerte, coûts
cmrmp audit                    # couverture : années, maîtres d'ouvrage, part des marchés avec PV
cmrmp hf publish --repo utilisateur/cmr-marches-publics --bucket utilisateur/cmr-marches-brut
```

Toutes les étapes sont relançables : une URL déjà capturée n'est pas re-téléchargée, une
extraction LLM déjà faite n'est pas refacturée.

## Fichier des contribuables (DGI)

```bash
cmrmp dgi crawl                     # parcours des formulaires + listes officielles
cmrmp dgi unify                     # data/dgi/contribuables.parquet et .csv.gz (un NIU par ligne)
cmrmp dgi publish --bucket krohn/cmr-marches-brut
```

`dgi crawl` lit le formulaire ASP.NET (`__VIEWSTATE`, `__EVENTVALIDATION`, listes déroulantes,
boutons). Il soumet chaque option des listes déroulantes, puis des préfixes de raison sociale
(A…Z, 0…9), raffinés d'une lettre tant que le résultat semble tronqué. Il suit la pagination
GridView (`__doPostBack(…, 'Page$N')`) et déclenche les boutons d'export (Excel, CSV, PDF).
Chaque POST est mis en cache sous une clé sans l'état ASP.NET : un parcours interrompu reprend
sans refaire de requêtes. Les listes officielles en PDF (contribuables inactifs au 1er février 2026,
fichier de la dépense publique du MINFI) sont lues avec les tableaux PyMuPDF, et les nouveaux
fichiers liés depuis impots.cm sont ajoutés automatiquement.

Le fichier unifié garde, par NIU : raison sociale, sigle, activité, centre, régime, localisation,
type de personne (M morale, P physique), statuts des listes où il figure et toutes les URL
sources. Les personnes physiques (NIU en P) sont pseudonymisées par défaut (NIU, nom, sigle et
localisation hachés avec `CMRMP_SALT`), et le bucket est privé par défaut. Le registre compte
environ 3,2 millions de NIU selon un guide tiers : à 3 secondes par page, un parcours complet
prend plusieurs jours, d'où la reprise par cache et le workflow mensuel `dgi.yml`.

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
| `pipeline.yml` | chaque dimanche 01:17 UTC, ou manuel | tests → collecte (une tâche par source, en parallèle ; pour COLEPS, l'agent browser-use + llama.cpp produit la carte s'il n'en existe pas de valide) → documents → extraction, graphe, audit → publication HF |
| `dgi.yml` | le 3 de chaque mois, ou manuel | fichier des contribuables → fichier unifié → bucket HF |
| `agent-explore.yml` | manuel | cartographie de COLEPS par l'agent (llama.cpp local ou Claude) ; la carte sort en artefact pour relecture |

Avec l'option `coleps_explore` de `pipeline.yml` : `auto` lance l'agent seulement sans carte
valide, `always` le relance, `never` ne le lance pas. Une carte relue dans `config/sitemaps/`
passe toujours avant celle de l'agent, que le bucket garde sous `sitemaps/`.

Le bucket Hugging Face sert de cache persistant entre deux lancements : chaque tâche
récupère ses dossiers (`raw/<source>`, `interim/<source>`), collecte au plus 320 minutes, puis
renvoie ce qu'elle a capturé même si le délai est atteint. La collecte reprend au lancement suivant.

À configurer dans *Settings → Secrets and variables → Actions* :

| Nom | Type | Valeur |
| --- | --- | --- |
| `HF_TOKEN` | secret | jeton Hugging Face en écriture (`gh secret set HF_TOKEN -R hashirama21/not-all-edge-cost-the-same`) |
| `ANTHROPIC_API_KEY` | secret | clé API Claude (extraction et agent) |
| `CMRMP_SALT` | secret | sel aléatoire long pour la pseudonymisation |
| `CMRMP_CONTACT` | variable | e-mail de contact placé dans le User-Agent |
| `HF_DATASET` | variable | par défaut `krohn/cmr-marches-publics` |
| `HF_BUCKET` | variable | par défaut `krohn/cmr-marches-brut` |
| `RUNNER` | variable (facultative) | runner auto-hébergé, si les sites bloquent ou ralentissent les IP des runners GitHub |
| `LLAMA_MODEL` | variable | votre modèle GGUF sur Hugging Face, `utilisateur/depot:quant` (par défaut `unsloth/Qwen3.5-9B-GGUF:Q4_K_M`) ; un dépôt privé passe par `HF_TOKEN` |
| `LLAMA_IMAGE` | variable (facultative) | image de llama-server, `ghcr.io/ggml-org/llama.cpp:server-cuda` sur runner GPU |
| `AGENT_MAX_STEPS` | variable (facultative) | nombre d'étapes de l'agent dans `pipeline.yml` (40) |

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
