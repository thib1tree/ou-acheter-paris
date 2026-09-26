"""Decoupage territorial : regions administratives et exception « Grand Paris ».

Ce module repond a une seule question — *quelles communes regarde-t-on ?* — et
il y repond **sans aucun appel reseau** : le referentiel des 35 011 communes
francaises est versionne dans `data/territoires/communes.csv.gz` (407 Ko), issu
de `@etalab/decoupage-administratif`.

Deux subtilites que le referentiel brut ne gere pas et que ce module tranche :

* **Paris, Lyon et Marseille.** Les DVF ventilent leurs mutations par
  arrondissement (`75101`), jamais par commune (`75056`). Le referentiel ne
  retient donc que les arrondissements municipaux et ecarte leur commune mere.
* **Le Grand Paris n'est pas une region.** C'est le territoire par defaut de
  l'application, defini par une liste explicite de communes
  (`data/territoires/grand-paris.txt`) plutot que par un code region.

Enfin, les DVF ne couvrent **ni l'Alsace-Moselle ni Mayotte** : le droit local
y confie la publicite fonciere au livre foncier, dont les donnees ne sont pas
ouvertes. Un territoire qui les contient le signale de lui-meme
(`departements_sans_dvf`), plutot que d'afficher une carte vide sans explication.
"""

from __future__ import annotations

import csv
import gzip
import os
from dataclasses import dataclass
from functools import lru_cache

import pandas as pd

#: Cle du territoire par defaut. Ce n'est pas une region administrative : c'est
#: l'exception assumee du projet, et le seul territoire dont les donnees sont
#: precalculees dans le depot.
CLE_GRAND_PARIS = "grand-paris"

#: Departements ou la DGFiP ne dispose pas des mutations : le livre foncier de
#: droit local (Alsace-Moselle) et Mayotte n'alimentent pas les DVF.
DEPARTEMENTS_SANS_DVF: frozenset[str] = frozenset({"57", "67", "68", "976"})

#: Collectivites d'outre-mer : hors champ des DVF et du cadastre Etalab, elles
#: n'ont pas de code region a deux chiffres. Les proposer afficherait des
#: territoires systematiquement vides.
_PREFIXE_REGION_COM = 3


def racine_projet() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repertoire_territoires() -> str:
    """Repertoire du referentiel, surchargeable via `DVF_TERRITOIRES_DIR`."""

    return os.environ.get("DVF_TERRITOIRES_DIR") or os.path.join(
        racine_projet(), "data", "territoires"
    )


def fichier_communes() -> str:
    return os.path.join(repertoire_territoires(), "communes.csv.gz")


def fichier_grand_paris() -> str:
    return os.path.join(repertoire_territoires(), "grand-paris.txt")


@dataclass(frozen=True)
class Territoire:
    """Un perimetre selectionnable dans la barre laterale."""

    cle: str
    nom: str
    #: Codes INSEE des communes du territoire (arrondissements pour PLM).
    codes_communes: frozenset[str]
    #: Departements couverts : c'est la maille de telechargement des DVF
    #: geolocalisees (un fichier par departement et par annee).
    departements: tuple[str, ...]
    population: int
    #: Vrai pour le Grand Paris : un territoire defini par une liste de
    #: communes et non par un code region.
    exception: bool = False

    @property
    def nb_communes(self) -> int:
        return len(self.codes_communes)

    @property
    def departements_sans_dvf(self) -> tuple[str, ...]:
        return tuple(d for d in self.departements if d in DEPARTEMENTS_SANS_DVF)

    def contient(self, code_commune: str) -> bool:
        return str(code_commune).zfill(5) in self.codes_communes


# --------------------------------------------------------------------------
# Referentiel des communes
# --------------------------------------------------------------------------


@lru_cache(maxsize=2)
def referentiel_communes(chemin: str | None = None) -> pd.DataFrame:
    """Les communes francaises, avec leur departement et leur region.

    Le fichier est petit et lu une seule fois par processus.
    """

    chemin = chemin or fichier_communes()
    if not os.path.exists(chemin):
        raise FileNotFoundError(
            f"Referentiel des communes absent : {chemin}. Il est versionne dans le "
            "depot ; regenerez-le avec `python scripts/preparer_territoire.py "
            "--referentiel`."
        )
    with gzip.open(chemin, "rt", encoding="utf-8", newline="") as flux:
        table = pd.DataFrame(list(csv.DictReader(flux, delimiter=";")))
    table["code_commune"] = table["code_commune"].str.zfill(5)
    table["population"] = pd.to_numeric(table["population"], errors="coerce").fillna(0).astype(int)
    return table


@lru_cache(maxsize=2)
def _codes_grand_paris(chemin: str | None = None) -> frozenset[str]:
    """Communes du Grand Paris, lues dans le fichier versionne.

    Format : un code INSEE par ligne, suivi facultativement d'une tabulation et
    du nom de la commune (lisibilite d'une revue de diff). Les lignes vides et
    les commentaires `#` sont ignores.
    """

    chemin = chemin or fichier_grand_paris()
    if not os.path.exists(chemin):
        raise FileNotFoundError(
            f"Liste des communes du Grand Paris absente : {chemin}. Elle est "
            "versionnee dans le depot ; regenerez-la avec "
            "`python scripts/preparer_territoire.py --grand-paris`."
        )
    codes: set[str] = set()
    with open(chemin, "r", encoding="utf-8") as flux:
        for ligne in flux:
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#"):
                continue
            codes.add(ligne.split("\t")[0].split(";")[0].strip().zfill(5))
    return frozenset(codes)


def _depuis_communes(
    cle: str, nom: str, table: pd.DataFrame, exception: bool = False
) -> Territoire:
    codes = frozenset(table["code_commune"])
    return Territoire(
        cle=cle,
        nom=nom,
        codes_communes=codes,
        departements=tuple(sorted(set(table["code_departement"]))),
        population=int(table["population"].sum()),
        exception=exception,
    )


@lru_cache(maxsize=1)
def territoires() -> tuple[Territoire, ...]:
    """Territoires proposes, le Grand Paris en tete.

    L'ordre est celui de la barre laterale : le territoire par defaut d'abord,
    puis les regions par ordre alphabetique. Les collectivites d'outre-mer sont
    ecartees — elles sont hors champ des DVF comme du cadastre Etalab.
    """

    table = referentiel_communes()
    liste: list[Territoire] = []

    grand_paris = table[table["code_commune"].isin(_codes_grand_paris())]
    liste.append(
        _depuis_communes(CLE_GRAND_PARIS, "Grand Paris", grand_paris, exception=True)
    )

    metropole_et_drom = table[table["code_region"].str.len() < _PREFIXE_REGION_COM]
    for (code_region, nom_region), communes in sorted(
        metropole_et_drom.groupby(["code_region", "nom_region"]), key=lambda item: item[0][1]
    ):
        liste.append(_depuis_communes(f"region-{code_region}", nom_region, communes))

    return tuple(liste)


def territoire(cle: str) -> Territoire:
    """Territoire correspondant a une cle, le Grand Paris par defaut."""

    for candidat in territoires():
        if candidat.cle == cle:
            return candidat
    return territoires()[0]


def communes_du_territoire(cle: str) -> pd.DataFrame:
    """Lignes du referentiel appartenant au territoire, triees par nom."""

    perimetre = territoire(cle)
    table = referentiel_communes()
    retenues = table[table["code_commune"].isin(perimetre.codes_communes)]
    return retenues.sort_values("nom_commune").reset_index(drop=True)


def departements_du_territoire(cle: str) -> tuple[str, ...]:
    """Departements a telecharger pour couvrir le territoire."""

    return territoire(cle).departements
