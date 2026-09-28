# Où acheter autour de Paris ?

**[www.ou-acheter-paris.fr](https://www.ou-acheter-paris.fr)**

Une carte des prix de vente réels des logements de Paris et de sa banlieue, pour choisir
où acheter : prix au m² par commune et par quartier, chaque vente une à une en zoomant,
évolution des prix, et les gares actuelles et à venir (Grand Paris Express, tramways,
RER).

Gratuit, sans publicité, sans compte. Aucune donnée n'est collectée sur les visiteurs.

## D'où viennent les chiffres

Des **demandes de valeurs foncières** (DVF) : chaque vente immobilière enregistrée par
l'administration fiscale, publiée en données ouvertes. Ce site est une réutilisation à
but non lucratif de données publiques.

| Donnée | Source | Licence |
|---|---|---|
| Ventes des cinq dernières années | [DVF géolocalisées](https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees) (DGFiP, Etalab) | Licence Ouverte 2.0 |
| Périmètre : 425 communes | [Unité urbaine de Paris](https://www.insee.fr/fr/metadonnees/geographie/unite-urbaine-2020/00851-paris) (INSEE) | Licence Ouverte 2.0 |
| Contours des quartiers | [Cadastre](https://cadastre.data.gouv.fr/) (Etalab) | Licence Ouverte 2.0 |
| Gares et projets | [Île-de-France Mobilités](https://data.iledefrance-mobilites.fr/) | Licence Ouverte 2.0 |
| Fonds de carte | Esri, OpenStreetMap, IGN | Conditions de chaque fournisseur |

Seules les ventes d'appartements et de maisons sont retenues. Sont écartées celles qui
ne reflètent pas un prix de marché : ventes mêlant logement et local commercial, ventes
de 3 logements ou plus, prix symboliques ou invraisemblables. Une zone de moins de
5 ventes reste grise. Les données et les gares sont vérifiées chaque mois et mises à
jour automatiquement.

## Comment ça marche

Un site entièrement statique. Des scripts Python téléchargent et nettoient les données,
puis produisent des fichiers ; le navigateur affiche la carte
([MapLibre GL](https://maplibre.org/)) et recalcule les statistiques à chaque filtre,
sans serveur.

```
site/      la page : carte, filtres, calcul dans le navigateur
src/       préparation des données : nettoyage, statistiques, contours, gares
scripts/   construction du site et mises à jour
data/      ventes nettoyées, contours, gares
tests/     tests Python, navigateur et cohérence Python / JavaScript
```

## Lancer le site chez soi

```bash
pip install -r requirements-dev.txt
python scripts/construire_site.py            # construit le site dans dist/
python -m http.server -d dist 8000           # puis http://localhost:8000
```

Tests : `python -m playwright install chromium`, puis `python -m pytest -q`.

## Publier sa propre copie

La CI publie le site sur Cloudflare Pages après les tests. Elle attend, dans
*Settings → Secrets and variables → Actions* :

- secrets `CLOUDFLARE_API_TOKEN` (droit *Cloudflare Pages · Edit*) et
  `CLOUDFLARE_ACCOUNT_ID` ;
- variable `CLOUDFLARE_PROJECT_NAME` (nom du projet Pages) ;
- secret `JETON_MISE_A_JOUR` : jeton GitHub à droits fins sur le seul dépôt
  (*Contents*, *Pull requests* et *Workflows* en écriture), avec la plus longue durée
  proposée. Il pousse les branches des mises à jour automatiques, fait tourner leur CI,
  et fusionne celles qui touchent aux workflows. La veille prévient un mois avant son
  expiration ;
- variable facultative `ADRESSE_SITE`, pour une autre adresse que celle du site.

## Maintenance automatique

Le site se tient à jour seul ; il ne demande une intervention qu'en cas d'imprévu, et
le dit alors par une issue (donc un courriel au propriétaire du dépôt).

| Quand | Quoi | Workflow |
|---|---|---|
| le 3 du mois | nouvelles DVF d'Etalab ? fenêtre glissée, site reconstruit, tests, pull request | `donnees.yml` |
| le 10 du mois | gares ouvertes ou annoncées ? même chemin | `reseau.yml` |
| chaque mois | nouvelles versions des actions et des dépendances Python | Dependabot |
| après chaque CI, et chaque jour | fusion des pull requests automatiques vertes, une à la fois | `fusion-auto.yml` |
| chaque lundi | veille : site en ligne, fraîcheur des données, fonds de carte, jeton, pull requests en attente, CI de `main`, workflows réactivés, fin de vie de Python | `veille.yml` |

Garde-fous :

- une pull request n'est fusionnée que si **tous les tests** passent, et seulement si
  la CI de `main` est verte ; le site n'est déployé que depuis un `main` vert. Au pire,
  le site en ligne reste sur sa dernière bonne version ;
- une mise à jour des données qui s'écarte de l'ordinaire (ventes en chute, prix révisés
  à années égales, communes disparues, contours manquants) n'est pas fusionnée :
  étiquette `a-verifier`, et une issue ;
- une CI rouge est relancée une fois ; si elle reste rouge, la pull request attend, et
  la veille la signale au bout de trois semaines ;
- les pull requests faites à la main ne sont jamais fusionnées d'office.

Ce qui reste hors de portée de l'automatisation, et que la veille signale : le
renouvellement du nom de domaine (à confier au renouvellement automatique du
registraire), le jeton GitHub à recréer à son expiration, un changement d'adresse ou de
format des sources (Etalab, IDFM, fonds de carte), ou la fin d'un service gratuit
(Cloudflare Pages, GitHub Actions).
