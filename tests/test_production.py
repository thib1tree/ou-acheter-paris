"""Garanties d'un site public : couleurs exactes, donnees aberrantes, cadrage,
et reglages fixes une fois pour toutes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import charge as donnees_carte  # noqa: E402
from src import geo, points  # noqa: E402
from src.stats import (  # noqa: E402
    GRIS_DONNEES_INSUFFISANTES,
    VOLUME_MIN_ANNUEL,
    EchelleCouleur,
    couleur_rendement,
    couleurs_rendement,
    rendement_sections,
)

RACINE = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Couleurs vectorisees : meme resultat que l'appel valeur par valeur
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "echelle",
    [EchelleCouleur(bas=2_000.0, haut=12_000.0), EchelleCouleur(bas=5.0, haut=5.0),
     EchelleCouleur(bas=0.0, haut=1.0, vide=True)],
)
def test_la_coloration_par_colonne_rend_les_couleurs_d_une_valeur_a_la_fois(echelle):
    valeurs = np.concatenate([np.linspace(-1_000, 15_000, 4_001), [np.nan]])
    assert echelle.couleurs(valeurs) == [echelle.couleur(v) for v in valeurs]


def test_l_evolution_par_colonne_rend_les_couleurs_d_une_valeur_a_la_fois():
    taux = np.concatenate([np.linspace(-30, 30, 2_001), [np.nan, None]])
    assert couleurs_rendement(taux, 5.0) == [couleur_rendement(t, 5.0) for t in taux]
    assert couleurs_rendement([1.0], 0.0) == [GRIS_DONNEES_INSUFFISANTES]


# --------------------------------------------------------------------------
# Ventes aux coordonnees aberrantes
# --------------------------------------------------------------------------


def _ventes(positions: list[tuple[float, float]]) -> pd.DataFrame:
    n = len(positions)
    return pd.DataFrame(
        {
            "longitude": [p[0] for p in positions],
            "latitude": [p[1] for p in positions],
            "annee": [2024] * n,
            "nb_pieces": [2] * n,
            "surface_bati": [50.0] * n,
            "valeur_fonciere": [500_000.0] * n,
            "prix_m2": [10_000.0] * n,
            "date_mutation": pd.to_datetime(["2024-05-02"] * n),
            "type_bien": ["Appartement"] * n,
            "adresse": ["1 Rue de Rivoli"] * n,
        }
    )


def test_une_vente_geolocalisee_hors_du_territoire_n_est_pas_dessinee(tmp_path):
    """Les DVF placent une centaine de ventes du Grand Paris a 40° ou 83° nord."""

    emprise = (48.4, 1.6, 49.2, 2.9)  # lat_min, lon_min, lat_max, lon_max
    ventes = _ventes([(2.35, 48.85), (0.27, 83.51), (2.29, 40.88)])

    manifeste = points.publier(ventes, "essai", str(tmp_path), emprise)
    tuiles = json.loads((tmp_path / manifeste).read_text(encoding="utf-8"))["tuiles"]

    assert len(tuiles) == 1
    x, y = (int(v) for v in tuiles[0].split("/"))
    tuile = json.loads((tmp_path / points.nom_tuile("essai", x, y)).read_text(encoding="utf-8"))
    assert tuile["lon"] == [2.35]
    # Aucun fichier provisoire ne reste derriere les ecritures atomiques.
    assert not list(tmp_path.glob(".tmp-*"))


def test_le_cadrage_d_ouverture_ignore_les_coordonnees_aberrantes():
    boite = (48.4, 1.6, 49.2, 2.9)
    lon = np.linspace(2.2, 2.5, 500)
    lat = np.linspace(48.8, 48.9, 500)
    ventes = pd.DataFrame(
        {"longitude": np.append(lon, [0.27, 2.29]), "latitude": np.append(lat, [83.5, 40.9])}
    )

    ouest, sud, est, nord = donnees_carte.cadrage_ventes(ventes, boite)

    assert 2.2 <= ouest < est <= 2.5
    assert 48.8 <= sud < nord <= 48.9


def test_les_limites_de_navigation_entourent_le_territoire():
    ouest, sud, est, nord = donnees_carte.limites_de_navigation((48.5, 1.6, 49.2, 2.8))
    assert ouest < 1.6 and est > 2.8 and sud < 48.5 and nord > 49.2
    assert donnees_carte.limites_de_navigation(None) is None


def test_l_empreinte_des_contours_suit_leur_trace():
    """Meme nombre de contours, trace different : les contours sont republies."""

    def collection(x):
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"code_section": "75101000AB"},
                    "geometry": {"type": "Polygon", "coordinates": [[[x, 48.8], [2.4, 48.8], [2.4, 48.9], [x, 48.8]]]},
                }
            ],
        }

    assert geo.empreinte(collection(2.3)) == geo.empreinte(collection(2.3))
    assert geo.empreinte(collection(2.3)) != geo.empreinte(collection(2.31))


def test_l_evolution_annuelle_est_exacte_sur_des_millesimes_reels():
    """Une hausse de 5 %/an exactement se lit 5,0 %/an, avec un R2 de 1."""

    lignes = []
    for rang, annee in enumerate(range(2021, 2026)):
        prix = 10_000 * 1.05**rang
        lignes += [
            {"code_section": "75101000AB", "annee": annee, "prix_m2": prix, "id_mutation": f"{annee}-{i}"}
            for i in range(VOLUME_MIN_ANNUEL)
        ]
    resultat = rendement_sections(pd.DataFrame(lignes), "prix_m2_median").iloc[0]

    assert resultat["taux_annuel"] == pytest.approx(5.0, abs=1e-9)
    assert resultat["regularite"] == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------
# Reglages retires de l'interface
# --------------------------------------------------------------------------


def test_une_zone_n_est_coloree_qu_a_partir_de_cinq_ventes():
    assert donnees_carte.SEUIL_GRISAGE == 4


def test_la_carte_s_ouvre_sur_le_fond_clair():
    """Le gris clair d'Esri, le plus discret sous les couleurs des prix."""

    assert donnees_carte.FOND_PAR_DEFAUT == "Clair (Esri)"
