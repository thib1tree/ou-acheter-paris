"""Tests du decoupage territorial, du parquet precalcule et de la carte a deux niveaux.

Trois exigences sont verrouillees ici, parce qu'elles pourraient casser en
silence :

1. **Le parquet ne change pas les chiffres.** Passer par
   `data/territoires/<cle>/mutations.parquet` doit donner exactement les memes
   ventes, aux memes prix, que la lecture des CSV bruts. Sans ce test, une
   difference de traitement entre les deux chemins resterait invisible.
2. **La simplification ne deplace pas les contours.** Retirer 70 % des sommets
   est acceptable tant qu'aucun point du trace ne bouge au-dela de la
   tolerance demandee.
3. **La bascule communes/sections est previsible.** C'est elle qui rend une
   agglomeration entiere affichable ; une regression la ferait retomber sur
   seize mille polygones d'un coup.

Lancement : `pytest -q`
"""

from __future__ import annotations

import gzip
import json
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import geo, telechargement, territoires  # noqa: E402
from src.ingestion import (  # noqa: E402
    OptionsNettoyage,
    RapportQualite,
    appliquer_filtres_qualite,
    charger_mutations_territoire,
    construire_transactions_territoire,
    ecrire_territoire,
    lire_preparation,
    reconstruire_mutations,
    territoire_prepare,
    typer_colonnes_dvf,
)
from src.stats import (  # noqa: E402
    CLE_COMMUNE,
    CLE_SECTION,
    couleurs_sections,
    rendement_sections,
    statistiques_sections,
)


# --------------------------------------------------------------------------
# Referentiel et territoires
# --------------------------------------------------------------------------


def test_grand_paris_est_le_premier_territoire():
    """Le territoire par defaut ouvre la liste : c'est le seul precalcule."""

    liste = territoires.territoires()
    assert liste[0].cle == territoires.CLE_GRAND_PARIS
    assert liste[0].exception is True
    # Les regions administratives suivent, par ordre alphabetique.
    noms = [t.nom for t in liste[1:]]
    assert noms == sorted(noms)


def test_cle_inconnue_retombe_sur_le_territoire_par_defaut():
    assert territoires.territoire("nexiste-pas").cle == territoires.CLE_GRAND_PARIS


def test_paris_est_ventile_par_arrondissement():
    """Les DVF ne connaissent pas la commune 75056, seulement ses arrondissements."""

    referentiel = territoires.referentiel_communes()
    codes = set(referentiel["code_commune"])
    assert "75101" in codes and "75120" in codes
    assert "75056" not in codes
    # Meme regle pour Lyon et Marseille.
    assert "69123" not in codes and "13055" not in codes
    assert "69381" in codes and "13201" in codes


def test_grand_paris_couvre_les_communes_preparees():
    """Le referentiel et le parquet livre doivent decrire le meme perimetre.

    Une commune presente dans les donnees mais absente du referentiel
    n'apparaitrait nulle part dans l'interface, et le decalage ne se verrait
    qu'a la lecture attentive d'une carte de 425 communes.
    """

    if not territoire_prepare(territoires.CLE_GRAND_PARIS):
        pytest.skip("territoire grand-paris non prepare")
    grand_paris = territoires.territoire(territoires.CLE_GRAND_PARIS)
    preparees = set(lire_preparation(territoires.CLE_GRAND_PARIS).get("communes") or [])
    assert preparees, "trace de preparation vide : le test ne verifie rien"
    assert preparees <= grand_paris.codes_communes


def test_departements_sans_dvf_sont_signales():
    """L'Alsace-Moselle releve du livre foncier : la DGFiP n'a pas ces mutations."""

    grand_est = next(t for t in territoires.territoires() if t.nom == "Grand Est")
    assert set(grand_est.departements_sans_dvf) == {"57", "67", "68"}
    # Et un territoire entierement couvert ne signale rien.
    assert territoires.territoire(territoires.CLE_GRAND_PARIS).departements_sans_dvf == ()


def test_toutes_les_regions_metropolitaines_sont_proposees():
    noms = {t.nom for t in territoires.territoires()}
    for attendue in ("Île-de-France", "Bretagne", "Corse", "La Réunion"):
        assert attendue in noms
    # Les collectivites d'outre-mer, hors champ des DVF, ne le sont pas.
    assert "Polynésie française" not in noms
    assert "Nouvelle-Calédonie" not in noms


# --------------------------------------------------------------------------
# Parquet precalcule
# --------------------------------------------------------------------------


def _lignes_dvf_factices() -> pd.DataFrame:
    """Un petit jeu de lignes DVF brutes, couvrant ce que le parquet doit tenir.

    Deux natures ecartees par defaut (`Echange`, `Adjudication`) pour verifier
    qu'elles survivent au precalcul, et un eventail de prix au m2 pour que les
    bornes de plausibilite aient prise.
    """

    lignes = []
    for indice, (prix, surface, nature, annee) in enumerate(
        [
            (300_000, 60, "Vente", 2021),
            (420_000, 60, "Vente", 2022),
            (600_000, 80, "Vente", 2023),
            (250_000, 50, "Vente", 2024),
            (900_000, 90, "Vente", 2025),
            (350_000, 50, "Echange", 2023),
            (480_000, 60, "Adjudication", 2024),
        ]
    ):
        section = f"78311000A{'ABCDEFG'[indice]}"
        lignes.append(
            {
                "id_mutation": f"T{indice}",
                "date_mutation": f"{annee}-06-0{indice + 1}",
                "nature_mutation": nature,
                "valeur_fonciere": str(float(prix)),
                "adresse_numero": "12",
                "adresse_suffixe": "None",
                "adresse_nom_voie": "RUE DES TESTS",
                "code_postal": "78800",
                "code_commune": "78311",
                "nom_commune": "Houilles",
                "code_departement": "78",
                "id_parcelle": f"{section}000{indice}",
                "section_prefixe": section[5:10],
                "lot1_surface_carrez": "nan",
                "nombre_lots": "1",
                "code_type_local": "2",
                "type_local": "Appartement",
                "surface_reelle_bati": str(float(surface)),
                "nombre_pieces_principales": "3",
                "surface_terrain": "nan",
                "longitude": "2.19",
                "latitude": "48.92",
            }
        )
    return typer_colonnes_dvf(pd.DataFrame(lignes), "factice.csv")


@pytest.fixture
def territoire_fige(tmp_path, monkeypatch):
    """Fige des mutations dans un parquet, exactement comme le ferait le script."""

    monkeypatch.setenv("DVF_TERRITOIRES_DIR", str(tmp_path))
    import src.ingestion as ingestion

    monkeypatch.setattr(ingestion, "repertoire_territoires", lambda: str(tmp_path))

    rapport = RapportQualite()
    brut = _lignes_dvf_factices()
    rapport.lignes_brutes = len(brut)
    # La preparation ne filtre **aucune** nature de mutation : le reglage doit
    # rester vivant dans la barre laterale.
    mutations = reconstruire_mutations(brut, OptionsNettoyage(natures_mutation=None), rapport)
    ecrire_territoire("test", mutations, rapport, {"annees": [2021, 2025]})
    return "test", mutations


def test_le_parquet_ne_change_aucun_chiffre(territoire_fige):
    """La promesse centrale : l'aller-retour parquet ne bouge pas une valeur.

    Le risque n'est pas theorique : le parquet preserve fidelement les types
    entiers de pandas, la ou la lecture d'un CSV rend des flottants — et ce
    decalage suffit a casser le repli sur la surface Carrez.
    """

    cle, mutations = territoire_fige
    rapport = RapportQualite()
    reference = appliquer_filtres_qualite(mutations.copy(), OptionsNettoyage(), rapport)
    depuis_parquet, _ = construire_transactions_territoire(cle)

    a = reference.sort_values("id_mutation").reset_index(drop=True)
    b = depuis_parquet.sort_values("id_mutation").reset_index(drop=True)

    assert len(a) == len(b) > 0
    assert list(a["id_mutation"]) == list(b["id_mutation"])
    assert a["prix_m2"].astype(float).equals(b["prix_m2"].astype(float))
    assert a["valeur_fonciere"].astype(float).equals(b["valeur_fonciere"].astype(float))
    assert a["code_section"].equals(b["code_section"])


def test_le_parquet_conserve_toutes_les_natures_de_mutation(territoire_fige):
    """Sinon le filtre « natures de mutation » n'aurait plus rien a filtrer."""

    cle, _ = territoire_fige
    mutations, _ = charger_mutations_territoire(cle, ["nature_mutation"])
    natures = set(mutations["nature_mutation"].dropna())
    assert "Vente" in natures
    # Les natures ecartees par defaut doivent etre presentes dans le parquet,
    # et seulement retirees au moment du filtrage.
    assert {"Echange", "Adjudication"} <= set(natures)

    sans_filtre, _ = construire_transactions_territoire(
        cle, OptionsNettoyage(natures_mutation=None)
    )
    par_defaut, _ = construire_transactions_territoire(cle)
    assert len(sans_filtre) > len(par_defaut)


def test_les_filtres_de_qualite_restent_vivants_sur_le_parquet(territoire_fige):
    """Les bornes de plausibilite doivent encore agir apres precalcul."""

    cle, _ = territoire_fige
    large, _ = construire_transactions_territoire(
        cle, OptionsNettoyage(prix_m2_min=0, prix_m2_max=1_000_000)
    )
    etroit, _ = construire_transactions_territoire(
        cle, OptionsNettoyage(prix_m2_min=5_000, prix_m2_max=8_000)
    )
    assert len(etroit) < len(large)
    assert etroit["prix_m2"].between(5_000, 8_000).all()


def test_territoire_absent_donne_un_message_actionnable(tmp_path, monkeypatch):
    import src.ingestion as ingestion

    monkeypatch.setattr(ingestion, "repertoire_territoires", lambda: str(tmp_path))
    assert not territoire_prepare("region-53")
    with pytest.raises(FileNotFoundError, match="preparer_territoire"):
        construire_transactions_territoire("region-53")


# --------------------------------------------------------------------------
# Lecture des fichiers departementaux telecharges
# --------------------------------------------------------------------------


@pytest.fixture
def departement_factice(tmp_path, monkeypatch):
    """Ecrit un fichier departemental au format geo-dvf, comme Etalab le publie."""

    monkeypatch.setenv("DVF_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(telechargement, "repertoire_cache_dvf", lambda: str(tmp_path))

    brut = _lignes_dvf_factices().drop(columns=["_fichier"]).astype(str)
    # Les parcelles nues n'ont pas de local : plus de la moitie du volume reel,
    # et le lecteur doit les ecarter des la lecture.
    nues = brut.head(3).copy()
    nues["id_mutation"] = ["N1", "N2", "N3"]
    nues["type_local"] = ""
    nues["code_commune"] = "78124"
    brut = pd.concat([brut, nues, nues], ignore_index=True)

    chemin = telechargement.chemin_departement("78", 2025)
    with gzip.open(chemin, "wt", encoding="utf-8", newline="") as flux:
        brut.to_csv(flux, index=False, sep=",")
    return brut


def test_lecture_departementale_ecarte_les_parcelles_nues(departement_factice):
    """Les lignes sans type de local sont plus de la moitie du volume."""

    brut = departement_factice
    lignes = telechargement.lignes_departement("78", 2025)
    assert len(lignes) < len(brut)
    assert lignes["type_local"].notna().all()


def test_lecture_departementale_restreint_aux_communes_demandees(departement_factice):
    del departement_factice
    lignes = telechargement.lignes_departement("78", 2025, codes_communes=["78311"])
    assert set(lignes["code_commune"]) == {"78311"}


# --------------------------------------------------------------------------
# Simplification des contours
# --------------------------------------------------------------------------


def _sommets(geometrie: dict) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []

    def parcourir(coordonnees):
        if isinstance(coordonnees, (list, tuple)):
            if coordonnees and isinstance(coordonnees[0], (int, float)):
                points.append(tuple(coordonnees[:2]))
            else:
                for element in coordonnees:
                    parcourir(element)

    parcourir(geometrie.get("coordinates"))
    return points


def _anneaux(geometrie: dict) -> list[list[tuple[float, float]]]:
    polygones = (
        geometrie["coordinates"]
        if geometrie["type"] == "MultiPolygon"
        else [geometrie["coordinates"]]
    )
    return [[tuple(p[:2]) for p in anneau] for polygone in polygones for anneau in polygone]


def _distance_au_trace(point, anneau: list[tuple[float, float]]) -> float:
    """Distance en metres d'un point a la ligne brisee d'un anneau.

    C'est l'invariant que Douglas-Peucker garantit : un sommet retire peut etre
    tres loin des sommets conserves — sur un long segment droit, c'est meme le
    cas general — mais il reste a portee du *trace*.
    """

    lon, lat = point
    facteur = math.cos(math.radians(lat))
    meilleur = float("inf")
    for debut, fin in zip(anneau, anneau[1:]):
        ax, ay = (debut[0] - lon) * facteur, debut[1] - lat
        bx, by = (fin[0] - lon) * facteur, fin[1] - lat
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            distance = math.hypot(ax, ay)
        else:
            position = max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
            distance = math.hypot(ax + position * dx, ay + position * dy)
        meilleur = min(meilleur, distance)
    return meilleur * 111_320


def _contours_du_depot() -> list[dict]:
    """Contours cadastraux bruts (Paris 1er), tels que les livre Etalab.

    Les fichiers de `data/geo/` sont deja simplifies : c'est sur le trace
    brut que la simplification doit faire ses preuves.
    """

    chemin = Path(__file__).parent / "donnees" / "75101-sections-brutes.geojson"
    return json.loads(chemin.read_text(encoding="utf-8"))["features"]


def test_la_simplification_allege_reellement_les_contours():
    entites = _contours_du_depot()
    avant = geo.compter_sommets(entites)
    apres = geo.compter_sommets(geo.simplifier_entites(entites, 2.0))
    # Sur des contours cadastraux reels, la moitie des sommets part au minimum.
    assert apres < avant / 2


@pytest.mark.parametrize("tolerance", [2.0, 10.0])
def test_la_simplification_ne_deplace_pas_le_trace(tolerance):
    """Aucun sommet retire ne doit s'ecarter du trace au-dela de la tolerance.

    C'est ce qui autorise a diviser le poids des contours par trois sans
    toucher a ce que l'utilisateur voit : a 2 m, l'ecart est tres inferieur au
    pixel aux echelles d'affichage de la carte.
    """

    for entite in _contours_du_depot()[:8]:
        simplifiee = geo.simplifier_geometrie(entite["geometry"], tolerance)
        assert set(_sommets(simplifiee)) <= set(_sommets(entite["geometry"])), (
            "un sommet invente n'est pas une simplification"
        )
        for origine, allege in zip(_anneaux(entite["geometry"]), _anneaux(simplifiee)):
            pire = max(_distance_au_trace(point, allege) for point in origine)
            # Marge d'un facteur 2 : l'approximation du facteur de longitude est
            # prise a la latitude moyenne de l'entite, pas point par point.
            assert pire <= tolerance * 2, f"ecart de {pire:.1f} m pour une tolerance de {tolerance} m"


def test_la_simplification_preserve_les_anneaux_fermes():
    for entite in _contours_du_depot()[:10]:
        geometrie = geo.simplifier_geometrie(entite["geometry"], 2.0)
        polygones = (
            geometrie["coordinates"]
            if geometrie["type"] == "MultiPolygon"
            else [geometrie["coordinates"]]
        )
        for polygone in polygones:
            for anneau in polygone:
                assert len(anneau) >= 4, "un anneau de moins de 4 sommets n'est plus une surface"
                assert anneau[0] == anneau[-1], "anneau non ferme"


def test_une_geometrie_minuscule_nest_pas_reduite_a_un_trait():
    """Le garde-fou : mieux vaut un contour lourd qu'un polygone degenere."""

    carre = {
        "type": "Polygon",
        "coordinates": [[[2.0, 48.0], [2.0001, 48.0], [2.0001, 48.0001], [2.0, 48.0001], [2.0, 48.0]]],
    }
    resultat = geo.simplifier_geometrie(carre, 1_000.0)
    assert len(resultat["coordinates"][0]) >= 4


# --------------------------------------------------------------------------
# Niveau communal de la carte
# --------------------------------------------------------------------------


def _communes_factices() -> dict:
    def carre(code, lon, lat, cote=0.05):
        return {
            "type": "Feature",
            "properties": {"code_commune": code},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon, lat], [lon + cote, lat], [lon + cote, lat + cote],
                    [lon, lat + cote], [lon, lat],
                ]],
            },
        }

    return {
        "type": "FeatureCollection",
        "features": [carre("92012", 2.20, 48.80), carre("95585", 2.35, 49.00)],
    }


def test_contours_communaux_absents_ne_font_pas_echouer(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "repertoire_communes", lambda: str(tmp_path))
    assert geo.charger_communes_territoire("inexistant")["features"] == []


# --------------------------------------------------------------------------
# Agregation a la maille communale
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def transactions_reelles():
    """Les ventes du territoire livre : la maille communale s'y verifie en vrai."""

    if not territoire_prepare(territoires.CLE_GRAND_PARIS):
        pytest.skip("territoire grand-paris non prepare")
    transactions, _ = construire_transactions_territoire(territoires.CLE_GRAND_PARIS)
    return transactions


def test_agregation_communale_sans_colonne_dupliquee(transactions_reelles):
    stats = statistiques_sections(transactions_reelles, CLE_COMMUNE)
    assert not stats.columns.duplicated().any()
    assert CLE_COMMUNE in stats.columns
    assert len(stats) == transactions_reelles["code_commune"].nunique()


def test_les_deux_mailles_comptent_les_memes_ventes(transactions_reelles):
    """Changer de niveau de lecture ne doit ni perdre ni dupliquer une vente."""

    par_section = statistiques_sections(transactions_reelles, CLE_SECTION)
    par_commune = statistiques_sections(transactions_reelles, CLE_COMMUNE)
    assert par_section["nb_transactions"].sum() == par_commune["nb_transactions"].sum()
    assert len(par_commune) < len(par_section)


def test_couleurs_et_rendement_suivent_la_maille(transactions_reelles):
    stats = statistiques_sections(transactions_reelles, CLE_COMMUNE)
    couleurs, echelle, _ = couleurs_sections(stats, "prix_m2_median", 0, cle=CLE_COMMUNE)
    assert set(couleurs) == set(stats[CLE_COMMUNE])
    assert echelle.bas < echelle.haut

    rendements = rendement_sections(transactions_reelles, "prix_m2_median", cle=CLE_COMMUNE)
    assert CLE_COMMUNE in rendements.columns
    assert len(rendements) <= len(stats)


def test_au_niveau_communal_le_libelle_de_zone_est_la_commune(transactions_reelles):
    """« Section 000AB » n'aurait aucun sens dans l'infobulle d'une commune."""

    stats = statistiques_sections(transactions_reelles, CLE_COMMUNE)
    assert (stats["section_courte"] == stats["nom_commune"]).all()


# --------------------------------------------------------------------------
# Script de preparation
# --------------------------------------------------------------------------


def test_extraction_de_lunite_urbaine_developpe_les_arrondissements():
    """Le zonage INSEE parle de « Paris » ; les DVF, de « Paris 1er »."""

    from scripts.preparer_territoire import communes_unite_urbaine

    table = pd.DataFrame({
        "code_commune_insee": ["75056", "92012", "78311", "13055", "29019"],
        "code_unite_urbaine": ["00851", "00851", "00851", "00759", "29000"],
    })
    codes = communes_unite_urbaine(table)
    assert "75056" not in codes
    assert len([c for c in codes if c.startswith("751")]) == 20
    assert {"92012", "78311"} <= set(codes)
    assert not {"13055", "29019"} & set(codes)


def test_extraction_refuse_un_fichier_au_mauvais_format():
    from scripts.preparer_territoire import communes_unite_urbaine

    with pytest.raises(ValueError, match="colonnes attendues"):
        communes_unite_urbaine(pd.DataFrame({"machin": ["x"]}))


# --------------------------------------------------------------------------
# Types des colonnes continues
# --------------------------------------------------------------------------
#
# Les DVF livrent souvent des surfaces rondes (« 60 »), dont pandas fait un
# entier nullable `Int64` que l'aller-retour parquet conserve fidelement. Le
# repli sur la surface Carrez ecrit alors une decimale — 50,5 m2 — dans une
# colonne entiere : « cannot safely cast non-equivalent object to int64 ».


def _mutation_minimale(**surcharges) -> pd.DataFrame:
    base = {
        "id_mutation": ["a", "b"],
        "date_mutation": pd.to_datetime(["2024-01-01", "2024-02-01"]),
        "nature_mutation": ["Vente", "Vente"],
        "valeur_fonciere": pd.array([300_000, 250_000], dtype="Int64"),
        "surface_bati": pd.array([60, 0], dtype="Int64"),
        "surface_carrez": pd.Series([None, 50.5], dtype="object"),
        "code_section": ["92012000AB", "92012000AB"],
        "code_commune": ["92012", "92012"],
        "nom_commune": ["Boulogne-Billancourt", "Boulogne-Billancourt"],
        "section_courte": ["AB", "AB"],
        "nb_locaux": pd.array([1, 1], dtype="Int64"),
        "nb_pieces": pd.array([3, 2], dtype="Int64"),
        "nb_dependances": [0, 0],
        "contient_local_activite": [False, False],
        "longitude": [2.24, 2.24],
        "latitude": [48.83, 48.83],
        "annee": pd.array([2024, 2024], dtype="Int64"),
        "type_bien": ["Appartement", "Appartement"],
        "types_locaux": ["Appartement", "Appartement"],
        "adresse": ["1 rue A", "2 rue B"],
    }
    base.update(surcharges)
    return pd.DataFrame(base)


@pytest.mark.parametrize(
    "carrez",
    [
        pd.Series([None, 50.5], dtype="object"),
        pd.Series([None, 50.5], dtype="float64"),
        pd.Series([None, 50.0], dtype="object"),
    ],
)
def test_le_repli_carrez_accepte_une_surface_decimale(carrez):
    """Une surface entiere ne doit pas empecher d'y ecrire 50,5 m2."""

    rapport = RapportQualite()
    resultat = appliquer_filtres_qualite(
        _mutation_minimale(surface_carrez=carrez), OptionsNettoyage(), rapport
    )
    assert rapport.surfaces_reprises_carrez == 1
    assert len(resultat) == 2, "la vente reprise sur la surface Carrez doit etre conservee"
    assert resultat["surface_bati"].dtype == "float64"


def test_les_colonnes_continues_sortent_en_flottant():
    """Un `Int64` en entree ne doit pas ressortir tel quel : les prix sont continus."""

    resultat = appliquer_filtres_qualite(
        _mutation_minimale(), OptionsNettoyage(), RapportQualite()
    )
    for colonne in ("valeur_fonciere", "surface_bati", "prix_m2", "nb_pieces", "nb_locaux"):
        assert resultat[colonne].dtype == "float64", colonne


def test_le_prix_au_m2_utilise_la_surface_carrez_reprise():
    resultat = appliquer_filtres_qualite(
        _mutation_minimale(), OptionsNettoyage(), RapportQualite()
    )
    reprise = resultat[resultat["id_mutation"] == "b"].iloc[0]
    assert reprise["surface_bati"] == pytest.approx(50.5)
    assert reprise["prix_m2"] == pytest.approx(round(250_000 / 50.5))


def test_conversion_numerique_tolere_la_virgule_dans_une_colonne_entiere():
    """Le meme piege, en amont : « 50,5 » au milieu de nombres ronds.

    `pd.to_numeric` fait un `Int64` des valeurs deja lisibles, puis le repli
    sur la virgule decimale y reinjecte une valeur fractionnaire.
    """

    from src.ingestion import _vers_numerique

    valeurs = _vers_numerique(pd.Series(["60", "75", "50,5", None]))
    assert valeurs.dtype == "float64"
    assert list(valeurs[:3]) == [60.0, 75.0, 50.5]
    assert pd.isna(valeurs.iloc[3])


# --------------------------------------------------------------------------
# Charge utile de la carte MapLibre
# --------------------------------------------------------------------------
#
# Ces tests tiennent le contrat entre Python et `site/carte/carte.js` :
# le navigateur recoit des tableaux paralleles et un descriptif de champs, et
# c'est lui qui met en forme. Un decalage entre `champs` et `valeurs`
# afficherait le prix au m2 en face du libelle « Surface mediane » sans que
# rien ne plante — d'ou une verification explicite.

import tempfile  # noqa: E402

import numpy as np  # noqa: E402

from src import charge as donnees_carte  # noqa: E402
from src import points  # noqa: E402
from src.stats import (  # noqa: E402
    GRIS_DONNEES_INSUFFISANTES,
    METRIQUES,
    PALETTE_SEQUENTIELLE,
    EchelleCouleur,
    Filtres,
)


def _source_carte() -> str:
    """Le code de la carte, lu tel qu'il sera servi au navigateur."""

    return (Path(__file__).resolve().parents[1] / "site" / "carte" / "carte.js").read_text(
        encoding="utf-8"
    )


def _xy(cle: str) -> tuple[int, int]:
    x, y = cle.split("/")
    return int(x), int(y)


def _tuiles(ventes: pd.DataFrame) -> list[dict]:
    """Toutes les tuiles ecrites pour un jeu de ventes, relues depuis le disque."""

    with tempfile.TemporaryDirectory() as dossier:
        points.publier(ventes, "essai", dossier)
        manifeste = json.loads(
            Path(dossier, points.nom_manifeste("essai")).read_text(encoding="utf-8")
        )
        return [
            json.loads(
                Path(dossier, points.nom_tuile("essai", *_xy(cle))).read_text(encoding="utf-8")
            )
            for cle in manifeste["tuiles"]
        ]


@pytest.fixture
def stats_factices() -> pd.DataFrame:
    return pd.DataFrame(
        {
            CLE_SECTION: ["92012000AB", "92012000AC"],
            "section_courte": ["AB", "AC"],
            "nom_commune": ["Boulogne-Billancourt", "Boulogne-Billancourt"],
            "code_commune": ["92012", "92012"],
            "nb_transactions": [42, 1],
            "prix_m2_median": [8500.0, 9000.0],
            "prix_m2_moyen": [8600.0, 9000.0],
            "prix_total_median": [520_000.0, 610_000.0],
            "prix_total_moyen": [530_000.0, 610_000.0],
            "surface_mediane": [61.0, 68.0],
            "prix_max": [1_200_000.0, 610_000.0],
            "prix_min": [180_000.0, 610_000.0],
        }
    )


def test_chaque_zone_porte_autant_de_valeurs_que_de_champs_annonces(stats_factices):
    charge = donnees_carte.couche(stats_factices, {"92012000AB": "#ff0000"}, CLE_SECTION)

    assert charge["codes"] == ["92012000AB", "92012000AC"]
    assert charge["couleurs"] == ["#ff0000", GRIS_DONNEES_INSUFFISANTES]
    assert charge["entetes"][0] == "Boulogne-Billancourt — section AB"
    for rangee in charge["valeurs"]:
        assert len(rangee) == len(charge["champs"])


def test_le_mode_rendement_ajoute_ses_champs_a_la_suite(stats_factices):
    rendements = pd.DataFrame(
        {
            CLE_SECTION: ["92012000AB", "92012000AC"],
            "taux_annuel": [4.2, float("nan")],
            "variation_cumulee": [22.0, float("nan")],
            "regularite": [0.85, float("nan")],
            "nb_annees": [5, 1],
            "fiable": [True, False],
        }
    )
    charge = donnees_carte.couche(stats_factices, {}, CLE_SECTION, rendements=rendements)

    assert [libelle for libelle, _ in donnees_carte.CHAMPS_RENDEMENT] == [
        libelle for libelle, _ in charge["champs"][len(donnees_carte.CHAMPS_ZONE):]
    ]
    for rangee in charge["valeurs"]:
        assert len(rangee) == len(charge["champs"])
    # Une tendance non fiable est envoyee vide plutot qu'a zero : le navigateur
    # affiche « n/d », il n'invente pas une evolution nulle.
    assert charge["valeurs"][1][len(donnees_carte.CHAMPS_ZONE)] is None
    assert "non calculable" in charge["notes"][1]


def test_la_charge_utile_est_serialisable_en_json(stats_factices):
    """`NaN` traverse pandas sans bruit, mais JSON ne sait pas le transporter."""

    stats_factices.loc[1, "prix_m2_median"] = float("nan")
    charge = donnees_carte.couche(stats_factices, {}, CLE_SECTION, grisees={"92012000AC"})

    texte = json.dumps(charge, allow_nan=False)
    assert "NaN" not in texte
    assert charge["valeurs"][1][1] is None
    assert "Trop peu de ventes" in charge["notes"][1]


def test_au_niveau_communal_lentete_est_le_nom_de_la_commune(stats_factices):
    # Au niveau communal, `statistiques_sections` recopie le nom de la commune
    # dans `section_courte` : « section 000AB » n'aurait aucun sens ici.
    stats = stats_factices.drop(columns=[CLE_SECTION])
    stats["section_courte"] = stats["nom_commune"]
    charge = donnees_carte.couche(stats, {}, CLE_COMMUNE)
    assert charge["entetes"] == ["Boulogne-Billancourt", "Boulogne-Billancourt"]


def test_le_repere_de_voirie_suit_la_zone(stats_factices):
    """Le sous-titre de l'infobulle est aligne sur les codes, pas sur l'ordre."""

    charge = donnees_carte.couche(
        stats_factices, {}, CLE_SECTION, reperes={"92012000AC": "Rue de Paris"}
    )
    assert charge["reperes"] == [None, "Rue de Paris"]

    # Aucun repere connu : le champ disparait au lieu d'envoyer une colonne de
    # `null` pour chaque zone du territoire.
    assert donnees_carte.couche(stats_factices, {}, CLE_SECTION)["reperes"] is None


def _ventes_factices(positions: list[tuple[float, float]]) -> pd.DataFrame:
    """Une vente par position donnee, toutes comparables sauf leur prix."""

    nombre = len(positions)
    return pd.DataFrame(
        {
            "longitude": [p[0] for p in positions],
            "latitude": [p[1] for p in positions],
            "surface_bati": [20.0, 70.0, 200.0, 50.0][:nombre],
            "prix_m2": [3_000.0, 6_000.0, 9_000.0, 12_000.0][:nombre],
            "valeur_fonciere": [60_000.0, 420_000.0, 1_800_000.0, 600_000.0][:nombre],
            "nb_pieces": [1, 3, 8, 2][:nombre],
            "type_bien": ["Appartement"] * nombre,
            "date_mutation": pd.to_datetime(["2023-01-02", "2024-05-06", "2025-09-10",
                                             "2022-03-04"][:nombre]),
            "annee": [2023, 2024, 2025, 2022][:nombre],
            "adresse": ["1 Rue A", "2 Rue B", "3 Rue C", "4 Rue D"][:nombre],
        }
    )


def test_une_tuile_de_ventes_est_faite_de_colonnes_et_non_d_objets():
    """Contrat du format : des tableaux paralleles, jamais de HTML.

    Les accolades et les noms de champs repetes d'une `FeatureCollection`
    pesent la moitie du fichier. Et rien n'est mis en forme ici : une
    couleur ou une infobulle ecrites par Python devraient etre reecrites a
    chaque changement de filtre, c'est-a-dire retelechargees.
    """

    ventes = _ventes_factices([(2.30, 48.80), (2.3005, 48.8005)])
    with tempfile.TemporaryDirectory() as dossier:
        points.publier(ventes, "essai", dossier)
        manifeste = json.loads(
            Path(dossier, points.nom_manifeste("essai")).read_text(encoding="utf-8")
        )
        assert manifeste["zoom"] == points.ZOOM_TUILES
        assert manifeste["cle"] == "essai"
        assert manifeste["tuiles"], "le manifeste enumere les tuiles ecrites"

        brut = Path(dossier, points.nom_tuile("essai", *_xy(manifeste["tuiles"][0]))).read_text(
            encoding="utf-8"
        )
        assert "<" not in brut and "couleur" not in brut
        tuile = json.loads(brut)

    for colonne in ("lon", "lat", "off", "an", "pi", "su", "va", "m2", "dt", "ty", "ad"):
        assert isinstance(tuile[colonne], list), colonne
    # Dictionnaires : les libelles sont ecrits une fois, pointes par indice.
    assert tuile["ty_l"] == ["Appartement"]
    assert all(libelle in tuile["ad_l"] for libelle in ("1 Rue A", "2 Rue B"))


def test_les_ventes_superposees_sont_groupees_des_l_ecriture():
    """Les DVF geolocalisent a la parcelle : un immeuble, un seul emplacement.

    Le groupement est fige ici plutot que refait dans le navigateur : `off`
    decoupe les ventes par emplacement, si bien que le navigateur n'a jamais
    de table de hachage a reconstruire — il ne parcourt que des tranches
    contigues. Sur le Grand Paris, 65 % des ventes partagent leur position
    avec au moins une autre.
    """

    ventes = _ventes_factices([(2.30, 48.80), (2.30, 48.80), (2.30, 48.80), (2.35, 48.85)])
    tuiles = _tuiles(ventes)

    emplacements = sum(len(tuile["lon"]) for tuile in tuiles)
    assert emplacements == 2, "un emplacement par position, pas par vente"

    empile = next(tuile for tuile in tuiles if len(tuile["lon"]) == 2 or tuile["off"][1] == 3)
    assert empile["off"][0] == 0
    # Les trois ventes du meme point occupent une seule tranche de `off`.
    assert 3 in [empile["off"][i + 1] - empile["off"][i] for i in range(len(empile["lon"]))]
    assert empile["off"][-1] == len(empile["an"])


def test_une_tuile_dit_les_trous_plutot_que_de_les_taire():
    """Une surface ou un nombre de pieces absent vaut `-1`, jamais `null`.

    C'est trois caracteres de moins par trou sur des colonnes qui en comptent
    des milliers, et le navigateur n'a qu'une comparaison a faire au lieu d'un
    test de type. Inventer la valeur, en revanche, ferait mentir l'infobulle.
    """

    ventes = _ventes_factices([(2.30, 48.80)])
    ventes["nb_pieces"] = None
    ventes["adresse"] = None
    tuile = _tuiles(ventes)[0]

    assert tuile["pi"] == [-1]
    assert tuile["ad_l"] == [""]
    # Les dates partent en AAAAMMJJ : un entier se compare et se trie.
    assert tuile["dt"] == [20230102]


def test_chaque_vente_tombe_dans_la_tuile_qui_la_contient():
    """Le navigateur calcule les memes numeros pour demander ses tuiles.

    Si les deux calculs divergeaient d'une unite, la carte demanderait des
    tuiles voisines de celles qui portent les ventes : rien ne s'afficherait,
    et rien ne le dirait.
    """

    ventes = _ventes_factices([(2.3522, 48.8566), (2.30, 48.80)])
    with tempfile.TemporaryDirectory() as dossier:
        points.publier(ventes, "essai", dossier)
        manifeste = json.loads(
            Path(dossier, points.nom_manifeste("essai")).read_text(encoding="utf-8")
        )
        for cle in manifeste["tuiles"]:
            x, y = _xy(cle)
            tuile = json.loads(
                Path(dossier, points.nom_tuile("essai", x, y)).read_text(encoding="utf-8")
            )
            for longitude, latitude in zip(tuile["lon"], tuile["lat"]):
                attendus = points._numero_tuile(
                    np.array([longitude]), np.array([latitude]), points.ZOOM_TUILES
                )
                assert (int(attendus[0][0]), int(attendus[1][0])) == (x, y)


def test_les_filtres_partent_en_regles_et_non_en_ventes_retenues():
    """Contrat de la charge utile des filtres : quelques dizaines d'octets.

    Les appliquer ici obligerait a renvoyer les ventes retenues a chaque case
    cochee — c'est-a-dire tout ce que le decoupage en tuiles evite. Le
    navigateur rejoue les memes regles que `stats.filtrer`, bornes ouvertes
    comprises.
    """

    charge = donnees_carte.filtres_des_ventes(
        Filtres(
            annees=(2023, 2024),
            surface=(0.0, 120.0),
            surface_max_ouvert=True,
            types_bien=["Appartement"],
        )
    )

    assert charge == {
        "an": [2023, 2024],
        "su": [0.0, 120.0, True],
        "ty": ["Appartement"],
        # Aucun filtre neuf / ancien pose : tout passe (`None`, pas `[]`).
        "et": None,
    }
    assert charge["ty"] == ["Appartement"]


def test_les_ventes_se_colorent_sur_l_echelle_des_sections():
    """Un point plus rouge que sa section s'y est vendu plus cher qu'elle.

    Deux bornes et une palette partent, pas une couleur par vente : a deux
    cent mille emplacements, ce serait autant de chaines a renvoyer a chaque
    changement de filtre. C'est le GPU qui en tire la teinte.
    """

    echelle = EchelleCouleur.depuis([3_000.0, 6_000.0, 9_000.0])
    charge = donnees_carte.echelle_des_ventes(echelle, "prix_m2")

    assert charge["bas"] == echelle.bas and charge["haut"] == echelle.haut
    assert charge["palette"] == list(PALETTE_SEQUENTIELLE)
    assert charge["colonne"] == "m2"
    # La metrique choisie change la colonne lue dans la tuile, pas l'echelle.
    assert donnees_carte.echelle_des_ventes(echelle, "valeur_fonciere")["colonne"] == "va"
    # Sans echelle exploitable, tout reste gris plutot que colore au hasard.
    assert donnees_carte.echelle_des_ventes(None, "prix_m2")["vide"] is True


def test_les_gares_partent_avec_leur_statut_leurs_lignes_et_leur_rang():
    """Contrat de la charge utile des gares, dont le navigateur tire deux couches.

    Le statut est filtre cote MapLibre plutot que cote Python : cocher « celles
    a venir » ne doit rien retelecharger. Le rang, lui, pilote la taille du
    point, et les lignes remplacent le reseau dans l'infobulle — « RER B,
    Métro 4 » se lit, « RATP » ne dit rien.
    """

    charge = donnees_carte.points_gares(
        [
            {
                "nom": "Châtillon Montrouge", "latitude": 48.81, "longitude": 2.30,
                "reseau": "Métro", "type": "subway", "statut": geo.STATUT_SERVICE,
                "lignes": ["Métro 13"], "lignes_a_venir": ["Métro 15"],
            },
            {
                "nom": "Vitry Centre", "latitude": 48.79, "longitude": 2.39,
                "reseau": "Métro", "type": "subway", "statut": geo.STATUT_TRAVAUX,
                "lignes": ["Métro 15"],
            },
            # Gare heritee d'un fichier anterieur : ni statut ni lignes.
            {"nom": "Sartrouville", "latitude": 48.93, "longitude": 2.15,
             "reseau": "Transilien", "type": "station"},
        ]
    )

    par_nom = {e["properties"]["nom"]: e["properties"] for e in charge["features"]}
    assert par_nom["Châtillon Montrouge"]["statut"] == geo.STATUT_SERVICE
    assert par_nom["Châtillon Montrouge"]["lignes"] == "Métro 13"
    # La ligne annoncee part a part : l'infobulle la dit « à venir ».
    assert par_nom["Châtillon Montrouge"]["a_venir"] == "Métro 15"
    assert par_nom["Vitry Centre"]["a_venir"] == ""
    assert par_nom["Vitry Centre"]["statut"] == geo.STATUT_TRAVAUX
    assert par_nom["Sartrouville"]["statut"] == geo.STATUT_SERVICE
    assert par_nom["Sartrouville"]["lignes"] == ""
    assert par_nom["Sartrouville"]["rang"] == geo.RANG_LOURD
    assert par_nom["Vitry Centre"]["rang"] == geo.RANG_LEGER
    # Le genre choisit le dessin, le rang la taille : une station de metro se
    # dessine « M » meme quand elle reste discrete en vue d'ensemble.
    assert par_nom["Châtillon Montrouge"]["genre"] == geo.GENRE_METRO
    assert par_nom["Sartrouville"]["genre"] == geo.GENRE_TRAIN


def test_les_constantes_du_champ_de_distance_sont_celles_de_maplibre():
    """Un bord mal encode ne planterait pas : il rendrait des icones floues.

    Le nuanceur de MapLibre place le bord a (256 - 64) / 256 et compte huit
    pixels par unite de distance (`#define SDF_PX 8.0`). Ces deux valeurs sont
    un contrat avec la bibliotheque versionnee, pas des reglages.
    """

    source = _source_carte()
    assert "var SDF_RAYON = 8;" in source
    assert "var SDF_SEUIL = 0.25;" in source
    assert "255 - 255 * (distance / SDF_RAYON + SDF_SEUIL)" in source

    nuanceur = (
        Path(__file__).resolve().parents[1]
        / "site" / "carte" / "vendor" / "maplibre-gl.mjs"
    ).read_text(encoding="utf-8", errors="ignore")
    assert "#define SDF_PX 8.0" in nuanceur
    assert "inner_edge=(256.0-64.0)/256.0" in nuanceur


def test_l_evolution_annuelle_est_une_metrique_comme_les_autres():
    """Elle se choisit dans la meme liste, et nulle part ailleurs : elle repond
    a la meme question que les autres, *qu'est-ce que la couleur raconte ?*
    Ses champs d'infobulle, eux, restent conditionnes a elle — ils n'ont aucun
    sens devant un niveau de prix.
    """

    assert donnees_carte.CLE_RENDEMENT in donnees_carte.LIBELLES_METRIQUES
    assert donnees_carte.LIBELLES_METRIQUES[donnees_carte.CLE_RENDEMENT] == "Évolution annuelle du prix médian au m²"
    # Les niveaux de prix restent en tete, l'evolution ferme la marche.
    assert [cle for cle, _ in donnees_carte.METRIQUES_PROPOSEES][-1] == donnees_carte.CLE_RENDEMENT
    assert donnees_carte.METRIQUE_BASE_RENDEMENT in METRIQUES

    # La mise en garde suit la metrique : elle descend dans la legende, donc
    # ne s'affiche que lorsque la metrique est choisie.
    legende = donnees_carte.legende_rendement(4.0, "Prix médian au m²")
    assert legende["note"] == donnees_carte.NOTE_RENDEMENT
    assert "note" not in donnees_carte.legende_prix(
        EchelleCouleur(bas=1.0, haut=2.0), "Prix médian au m²", "Gris : rien"
    )


def test_les_contours_publies_ne_portent_que_leur_code():
    """Couleurs et chiffres sont injectes cote navigateur, pas dans le fichier.

    C'est ce qui permet de ne telecharger les geometries qu'une fois : changer
    un filtre ne change que des couleurs, jamais les contours.
    """

    publie = geo.contours_carte(_communes_factices()["features"], CLE_COMMUNE)

    assert len(publie["features"]) == 2
    assert all(set(e["properties"]) == {"c"} for e in publie["features"])
    assert {e["properties"]["c"] for e in publie["features"]} == {"92012", "95585"}


@pytest.mark.parametrize("mode_rendement", [False, True])
@pytest.mark.parametrize("niveau", [CLE_SECTION, CLE_COMMUNE])
def test_une_selection_vide_rend_une_charge_utile_valide(niveau, mode_rendement):
    """Filtres trop serrés : la carte doit recevoir des couches vides, pas un plantage."""

    colonnes = [
        CLE_SECTION, CLE_COMMUNE, "section_courte", "nom_commune", "annee",
        "id_mutation", "prix_m2", "valeur_fonciere", "surface_bati",
    ]
    charge, _, _ = donnees_carte.couche_complete(
        pd.DataFrame(columns=colonnes), niveau, "prix_m2_median", 2, mode_rendement, "Prix médian au m²"
    )
    json.dumps(charge, allow_nan=False)
    assert charge["codes"] == [] and charge["valeurs"] == []
    assert charge["legende"]["graduations"], "la légende reste lisible même sans donnée"


def test_une_vente_unique_ne_prive_pas_la_zone_de_ses_champs(transactions_reelles):
    """Cas dégénéré : une seule vente, donc ni échelle ni tendance calculables."""

    charge, _, _ = donnees_carte.couche_complete(
        transactions_reelles.head(1), CLE_SECTION, "prix_m2_median", 2, True, "Prix médian au m²"
    )
    json.dumps(charge, allow_nan=False)
    assert len(charge["codes"]) == 1
    assert len(charge["valeurs"][0]) == len(charge["champs"])
    assert "non calculable" in charge["notes"][0]
