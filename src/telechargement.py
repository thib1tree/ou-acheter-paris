"""Recuperation des DVF geolocalisees, fichier annuel par fichier annuel.

Etalab republie les DVF de la DGFiP **geolocalisees et deja mises en forme**,
en un fichier par annee pour la France entiere (voir `src/millesime.py`) :

    https://static.data.gouv.fr/resources/demandes-de-valeurs-foncieres-geolocalisees/<horodatage>/full-2025.csv.gz

C'est la source qui evite les deux impasses des autres :

* l'export section par section de `app.dvf.etalab.gouv.fr` demande un clic par
  section cadastrale — inexploitable au-dela d'une poignee de communes ;
* le fichier annuel brut de la DGFiP (`ValeursFoncieres-{annee}.txt`, ~500 Mo)
  couvre lui aussi toute la France, mais **sans coordonnees ni identifiant de
  mutation** : il faudrait geocoder soi-meme et deviner le regroupement des
  lignes en ventes. Le millesime geolocalise fait deja ce travail, et il est
  dans le schema exact que `src/ingestion.py` sait lire.

Un fichier annuel pese 100 a 150 Mo compresse ; il est telecharge une fois,
verifie par son empreinte SHA-1, puis lu par blocs en ne gardant que les
communes du territoire. (Jusqu'en avril 2026, Etalab decoupait aussi chaque
annee par departement ; cette arborescence est archivee.)
"""

from __future__ import annotations

import gzip
import hashlib
import os
from typing import Iterable, Iterator

import pandas as pd

try:  # pragma: no cover - dependance optionnelle, comme dans src/geo.py
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

#: Table d'appartenance geographique de l'INSEE, qui porte le rattachement de
#: chaque commune a son unite urbaine. C'est la seule source de la liste des
#: communes du Grand Paris ; elle n'est pas derivable des decoupages
#: administratifs (une unite urbaine est une notion morphologique, pas
#: institutionnelle).
URL_UNITES_URBAINES = "https://www.insee.fr/fr/statistiques/fichier/2028028/table-appartenance-geo-communes-25.zip"

#: Code INSEE de l'unite urbaine de Paris (millesime 2020).
CODE_UU_PARIS = "00851"

DELAI_RESEAU = 120  # secondes, entre deux paquets recus.

ENTETES_HTTP = {"User-Agent": "cartographie-dvf/1.0 (mise a jour des donnees)"}

#: Lecture par blocs : un fichier annuel compte quelque 4 millions de lignes,
#: dont une sur dix pour le Grand Paris. On filtre au fil de l'eau plutot que
#: de tout charger puis reduire.
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


def chemin_periode(periode: str) -> str:
    """Fichier en cache d'une periode (`2025`, `2026-s1`)."""

    return os.path.join(repertoire_cache_dvf(), f"full-{periode}.csv.gz")


def _sha1(chemin: str) -> str:
    empreinte = hashlib.sha1()
    with open(chemin, "rb") as flux:
        for paquet in iter(lambda: flux.read(1 << 20), b""):
            empreinte.update(paquet)
    return empreinte.hexdigest()


def telecharger_periode(periode: str, fichier: dict, forcer: bool = False) -> str:
    """Telecharge le fichier d'une periode et renvoie son chemin en cache.

    `fichier` est l'entree de `millesime.EtatDistant.fichiers` : adresse,
    taille, empreinte SHA-1. Un fichier deja en cache qui n'a plus l'empreinte
    publiee (Etalab l'a republie) est retelecharge.

    Le fichier est ecrit en flux : rien ne transite par la memoire, et un
    telechargement interrompu ou altere ne laisse pas de fichier en cache
    (ecriture dans un `.partiel` renomme seulement une fois complet et verifie).
    """

    destination = chemin_periode(periode)
    attendue = fichier.get("sha1")
    if os.path.exists(destination) and not forcer and (not attendue or _sha1(destination) == attendue):
        return destination
    if requests is None:
        raise TelechargementImpossible(
            "Module `requests` absent : impossible de telecharger les DVF."
        )

    url = fichier.get("url")
    if not url:
        raise TelechargementImpossible(f"{periode} : aucune adresse de fichier")
    partiel = destination + ".partiel"
    try:
        empreinte = hashlib.sha1()
        with requests.get(
            url, timeout=DELAI_RESEAU, headers=ENTETES_HTTP, stream=True
        ) as reponse:
            reponse.raise_for_status()
            with open(partiel, "wb") as flux:
                for paquet in reponse.iter_content(chunk_size=1 << 20):
                    empreinte.update(paquet)
                    flux.write(paquet)
        if attendue and empreinte.hexdigest() != attendue:
            raise ValueError(f"empreinte SHA-1 {empreinte.hexdigest()}, {attendue} attendue")
        os.replace(partiel, destination)
    except Exception as erreur:  # noqa: BLE001 - message lisible plutot qu'une trace
        if os.path.exists(partiel):
            os.remove(partiel)
        raise TelechargementImpossible(
            f"{periode} : {type(erreur).__name__} — {erreur} ({url})"
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


def lignes_periode(
    periode: str,
    fichier: dict | None = None,
    codes_communes: Iterable[str] | None = None,
    forcer: bool = False,
) -> pd.DataFrame:
    """Lignes DVF d'une periode, restreintes aux communes demandees.

    Sans `fichier`, la periode doit deja etre en cache.

    Les lignes sans `type_local` decrivent des parcelles nues (terres, sols,
    terrains a batir). Elles sont ecartees des la lecture : le pipeline ne s'en
    sert jamais — la valeur fonciere qu'elles portent est celle de la mutation,
    repetee a l'identique sur les lignes de local — et elles representent plus
    de la moitie du volume.
    """

    chemin = telecharger_periode(periode, fichier, forcer=forcer) if fichier else chemin_periode(periode)
    if not os.path.exists(chemin):
        raise TelechargementImpossible(f"{periode} : fichier absent du cache ({chemin})")
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
    return typer_colonnes_dvf(lignes, os.path.basename(chemin))
