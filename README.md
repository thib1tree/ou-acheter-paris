# Où acheter autour de Paris ?

**[www.ou-acheter-paris.fr](https://www.ou-acheter-paris.fr)**

Une carte des prix de vente réels des logements de Paris et de sa banlieue, pour choisir
où acheter : prix au m² par commune et par quartier, chaque vente une à une en zoomant,
évolution des prix, et les gares actuelles et à venir (Grand Paris Express, tramways,
RER).

Gratuit, sans publicité, sans compte, sans cookie ni mesure d'audience. Voir les
[mentions légales et la confidentialité](site/mentions-legales.html).

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
| Fonds de carte | Esri (par défaut), OpenStreetMap, IGN | Conditions de chaque fournisseur |

Seules les ventes d'appartements et de maisons sont retenues. Sont écartées celles qui
ne reflètent pas un prix de marché : ventes mêlant logement et local commercial, ventes
de 3 logements ou plus, prix symboliques ou invraisemblables. Une zone de moins de
5 ventes reste grise. Les données et les gares sont vérifiées chaque mois et mises à
jour automatiquement.

À savoir pour lire les prix : le neuf vendu sur plan (VEFA), plus cher, se sépare de
l'ancien par un filtre ; le prix d'une maison comprend son terrain, mais son prix au m²
ne rapporte qu'à la surface habitable ; une vente n'apparaît dans les DVF que plusieurs
mois après sa signature. Les gares à venir distinguent celles en travaux de celles
encore en projet.

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

Les versions installées sont figées, empreinte de chaque paquet comprise, dans
`requirements.txt` et `requirements-dev.txt`. Ces deux fichiers sont produits par
`pip-compile` (Linux, Python de `.python-version`) à partir de `requirements.in` et
`requirements-dev.in`, qui disent ce que le projet accepte ; on ne les modifie pas à la
main (la commande est en tête de `requirements.in`). Sur un autre système,
`pip install -r requirements-dev.in` installe les mêmes outils sans les figer.

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

## Mentions légales et données personnelles

Les mentions légales (éditeur, hébergeur, sources, données personnelles) sont dans
[`site/mentions-legales.html`](site/mentions-legales.html), publiée avec le site ;
l'adresse de contact est `CONTACT`, dans `scripts/construire_site.py`.

Les ventes affichées sont des données personnelles : une personne concernée peut
demander le retrait d'une vente. Pour la retirer :

```bash
python scripts/retirer_vente.py --commune Montreuil --adresse "rue de paris" --date 2024-03
python scripts/retirer_vente.py --commune Montreuil --adresse "12 rue de paris" --date 2024-03-15 --ajouter
```

La première commande liste les ventes trouvées ; la seconde écrit la vente dans
`data/retraits.csv`. Une fois ce fichier fusionné sur `main`, la vente disparaît de la
carte et des statistiques, et le reste aux mises à jour suivantes. Il faut répondre à la
personne sous un mois.

## Maintenance automatique

Le site se tient à jour seul ; il ne demande une intervention qu'en cas d'imprévu, et
le dit alors par une issue (donc un courriel au propriétaire du dépôt).

| Quand | Quoi | Workflow |
|---|---|---|
| le 3 du mois | nouvelles DVF d'Etalab ? fenêtre glissée, site reconstruit, tests, pull request | `donnees.yml` |
| le 10 du mois | gares ouvertes ou annoncées ? même chemin | `reseau.yml` |
| chaque mois | nouvelles versions des actions et des dépendances Python | Dependabot |
| après chaque CI, et chaque jour | fusion des pull requests automatiques vertes, une à la fois | `fusion-auto.yml` |
| chaque lundi | veille : site en ligne, fraîcheur des données, fonds de carte, jeton, pull requests en attente, CI de `main`, workflows réactivés, failles de MapLibre, taille du dépôt, fin de vie de Python | `veille.yml` |

Garde-fous, du premier au dernier rempart :

1. **Qui** : seules les pull requests nées d'une automatisation (Dependabot, données,
   gares, version de Python) sont fusionnées d'office ; jamais une pull request faite à
   la main.
2. **Quoi** : chacune reste dans son périmètre. Les données ne touchent qu'à `data/`,
   les gares qu'à `data/geo/gares.json`, Dependabot qu'à des lignes de version (une
   action épinglée par empreinte, une borne de dépendance). Une ligne de plus — une
   étape de CI, un test, une option de pip — et la fusion attend un humain.
3. **Quand** : une mise à jour de Dependabot attend sept jours, le temps qu'une version
   piégée soit repérée et retirée avant de s'exécuter avec les secrets du dépôt. Les
   workflows n'installent que des versions figées, vérifiées par leur empreinte : aucun
   paquet n'entre autrement. Et le jeton `JETON_MISE_A_JOUR` n'est donné qu'aux étapes
   qui poussent une branche ou ouvrent une pull request, jamais à `pip` ni aux tests.
4. **Données** : une mise à jour qui s'écarte de l'ordinaire (ventes en chute, prix
   révisés à années égales, communes disparues, contours manquants) porte l'étiquette
   `a-verifier` et n'est pas fusionnée ; une issue prévient.
5. **Tests** : tous les tests (Python, navigateur, cohérence Python / JavaScript) doivent
   passer sur la pull request, puis de nouveau sur `main` après la fusion ; une fusion à
   la fois, et seulement si `main` est vert.
6. **Contrôle avant production** : sur `main`, le site est d'abord publié à une adresse
   de contrôle et vérifié tel que Cloudflare le sert (fichiers, en-têtes, et parcours
   dans un vrai navigateur : carte colorée, filtre recalculé, ventes au zoom). Un échec
   arrête tout : la production reste sur la version précédente.
7. **Retour arrière** : si la production échoue malgré tout à la même vérification, le
   dernier déploiement sain est rétabli.

Au pire, donc, le site en ligne reste sur sa dernière bonne version, et une issue le
dit.

### Être prévenu

Tout ce qui demande une action ouvre (ou complète) une issue, au nom de
`github-actions` : un `main` rouge, un contrôle avant production raté, une mise à jour
des données à vérifier ou en échec, une panne de la fusion automatique, et tout ce que
relève la veille du lundi (site injoignable, données figées, fond de carte mort, jeton
proche de l'expiration, pull request qui attend depuis trois semaines). L'issue `veille`
se referme d'elle-même quand tout est rentré dans l'ordre.

Pour recevoir ces issues par courriel : sur le dépôt, *Watch → All Activity* (ou
*Custom → Issues*), et dans *Settings → Notifications* de GitHub, *Watching* coché
pour *Email*, avec une adresse vérifiée.

Ce qui reste hors de portée de l'automatisation, et que la veille signale : le
renouvellement du nom de domaine (à confier au renouvellement automatique du
registraire), le jeton GitHub à recréer à son expiration, un changement d'adresse ou de
format des sources (Etalab, IDFM, fonds de carte), ou la fin d'un service gratuit
(Cloudflare Pages, GitHub Actions).

## Licence

Le code est sous [licence MIT](LICENSE). Les données de `data/` restent sous la licence
de leur source (tableau ci-dessus).
