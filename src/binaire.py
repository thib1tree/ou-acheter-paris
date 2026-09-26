"""Les ventes du territoire en colonnes binaires, pour le calcul dans le navigateur.

Le site statique n'a pas de serveur pour recalculer les statistiques quand un
filtre change : c'est le navigateur qui les refait (`site/calcul.js`), dans un
Web Worker, a partir de ce fichier. Il porte, pour chaque vente, exactement
ce qu'il faut pour rejouer `stats.filtrer`, `stats.statistiques_sections` et
`stats.rendement_sections` — et rien d'autre.

Trois decisions en font un fichier de 3 Mo compresse pour 650 000 ventes :

1. **Des colonnes d'entiers.** La valeur fonciere et la surface sont coupees en
   partie entiere et centiemes : `(entier * 100 + centiemes) / 100` redonne,
   en virgule flottante, *exactement* le nombre lu dans le parquet — la
   division est correctement arrondie, comme la lecture d'un decimal. Le prix
   au m² n'est pas stocke : `round(valeur / surface)`, arrondi au pair comme
   `numpy.round`, le redonne a l'identique (l'ecriture le verifie).
2. **Un ordre qui evite les tris.** Les ventes sont rangees par commune puis
   par prix au m² croissant. Filtrer conserve cet ordre : la mediane au m²
   d'une commune se lit alors sans rien trier, et celle d'une section apres
   une simple repartition stable par section (tri par comptage, lineaire).
3. **Des index, pas des codes.** Section, annee et type sont des entiers ; les
   codes et les titres des zones sont ecrits une fois, dans les metadonnees.

Les colonnes sont ecrites les plus larges d'abord, en petit-boutiste : chaque
colonne commence a une adresse multiple de sa largeur, et le navigateur les lit
sans copie (`new Uint32Array(tampon, debut, n)`).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.stats import CLE_COMMUNE, CLE_SECTION

#: Index de section reserve aux ventes sans section (aucune dans les donnees
#: actuelles : le cas est prevu, pas observe).
SANS_SECTION = 0xFFFF

#: Version du format : le navigateur refuse un fichier qu'il ne sait pas lire.
VERSION = 1


class FormatImpossible(ValueError):
    """Une valeur ne tient pas dans le format : mieux vaut echouer que tronquer."""


def _parties(valeurs: np.ndarray, nom: str, maximum: int) -> tuple[np.ndarray, np.ndarray]:
    """Partie entiere et centiemes, verifies pour redonner la valeur exacte."""

    entiers = np.floor(valeurs)
    centiemes = np.round((valeurs - entiers) * 100)
    if np.any(entiers < 0) or np.any(entiers > maximum):
        raise FormatImpossible(f"{nom} hors de [0, {maximum}]")
    if not np.array_equal((entiers * 100 + centiemes) / 100, valeurs):
        raise FormatImpossible(f"{nom} : une valeur a plus de deux decimales")
    return entiers, centiemes


def encoder_ventes(
    transactions: pd.DataFrame, libelles: dict[str, str]
) -> tuple[bytes, dict]:
    """Rend le fichier binaire et ses metadonnees (a ecrire en JSON a cote).

    `libelles` : titre de chaque zone (`charge.libelles_zones`).
    """

    ventes = transactions[
        [CLE_COMMUNE, CLE_SECTION, "annee", "type_bien", "valeur_fonciere", "surface_bati", "prix_m2"]
    ].copy()
    ventes = ventes.sort_values([CLE_COMMUNE, "prix_m2"], kind="stable").reset_index(drop=True)

    communes = sorted(str(c) for c in ventes[CLE_COMMUNE].dropna().unique())
    sections = sorted(str(s) for s in ventes[CLE_SECTION].dropna().unique())
    if ventes[CLE_COMMUNE].isna().any():
        raise FormatImpossible("vente sans commune")
    if len(sections) >= SANS_SECTION:
        raise FormatImpossible("trop de sections pour un index sur 16 bits")

    index_commune = pd.Index(communes).get_indexer(ventes[CLE_COMMUNE].astype(str))
    index_section = pd.Index(sections).get_indexer(ventes[CLE_SECTION].astype("string").fillna(""))
    index_section = np.where(index_section < 0, SANS_SECTION, index_section)
    bornes_communes = np.searchsorted(index_commune, np.arange(len(communes) + 1)).astype("<u4")

    annees = ventes["annee"].astype(int).to_numpy()
    annee0 = int(annees.min())
    if annees.max() - annee0 > 255:
        raise FormatImpossible("plus de 256 millesimes")
    types = sorted(str(t) for t in ventes["type_bien"].unique())

    valeur = ventes["valeur_fonciere"].to_numpy(dtype=float)
    surface = ventes["surface_bati"].to_numpy(dtype=float)
    euros, centimes = _parties(valeur, "valeur fonciere", 2**32 - 1)
    metres, centiemes = _parties(surface, "surface", 2**16 - 1)
    if not np.array_equal(np.round(valeur / surface), ventes["prix_m2"].to_numpy(dtype=float)):
        raise FormatImpossible("le prix au m² ne se recalcule pas a l'identique")

    colonnes = [
        ("communes", bornes_communes),
        ("euros", euros.astype("<u4")),
        ("section", index_section.astype("<u2")),
        ("metres", metres.astype("<u2")),
        ("annee", (annees - annee0).astype("u1")),
        ("type", pd.Index(types).get_indexer(ventes["type_bien"].astype(str)).astype("u1")),
        ("centimes", centimes.astype("u1")),
        ("centiemes", centiemes.astype("u1")),
    ]
    disposition = []
    debut = 0
    for nom, valeurs in colonnes:
        disposition.append({"nom": nom, "type": valeurs.dtype.str.lstrip("<|"), "debut": debut, "n": len(valeurs)})
        debut += valeurs.nbytes
    contenu = b"".join(valeurs.tobytes() for _, valeurs in colonnes)

    meta = {
        "version": VERSION,
        "n": len(ventes),
        "annee0": annee0,
        "types": types,
        "colonnes": disposition,
        "communes": {"codes": communes, "entetes": [libelles.get(c, c) for c in communes]},
        "sections": {"codes": sections, "entetes": [libelles.get(s, s) for s in sections]},
    }
    return contenu, meta
