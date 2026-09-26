"""Ventes individuelles servies au navigateur en **tuiles geographiques**.

La carte ne demande pas ses points a Python. Les ventes d'un territoire —
pres de sept cent mille pour le Grand Paris — sont decoupees une fois pour
toutes en tuiles XYZ ecrites dans `static/`, et le navigateur ne telecharge
que les deux ou trois tuiles qu'il a sous les yeux, au moment ou le zoom
descend vers la section. Aucun clic, aucune reexecution : les points sont
deja la quand l'aplat s'efface.

Trois decisions portent la fluidite de ce format :

1. **Colonnes, pas objets.** Une tuile est un paquet de tableaux paralleles,
   pas une `FeatureCollection`, ou les accolades et les noms de champs repetes
   pesent la moitie du fichier ; ici ils sont ecrits une fois.

2. **Emplacements deja groupes.** Les DVF geolocalisent a la parcelle : deux
   tiers des ventes partagent leur position avec une autre. Le groupement est
   fige a l'ecriture (`off` decoupe les ventes par emplacement), si bien que
   le navigateur n'a jamais de table de hachage a reconstruire — il ne fait
   que parcourir des tranches contigues.

3. **Rien de mis en forme.** Ni HTML, ni couleur, ni libelle : que des
   nombres et deux dictionnaires (types de biens, adresses). Les filtres de
   la barre laterale et l'echelle de couleurs s'appliquent dans le
   navigateur, donc changer un filtre ne retelecharge pas une seule tuile.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from typing import Any

import numpy as np
import pandas as pd

#: Niveau de zoom du decoupage. A 14, une tuile couvre environ 2,5 km : le
#: quartier qu'on a sous les yeux quand les points apparaissent, et jamais
#: plus de quelques centaines de kilo-octets meme au coeur de Paris. Plus fin,
#: on multiplierait les requetes ; plus grossier, la premiere serait lourde.
ZOOM_TUILES = 14

#: Colonnes lues dans les transactions, et nom de la colonne correspondante
#: dans la tuile. Les quatre premieres servent aux filtres de la barre
#: laterale, les autres a l'infobulle — et le prix au m2 aux deux.
COLONNES_NUMERIQUES = {
    "an": "annee",
    "pi": "nb_pieces",
    "su": "surface_bati",
    "va": "valeur_fonciere",
    "m2": "prix_m2",
}


def _numero_tuile(
    longitude: np.ndarray, latitude: np.ndarray, zoom: int
) -> tuple[np.ndarray, np.ndarray]:
    """Coordonnees XYZ de la tuile contenant chaque point (Web Mercator)."""

    cote = 2 ** zoom
    x = np.floor((longitude + 180.0) / 360.0 * cote).astype(np.int64)
    # Mercator : la latitude est repliee par `asinh(tan)`, et bornee a +-85°
    # pour que les rares coordonnees aberrantes ne produisent pas d'infini.
    phi = np.radians(np.clip(latitude, -85.05112878, 85.05112878))
    y = np.floor((1.0 - np.arcsinh(np.tan(phi)) / math.pi) / 2.0 * cote).astype(np.int64)
    return np.clip(x, 0, cote - 1), np.clip(y, 0, cote - 1)


def _entiers(valeurs) -> np.ndarray:
    """Colonne numerique arrondie a l'entier, les trous a -1.

    `-1` plutot que `null` : c'est trois caracteres de moins par trou, et le
    navigateur n'a qu'une comparaison a faire au lieu d'un test de type.
    """

    reels = pd.to_numeric(pd.Series(valeurs), errors="coerce").to_numpy(dtype="float64")
    return np.rint(np.where(np.isfinite(reels), reels, -1.0)).astype(np.int64)


def ecrire_atomiquement(chemin: str, contenu: str) -> None:
    """Ecrit un fichier d'un bloc : on le lit en entier, ou pas du tout.

    Plusieurs visiteurs peuvent demander le meme decoupage au meme moment, et
    le navigateur de l'un peut lire une tuile pendant que la session d'un
    autre l'ecrit. Ecrire a cote puis renommer rend l'operation indivisible :
    `os.replace` substitue le fichier complet, jamais un fichier a moitie
    ecrit.
    """

    dossier = os.path.dirname(chemin) or "."
    descripteur, provisoire = tempfile.mkstemp(dir=dossier, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(descripteur, "w", encoding="utf-8") as flux:
            flux.write(contenu)
        os.replace(provisoire, chemin)
    except BaseException:
        try:
            os.remove(provisoire)
        except OSError:
            pass
        raise


def jeton(*elements: Any) -> str:
    """Empreinte courte et stable d'un jeu de reglages."""

    return hashlib.sha1(repr(elements).encode("utf-8")).hexdigest()[:16]


def nom_tuile(cle: str, x: int, y: int) -> str:
    return f"dvf-pts-{cle}-{x}-{y}.json"


def nom_manifeste(cle: str) -> str:
    return f"dvf-pts-{cle}.json"


def publier(
    transactions: pd.DataFrame,
    cle: str,
    repertoire: str,
    emprise: tuple[float, float, float, float] | None = None,
) -> str:
    """Ecrit les tuiles de ventes et rend le nom du manifeste a servir.

    Le manifeste enumere les tuiles reellement ecrites : sans lui, le
    navigateur demanderait des tuiles vides en bordure de territoire et
    collectionnerait les 404 a chaque deplacement.

    L'ecriture est idempotente : `cle` porte l'empreinte des reglages, donc un
    manifeste deja present signifie que ses tuiles le sont aussi. Tout le
    travail de conversion est fait **une fois pour le territoire entier**, et
    non par tuile : a mille tuiles, un `strftime` ou une factorisation par
    tuile coutent plus cher que l'ecriture elle-meme.

    `emprise` (`lat_min, lon_min, lat_max, lon_max`) ecarte les ventes
    geolocalisees hors du territoire. Les DVF en comptent une centaine pour le
    Grand Paris — des coordonnees a 40° ou 83° de latitude, fautes de saisie
    manifestes — qui se dessineraient sinon en Mediterranee ou au pole Nord.
    Elles restent comptees dans les statistiques de leur section : c'est leur
    point seul qui est faux, pas la vente.
    """

    manifeste = nom_manifeste(cle)
    chemin_manifeste = os.path.join(repertoire, manifeste)
    if os.path.exists(chemin_manifeste):
        return manifeste

    tuiles = _ecrire_tuiles(transactions, cle, repertoire, emprise)
    # Le manifeste en dernier : tant qu'il n'existe pas, aucun navigateur ne
    # demande les tuiles, et sa presence garantit qu'elles sont toutes ecrites.
    ecrire_atomiquement(
        chemin_manifeste,
        json.dumps({"zoom": ZOOM_TUILES, "cle": cle, "tuiles": tuiles}, separators=(",", ":")),
    )
    return manifeste


def dans_emprise(
    longitude, latitude, emprise: tuple[float, float, float, float] | None, marge: float = 0.02
) -> np.ndarray:
    """Masque des positions situees dans l'emprise, a `marge` degres pres."""

    longitude = pd.to_numeric(pd.Series(longitude), errors="coerce").to_numpy(dtype="float64")
    latitude = pd.to_numeric(pd.Series(latitude), errors="coerce").to_numpy(dtype="float64")
    valides = np.isfinite(longitude) & np.isfinite(latitude)
    if emprise is None:
        return valides
    lat_min, lon_min, lat_max, lon_max = emprise
    with np.errstate(invalid="ignore"):
        return (
            valides
            & (latitude >= lat_min - marge)
            & (latitude <= lat_max + marge)
            & (longitude >= lon_min - marge)
            & (longitude <= lon_max + marge)
        )


def _ecrire_tuiles(
    transactions: pd.DataFrame,
    cle: str,
    repertoire: str,
    emprise: tuple[float, float, float, float] | None = None,
) -> list[str]:
    ventes = transactions.dropna(subset=["longitude", "latitude"])
    ventes = ventes[dans_emprise(ventes["longitude"], ventes["latitude"], emprise)]
    if ventes.empty:
        return []

    longitude = np.round(ventes["longitude"].to_numpy(dtype="float64"), 5)
    latitude = np.round(ventes["latitude"].to_numpy(dtype="float64"), 5)
    x, y = _numero_tuile(longitude, latitude, ZOOM_TUILES)

    # Un seul tri donne les deux groupements : les tranches de tuiles, et a
    # l'interieur de chacune les tranches d'emplacements. Tout le reste du
    # module ne fait que decouper selon ces tranches.
    ordre = np.lexsort((latitude, longitude, y, x))
    longitude, latitude, x, y = longitude[ordre], latitude[ordre], x[ordre], y[ordre]

    colonnes = {
        court: _entiers(ventes[long].to_numpy())[ordre]
        for court, long in COLONNES_NUMERIQUES.items()
        if long in ventes.columns
    }
    # Date en `AAAAMMJJ` : un entier se compare, se trie et s'ecrit en huit
    # caracteres. `strftime` sur sept cent mille lignes couterait a lui seul
    # plus que l'ecriture de toutes les tuiles.
    dates = pd.to_datetime(ventes["date_mutation"], errors="coerce")
    valides = dates.notna().to_numpy()
    jours = np.where(
        valides,
        dates.dt.year.fillna(0).to_numpy(dtype="int64") * 10000
        + dates.dt.month.fillna(0).to_numpy(dtype="int64") * 100
        + dates.dt.day.fillna(0).to_numpy(dtype="int64"),
        -1,
    )
    colonnes["dt"] = jours[ordre]

    # Factorisation globale : les memes types et les memes adresses reviennent
    # d'une tuile a l'autre, et `pd.factorize` sur la colonne entiere coute
    # moins qu'un `Categorical` par tuile.
    dictionnaires = {}
    for court, long in (("ty", "type_bien"), ("ad", "adresse")):
        if long not in ventes.columns:
            continue
        codes, libelles = pd.factorize(ventes[long].fillna("").astype(str), sort=False)
        colonnes[court] = codes.astype(np.int64)[ordre]
        dictionnaires[court] = np.asarray(libelles, dtype=object)

    coupures = np.flatnonzero((np.diff(x) != 0) | (np.diff(y) != 0)) + 1
    debuts = np.concatenate(([0], coupures))
    fins = np.concatenate((coupures, [len(x)]))

    noms: list[str] = []
    for debut, fin in zip(debuts, fins):
        nom = nom_tuile(cle, int(x[debut]), int(y[debut]))
        _ecrire_tuile(
            os.path.join(repertoire, nom),
            longitude[debut:fin],
            latitude[debut:fin],
            {court: valeurs[debut:fin] for court, valeurs in colonnes.items()},
            dictionnaires,
        )
        noms.append(f"{int(x[debut])}/{int(y[debut])}")
    return noms


def _ecrire_tuile(
    chemin: str,
    longitude: np.ndarray,
    latitude: np.ndarray,
    colonnes: dict[str, np.ndarray],
    dictionnaires: dict[str, np.ndarray],
) -> None:
    """Une tuile : ses emplacements, et les ventes de chacun.

    Les ventes arrivent triees par position : les emplacements sont donc des
    tranches contigues, reperees par leurs seules ruptures. `off` porte les
    bornes de ces tranches — `off[i]` a `off[i+1]` sont les ventes de
    l'emplacement `i`.
    """

    rupture = np.flatnonzero((np.diff(longitude) != 0) | (np.diff(latitude) != 0)) + 1
    debuts = np.concatenate(([0], rupture))

    charge = {
        "lon": longitude[debuts].tolist(),
        "lat": latitude[debuts].tolist(),
        "off": np.concatenate((debuts, [len(longitude)])).tolist(),
    }
    for court, valeurs in colonnes.items():
        if court in dictionnaires:
            # Dictionnaire local : une tuile ne connait qu'une poignee
            # d'adresses, et les indices tiennent alors sur deux chiffres.
            libelles, locaux = np.unique(valeurs, return_inverse=True)
            charge[court] = locaux.tolist()
            charge[court + "_l"] = [str(v) for v in dictionnaires[court][libelles]]
        else:
            charge[court] = valeurs.tolist()

    # `json.dumps` et non `json.dump` : seul le premier passe par l'encodeur C.
    # Sur un millier de tuiles, l'ecart se compte en dizaines de secondes.
    ecrire_atomiquement(chemin, json.dumps(charge, ensure_ascii=False, separators=(",", ":")))
