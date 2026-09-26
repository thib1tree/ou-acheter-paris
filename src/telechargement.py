"""Recuperation des DVF geolocalisees, departement par departement.

Etalab republie les DVF de la DGFiP **geolocalisees et deja mises en forme**,
decoupees par annee et par departement :

    https://files.data.gouv.fr/geo-dvf/latest/csv/{annee}/departements/{dep}.csv.gz

C'est la source qui evite les deux impasses des autres :

* l'export section par section de `app.dvf.etalab.gouv.fr` demande un clic par
  section cadastrale — inexploitable au-dela d'une poignee de communes ;
* le fichier annuel brut de la DGFiP (`ValeursFoncieres-{annee}.txt`, ~500 Mo)
  couvre toute la France d'un bloc, mais **sans coordonnees ni identifiant de
  mutation** : il faudrait geocoder soi-meme et deviner le regroupement des
  lignes en ventes. Le millesime geolocalise fait deja ce travail, et il est
  dans le schema exact que `src/ingestion.py` sait lire.

Un departement-annee pese 0,4 a 2,3 Mo compresse : couvrir le Grand Paris sur
six millesimes represente une cinquantaine de Mo, contre 3,5 Go pour le
national.
"""

from __future__ import annotations

import gzip
import os
import shutil
from typing import Iterable, Iterator

import pandas as pd

try:  # pragma: no cover - dependance optionnelle, comme dans src/geo.py
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

#: Fichiers annuels par departement, millesime courant.
URL_DVF_DEPARTEMENT = (
    "https://files.data.gouv.fr/geo-dvf/latest/csv/{annee}/departements/{departement}.csv.gz"
)

#: Table d'appartenance geographique de l'INSEE, qui porte le rattachement de
#: chaque commune a son unite urbaine. C'est la seule source de la liste des
#: communes du Grand Paris ; elle n'est pas derivable des decoupages
#: administratifs (une unite urbaine est une notion morphologique, pas
#: institutionnelle).
URL_UNITES_URBAINES = "https://www.insee.fr/fr/statistiques/fichier/2028028/table-appartenance-geo-communes-25.zip"

#: Code INSEE de l'unite urbaine de Paris (millesime 2020).
CODE_UU_PARIS = "00851"

DELAI_RESEAU = 120  # secondes : un departement-annee peut peser quelques Mo.

ENTETES_HTTP = {"User-Agent": "cartographie-dvf/1.0 (+https://files.data.gouv.fr/geo-dvf/)"}

#: Lecture par blocs : un departement dense sur une annee tient largement en
#: memoire, mais huit departements sur six millesimes, non. On filtre au fil de
#: l'eau plutot que de tout charger puis reduire.
TAILLE_BLOC = 200_000

#: Colonnes effectivement utilisees par le pipeline. Les DVF geolocalisees en
#: comptent 40 : n'en lire que la moitie divise d'autant l'empreinte memoire.
COLONNES_UTILES: tuple[str, ...] = (
    "id_mutation",
    "date_mutation",
    "nature_mutation",
    "valeur_fonciere",
    "adresse_numero",
    "adresse_suffixe",
    "adresse_nom_voie",
    "code_postal",
    "code_commune",
    "nom_commune",
    "code_departement",
    "id_parcelle",
    "section_prefixe",
    "lot1_surface_carrez",
    "lot2_surface_carrez",
    "lot3_surface_carrez",
    "lot4_surface_carrez",
    "lot5_surface_carrez",
    "nombre_lots",
    "code_type_local",
    "type_local",
    "surface_reelle_bati",
    "nombre_pieces_principales",
    "surface_terrain",
    "longitude",
    "latitude",
)


class TelechargementImpossible(RuntimeError):
    """Le fichier n'a pas pu etre recupere (reseau, quota, millesime absent)."""


def repertoire_cache_dvf() -> str:
    """Cache des fichiers telecharges, surchargeable via `DVF_CACHE_DIR`."""

    racine = os.environ.get("DVF_CACHE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache"
    )
    chemin = os.path.join(racine, "dvf")
    os.makedirs(chemin, exist_ok=True)
    return chemin


def chemin_departement(departement: str, annee: int) -> str:
    return os.path.join(repertoire_cache_dvf(), f"{annee}-{departement}.csv.gz")


def telecharger_departement(
    departement: str, annee: int, forcer: bool = False
) -> str:
    """Telecharge un departement-annee et renvoie le chemin du fichier en cache.

    Le fichier est ecrit en flux : rien ne transite par la memoire, et un
    telechargement interrompu ne laisse pas de fichier tronque en cache (ecriture
    dans un `.partiel` renomme seulement une fois complet).
    """

    destination = chemin_departement(departement, annee)
    if os.path.exists(destination) and not forcer:
        return destination
    if requests is None:
        raise TelechargementImpossible(
            "Module `requests` absent : impossible de telecharger les DVF."
        )

    url = URL_DVF_DEPARTEMENT.format(annee=annee, departement=departement)
    partiel = destination + ".partiel"
    try:
        with requests.get(
            url, timeout=DELAI_RESEAU, headers=ENTETES_HTTP, stream=True
        ) as reponse:
            reponse.raise_for_status()
            with open(partiel, "wb") as flux:
                shutil.copyfileobj(reponse.raw, flux)
        os.replace(partiel, destination)
    except Exception as erreur:  # noqa: BLE001 - message lisible plutot qu'une trace
        if os.path.exists(partiel):
            os.remove(partiel)
        raise TelechargementImpossible(
            f"{departement} / {annee} : {type(erreur).__name__} — {erreur} ({url})"
        ) from erreur
    return destination


def _blocs(chemin: str) -> Iterator[pd.DataFrame]:
    """Lit un CSV DVF gzippe par blocs, en ne gardant que les colonnes utiles."""

    ouvrir = gzip.open if chemin.endswith(".gz") else open
    with ouvrir(chemin, "rt", encoding="utf-8-sig", newline="") as flux:
        ligne_entete = flux.readline().rstrip("\r\n")

    # Etalab publie en virgule, mais le meme schema circule aussi en
    # point-virgule (exports de `app.dvf.etalab.gouv.fr`, retraitements tiers).
    # Deviner coute une ligne et evite un fichier lu en une seule colonne.
    separateur = max(",;\t|", key=ligne_entete.count)
    entete = [c.strip().strip('"').lower() for c in ligne_entete.split(separateur)]
    colonnes = [c for c in entete if c in COLONNES_UTILES]

    for bloc in pd.read_csv(
        chemin,
        sep=separateur,
        dtype=str,
        keep_default_na=False,
        na_values=["", "None", "nan", "NaN", "NULL"],
        usecols=lambda nom: str(nom).strip().strip('"').lower() in set(colonnes),
        chunksize=TAILLE_BLOC,
        compression="infer",
        encoding="utf-8-sig",
        low_memory=False,
    ):
        yield bloc


def lignes_departement(
    departement: str,
    annee: int,
    codes_communes: Iterable[str] | None = None,
    forcer: bool = False,
) -> pd.DataFrame:
    """Lignes DVF d'un departement-annee, restreintes aux communes demandees.

    Les lignes sans `type_local` decrivent des parcelles nues (terres, sols,
    terrains a batir). Elles sont ecartees des la lecture : le pipeline ne s'en
    sert jamais — la valeur fonciere qu'elles portent est celle de la mutation,
    repetee a l'identique sur les lignes de local — et elles representent plus
    de la moitie du volume.
    """

    chemin = telecharger_departement(departement, annee, forcer=forcer)
    perimetre = {str(c).zfill(5) for c in codes_communes} if codes_communes else None

    morceaux: list[pd.DataFrame] = []
    for bloc in _blocs(chemin):
        bloc.columns = [str(col).strip().lower() for col in bloc.columns]
        if "code_commune" not in bloc.columns:
            continue
        bloc["code_commune"] = bloc["code_commune"].astype("string").str.strip().str.zfill(5)
        if perimetre is not None:
            bloc = bloc[bloc["code_commune"].isin(perimetre)]
        if "type_local" in bloc.columns:
            bloc = bloc[bloc["type_local"].notna()]
        if not bloc.empty:
            morceaux.append(bloc)

    if not morceaux:
        return pd.DataFrame(columns=list(COLONNES_UTILES))

    from .ingestion import typer_colonnes_dvf

    lignes = pd.concat(morceaux, ignore_index=True)
    return typer_colonnes_dvf(lignes, f"{annee}-{departement}.csv.gz")


def annees_disponibles(departement: str, candidates: Iterable[int]) -> list[int]:
    """Millesimes reellement publies pour un departement.

    Le millesime de l'annee en cours n'apparait qu'a la premiere publication
    semestrielle : interroger plutot que supposer evite un echec au demarrage
    chaque 1er janvier.
    """

    if requests is None:
        return []
    disponibles: list[int] = []
    for annee in sorted(candidates):
        if os.path.exists(chemin_departement(departement, annee)):
            disponibles.append(annee)
            continue
        url = URL_DVF_DEPARTEMENT.format(annee=annee, departement=departement)
        try:
            reponse = requests.head(url, timeout=DELAI_RESEAU, headers=ENTETES_HTTP)
            if reponse.status_code < 400:
                disponibles.append(annee)
        except Exception:  # noqa: BLE001 - une sonde qui echoue n'est pas bloquante
            continue
    return disponibles
