"""Tests du pipeline DVF.

Chaque test correspond a un piege reellement observe dans les donnees du
depot : lignes repetees, valeur fonciere dupliquee sur chaque ligne, marqueurs
`"None"` / `"nan"`, collision de codes de section entre communes, etc.

Lancement : `pytest -q`
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import geo, telechargement  # noqa: E402
from src.ingestion import (  # noqa: E402
    OptionsNettoyage,
    RapportQualite,
    appliquer_filtres_qualite,
    construire_transactions_territoire,
    reconstruire_mutations,
    retirer_ventes,
    territoire_prepare,
    ventes_retirees,
)
from src.stats import (  # noqa: E402
    ANNEES_MIN_TENDANCE,
    GRIS_DONNEES_INSUFFISANTES,
    PALETTE_SEQUENTIELLE,
    VOLUME_MIN_ANNUEL,
    EchelleCouleur,
    Filtres,
    amplitude_rendement,
    bornes_couleur,
    couleur_rendement,
    couleurs_sections,
    filtrer,
    formater_euros,
    formater_taux_annuel,
    rendement_sections,
    statistiques_sections,
    voies_dominantes,
)

ENTETE = (
    "id_mutation;date_mutation;numero_disposition;nature_mutation;valeur_fonciere;"
    "adresse_numero;adresse_suffixe;adresse_nom_voie;code_postal;code_commune;nom_commune;"
    "code_departement;id_parcelle;lot1_numero;lot1_surface_carrez;nombre_lots;code_type_local;"
    "type_local;surface_reelle_bati;nombre_pieces_principales;surface_terrain;longitude;latitude;"
    "section_prefixe"
)


def ligne(
    id_mutation: str,
    date: str,
    valeur: str,
    type_local: str,
    surface: str,
    pieces: str,
    parcelle: str,
    commune: str = "78311",
    nom_commune: str = "Houilles",
    lot: str = "None",
    nature: str = "Vente",
    carrez: str = "nan",
) -> str:
    return (
        f'"{id_mutation}";"{date}";"1";"{nature}";"{valeur}";"12";"None";"RUE DES TESTS";'
        f'"78800";"{commune}";"{nom_commune}";"78";"{parcelle}";"{lot}";"{carrez}";"1";"2";'
        f'"{type_local}";"{surface}";"{pieces}";"nan";"2.19";"48.92";"{parcelle[5:10]}"'
    )


@pytest.fixture
def ventes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Des lignes DVF brutes aux ventes exploitables, par le chemin de production.

    C'est exactement l'enchainement de `scripts/preparer_territoire.py` :
    lecture d'un fichier departemental geo-dvf, reconstruction des mutations,
    puis filtres de qualite. Les tests ci-dessous decrivent donc des pieges
    reellement traverses par les donnees, pas un chemin de test parallele.
    """

    monkeypatch.setenv("DVF_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(telechargement, "repertoire_cache_dvf", lambda: str(tmp_path))

    def executer(lignes: list[str], options: OptionsNettoyage | None = None, separateur: str = ";"):
        chemin = telechargement.chemin_departement("78", 2025)
        contenu = "\n".join([ENTETE, *lignes]) + "\n"
        if separateur != ";":
            contenu = contenu.replace(";", separateur)
        with gzip.open(chemin, "wt", encoding="utf-8", newline="") as flux:
            flux.write(contenu)

        brut = telechargement.lignes_departement("78", 2025)
        options = options or OptionsNettoyage()
        rapport = RapportQualite()
        rapport.fichiers = [Path(chemin).name]
        rapport.lignes_brutes = len(brut)
        mutations = reconstruire_mutations(brut, options, rapport)
        if mutations.empty:
            return pd.DataFrame(columns=["id_mutation", "prix_m2"]), rapport
        return appliquer_filtres_qualite(mutations, options, rapport), rapport

    return executer


# --------------------------------------------------------------------------
# Reconstruction des mutations
# --------------------------------------------------------------------------


def test_lignes_repetees_ne_doublent_pas_la_surface(ventes):
    """Le meme logement liste deux fois (un lot par ligne) ne compte qu'une fois."""

    transactions, rapport = ventes(
        [
            ligne("M1", "2023-05-02", "420000.0", "Appartement", "60.0", "3.0", "78311000AB0001", lot="2"),
            ligne("M1", "2023-05-02", "420000.0", "Appartement", "60.0", "3.0", "78311000AB0001", lot="5"),
        ]
    )

    assert len(transactions) == 1
    assert rapport.doublons_supprimes == 1
    assert transactions.loc[0, "surface_bati"] == 60.0
    assert transactions.loc[0, "prix_m2"] == 7000.0  # et non 3 500 si la surface doublait


def test_valeur_fonciere_jamais_sommee_et_dependances_ignorees(ventes):
    """La valeur est repetee sur chaque ligne ; la cave n'ajoute pas de surface."""

    transactions, _ = ventes(
        [
            ligne("M2", "2023-06-01", "300000.0", "Appartement", "50.0", "2.0", "78311000AB0002"),
            ligne("M2", "2023-06-01", "300000.0", "Dépendance", "nan", "0.0", "78311000AB0002"),
        ]
    )

    assert len(transactions) == 1
    assert transactions.loc[0, "valeur_fonciere"] == 300_000.0  # pas 600 000
    assert transactions.loc[0, "surface_bati"] == 50.0
    assert transactions.loc[0, "nb_dependances"] == 1
    assert transactions.loc[0, "prix_m2"] == 6000.0


def test_deux_logements_distincts_restent_additionnes(ventes):
    """Le dedoublonnage ne doit pas ecraser deux biens reellement differents."""

    transactions, _ = ventes(
        [
            ligne("M3", "2023-07-01", "800000.0", "Appartement", "50.0", "2.0", "78311000AB0003"),
            ligne("M3", "2023-07-01", "800000.0", "Appartement", "70.0", "3.0", "78311000AB0003"),
        ]
    )

    assert transactions.loc[0, "surface_bati"] == 120.0
    assert transactions.loc[0, "nb_locaux"] == 2


def test_local_activite_et_vente_en_bloc_ecartes(ventes):
    # Les trois lignes de M5 sont identiques : sans lots distincts elles sont
    # vues comme des doublons, on desactive donc le dedoublonnage ici.
    transactions, rapport = ventes(
        [
            ligne("M4", "2023-08-01", "900000.0", "Appartement", "60.0", "3.0", "78311000AB0004"),
            ligne(
                "M4", "2023-08-01", "900000.0",
                "Local industriel. commercial ou assimilé", "120.0", "0.0", "78311000AB0004",
            ),
            *[
                ligne("M5", "2023-09-01", "1500000.0", "Appartement", "40.0", "2.0", "78311000AB0005")
                for _ in range(3)
            ],
        ],
        options=OptionsNettoyage(dedoublonner=False),
    )

    assert transactions.empty
    assert rapport.mutations_avec_local_activite == 1
    assert any("local d'activite" in motif for motif in rapport.rejets)
    assert any("Vente en bloc" in motif for motif in rapport.rejets)


# --------------------------------------------------------------------------
# Lecture et normalisation
# --------------------------------------------------------------------------


def test_marqueurs_none_et_nan_traites_comme_valeurs_manquantes(ventes):
    transactions, rapport = ventes(
        [
            ligne("M6", "2023-01-10", "nan", "Appartement", "45.0", "2.0", "78311000AB0006"),
            ligne("M7", "2023-01-11", "250000.0", "Appartement", "None", "2.0", "78311000AB0007"),
            ligne("M8", "2023-01-12", "250000.0", "Appartement", "50.0", "2.0", "78311000AB0008"),
        ]
    )

    assert list(transactions["id_mutation"]) == ["M8"]
    assert "Valeur fonciere absente" in rapport.rejets
    assert "Surface batie nulle ou absente" in rapport.rejets


def test_surface_absente_reprise_sur_la_surface_carrez(ventes):
    """Un appartement sans `surface_reelle_bati` mais avec une surface de lot.

    La surface Carrez est la mesure legale du lot : s'en servir en dernier
    recours vaut mieux que perdre la vente.
    """

    transactions, rapport = ventes(
        [
            ligne("C1", "2023-02-01", "300000.0", "Appartement", "None", "3.0",
                  "78311000AB0001", lot="1", carrez="60.0"),
            # Aucune des deux surfaces : la vente reste inexploitable.
            ligne("C2", "2023-02-02", "300000.0", "Appartement", "None", "3.0",
                  "78311000AB0002", lot="1", carrez="nan"),
        ]
    )

    assert list(transactions["id_mutation"]) == ["C1"]
    assert transactions.iloc[0]["surface_bati"] == pytest.approx(60.0)
    assert transactions.iloc[0]["prix_m2"] == pytest.approx(5000.0)
    assert rapport.surfaces_reprises_carrez == 1
    assert rapport.rejets["Surface batie nulle ou absente"] == 1


def test_separateur_virgule_et_decimale_virgule(ventes):
    """Etalab publie en virgule, d'autres exports en point-virgule.

    La decimale suit le meme sort : le separateur du fichier est devine a la
    lecture, et la conversion numerique tolere la virgule decimale.
    """

    transactions, _ = ventes(
        [ligne("M9", "2023-02-01", "300000,50", "Appartement", "60,0", "3.0", "78311000AB0009")],
        separateur=",",
    )

    assert len(transactions) == 1
    assert transactions.loc[0, "surface_bati"] == 60.0
    assert transactions.loc[0, "valeur_fonciere"] == pytest.approx(300_000.50)


def test_sections_homonymes_de_communes_differentes_ne_fusionnent_pas(ventes):
    """`000AB` existe dans plusieurs communes : la cle doit inclure l'INSEE."""

    transactions, _ = ventes(
        [
            ligne("MA", "2023-03-01", "300000.0", "Appartement", "50.0", "2.0", "78311000AB0010"),
            ligne(
                "MB", "2023-03-02", "600000.0", "Appartement", "50.0", "2.0", "92063000AB0010",
                commune="92063", nom_commune="Rueil-Malmaison",
            ),
        ]
    )

    assert set(transactions["code_section"]) == {"78311000AB", "92063000AB"}
    assert len(statistiques_sections(transactions)) == 2


def test_ventes_symboliques_et_prix_aberrants_ecartes(ventes):
    transactions, rapport = ventes(
        [
            ligne("MC", "2023-04-01", "1.0", "Maison", "100.0", "5.0", "78311000AB0011"),
            ligne("MD", "2023-04-02", "9000000.0", "Appartement", "30.0", "1.0", "78311000AB0012"),
            ligne("ME", "2023-04-03", "400000.0", "Maison", "80.0", "4.0", "78311000AB0013"),
        ]
    )

    assert list(transactions["id_mutation"]) == ["ME"]
    assert rapport.mutations_finales == 1
    assert sum(rapport.rejets.values()) == 2


def test_natures_de_mutation_filtrees_par_defaut(ventes):
    lignes = [
        ligne("MF", "2023-05-01", "400000.0", "Maison", "80.0", "4.0", "78311000AB0014", nature="Echange"),
        ligne("MG", "2023-05-02", "400000.0", "Maison", "80.0", "4.0", "78311000AB0015"),
    ]
    transactions, _ = ventes(lignes)
    assert list(transactions["id_mutation"]) == ["MG"]

    # Le parquet d'un territoire conserve toutes les natures, precisement pour
    # que ce filtre reste vivant dans la barre laterale.
    toutes, _ = ventes(lignes, options=OptionsNettoyage(natures_mutation=None))
    assert set(toutes["id_mutation"]) == {"MF", "MG"}


# --------------------------------------------------------------------------
# Statistiques et evolution annuelle du prix
# --------------------------------------------------------------------------


@pytest.fixture
def transactions_synthetiques() -> pd.DataFrame:
    """Deux sections contrastees sur 2021-2024.

    * `78311000AB` : 6 ventes par an, prix au m2 en hausse geometrique de
      exactement 10 % par an — la tendance attendue est connue a l'avance.
    * `78311000AC` : une seule vente par an, prix en dents de scie — aucune
      annee n'atteint le plancher de volume, la tendance est incalculable.
    """

    lignes = []
    for rang, annee in enumerate((2021, 2022, 2023, 2024)):
        prix_m2 = 5000.0 * (1.10**rang)
        for index in range(6):
            lignes.append(
                {
                    "id_mutation": f"{annee}-{index}",
                    "date_mutation": pd.Timestamp(f"{annee}-06-0{index + 1}"),
                    "annee": annee,
                    "code_section": "78311000AB",
                    "section_courte": "AB",
                    "nom_commune": "Houilles",
                    "code_commune": "78311",
                    "type_bien": "Appartement",
                    "surface_bati": 50.0,
                    "nb_pieces": 2.0,
                    "valeur_fonciere": prix_m2 * 50.0,
                    "prix_m2": prix_m2,
                    "nb_dependances": 0,
                    "adresse": "1 Rue des Tests",
                    "nature_mutation": "Vente",
                    "latitude": 48.92,
                    "longitude": 2.19,
                }
            )
    for annee, prix_m2 in ((2021, 4000), (2022, 6000), (2023, 4200), (2024, 6500)):
        lignes.append(
            {
                "id_mutation": f"rare-{annee}",
                "date_mutation": pd.Timestamp(f"{annee}-06-15"),
                "annee": annee,
                "code_section": "78311000AC",
                "section_courte": "AC",
                "nom_commune": "Houilles",
                "code_commune": "78311",
                "type_bien": "Maison",
                "surface_bati": 100.0,
                "nb_pieces": 5.0,
                "valeur_fonciere": prix_m2 * 100.0,
                "prix_m2": float(prix_m2),
                "nb_dependances": 0,
                "adresse": "2 Rue des Tests",
                "nature_mutation": "Vente",
                "latitude": 48.93,
                "longitude": 2.20,
            }
        )
    return pd.DataFrame(lignes)


def test_rendement_retrouve_un_taux_de_croissance_connu(transactions_synthetiques):
    rendement = rendement_sections(transactions_synthetiques, "prix_m2_median")
    active = rendement.set_index("code_section").loc["78311000AB"]

    # Les medianes annuelles suivent exactement 1,10^n : la pente en log doit
    # redonner 10 %/an, et le cumul sur 3 ans 1,10^3 - 1 = 33,1 %.
    assert active["taux_annuel"] == pytest.approx(10.0)
    assert active["variation_cumulee"] == pytest.approx(33.1, abs=0.05)
    assert active["nb_annees"] == 4
    assert active["nb_ventes"] == 24
    assert active["regularite"] == pytest.approx(1.0)  # alignement parfait
    assert bool(active["fiable"]) is True
    assert formater_taux_annuel(active["taux_annuel"]) == "+10,0 %/an"


def test_rendement_ignore_les_annees_trop_peu_fournies(transactions_synthetiques):
    rendement = rendement_sections(transactions_synthetiques, "prix_m2_median")
    codes = set(rendement["code_section"])

    # Aucune annee de `78311000AC` n'atteint VOLUME_MIN_ANNUEL : la section
    # n'apporte aucun point de tendance, donc pas de taux inventé.
    assert "78311000AC" not in codes
    assert couleur_rendement(None, 10.0) == GRIS_DONNEES_INSUFFISANTES


def test_rendement_exige_assez_d_annees(transactions_synthetiques):
    # Restreinte a deux annees, la section active n'atteint plus le plancher.
    deux_annees = transactions_synthetiques[transactions_synthetiques["annee"] <= 2022]
    rendement = rendement_sections(deux_annees, "prix_m2_median")
    active = rendement.set_index("code_section").loc["78311000AB"]

    assert active["nb_annees"] == 2 < ANNEES_MIN_TENDANCE
    assert bool(active["fiable"]) is False
    assert pd.isna(active["taux_annuel"])


def test_rendement_signale_une_tendance_irreguliere():
    """Memes prix de depart et d'arrivee, regularite opposee.

    Le R2 est la garantie qu'un taux ne se lit pas comme une tendance
    installee : les deux sections partent de 5 000 et arrivent a 5 788 EUR/m2,
    mais la seconde y va en zigzag. Sa tendance ajustee tombe donc a plat —
    ce qui est la bonne reponse — et son R2 le dit.
    """

    lignes = []
    series = {
        "78311000AA": [5000.0, 5250.0, 5512.5, 5788.1],  # +5 %/an, regulier
        "78311000AB": [5000.0, 6500.0, 4200.0, 5788.1],  # meme depart/arrivee, chaotique
    }
    for code, prix_annuels in series.items():
        for annee, prix in zip((2021, 2022, 2023, 2024), prix_annuels):
            for index in range(VOLUME_MIN_ANNUEL):
                lignes.append(
                    {
                        "id_mutation": f"{code}-{annee}-{index}",
                        "annee": annee,
                        "code_section": code,
                        "prix_m2": prix,
                        "valeur_fonciere": prix * 50,
                        "surface_bati": 50.0,
                    }
                )
    rendement = rendement_sections(pd.DataFrame(lignes), "prix_m2_median").set_index("code_section")

    assert rendement.loc["78311000AA", "taux_annuel"] == pytest.approx(5.0, abs=0.1)
    assert rendement.loc["78311000AA", "regularite"] > 0.99

    # Comparer le seul premier au seul dernier millesime donnerait ici la meme
    # hausse pour les deux sections. La tendance ajustee, elle, refuse de voir
    # une pente dans un zigzag, et le R2 explique pourquoi.
    assert abs(rendement.loc["78311000AB", "taux_annuel"]) < 1.0
    assert rendement.loc["78311000AB", "regularite"] < 0.05


def test_rendement_est_plus_stable_que_la_comparaison_de_deux_millesimes():
    """Raison d'etre du mode : deux millesimes se croisent au hasard.

    Sur un marche en dents de scie ou une annee sur deux voit se vendre des
    biens un peu plus chers, comparer N a N-1 renvoie une alternance de +20 %
    et -17 %. La tendance ajustee retient bien moins : la serie se termine sur
    une annee haute, il en reste 3,7 %/an, soit cinq fois moins — et son R2
    tres bas signale que cette pente ne veut rien dire.
    """

    lignes = []
    for annee, prix in ((2021, 5000.0), (2022, 6000.0), (2023, 5000.0), (2024, 6000.0)):
        for index in range(VOLUME_MIN_ANNUEL):
            lignes.append(
                {
                    "id_mutation": f"{annee}-{index}",
                    "annee": annee,
                    "code_section": "78311000AB",
                    "prix_m2": prix,
                    "valeur_fonciere": prix * 50,
                    "surface_bati": 50.0,
                }
            )
    rendement = rendement_sections(pd.DataFrame(lignes), "prix_m2_median")
    taux = float(rendement.iloc[0]["taux_annuel"])

    comparaison_n_vs_n1 = (6000.0 - 5000.0) / 5000.0 * 100  # +20 % en 2024 vs 2023
    assert abs(taux) < comparaison_n_vs_n1 / 5
    # Sur cette serie en dents de scie, le R2 vaut exactement 0,2 : la borne
    # est desserree pour ne pas dependre de l'ordre des sommations en virgule
    # flottante, qui place le resultat a 1e-10 de part et d'autre.
    assert float(rendement.iloc[0]["regularite"]) == pytest.approx(0.2, abs=1e-6)


def test_rendement_sans_donnee():
    vide = rendement_sections(pd.DataFrame(), "prix_m2_median")
    assert vide.empty
    assert "taux_annuel" in vide.columns


def test_amplitude_rendement_se_cale_sur_un_palier_lisible():
    # Une amplitude recalculee au centieme changerait la legende a chaque clic :
    # elle est arrondie au palier superieur.
    assert amplitude_rendement(pd.Series([1.2, -0.8, 1.9])) == 2.0
    assert amplitude_rendement(pd.Series([-4.1, 3.0, 4.9])) == 5.0
    assert amplitude_rendement(pd.Series([-40.0, 38.0])) == 20.0  # plafonnee
    assert amplitude_rendement(pd.Series(dtype=float)) > 0

    amplitude = amplitude_rendement(pd.Series([5.0, -5.0]))
    assert couleur_rendement(0.0, amplitude) == "#ffffff"
    assert couleur_rendement(-amplitude, amplitude) == "#67001f"
    assert couleur_rendement(amplitude, amplitude) == "#00441b"


def test_statistiques_sections_extremes(transactions_synthetiques: pd.DataFrame):
    stats = statistiques_sections(transactions_synthetiques).set_index("code_section")
    active = stats.loc["78311000AB"]

    # 4 annees x 6 ventes, prix au m2 de 5 000 a 5 000 x 1,10^3 = 6 655.
    assert active["nb_transactions"] == 24
    # Mediane de 24 valeurs : moyenne des 12e et 13e, soit (5 500 + 6 050) / 2.
    assert active["prix_m2_median"] == pytest.approx(5775.0)
    assert active["prix_max"] == pytest.approx(5000.0 * 1.10**3 * 50)
    assert active["prix_min"] == pytest.approx(250_000.0)
    assert active["surface_prix_max"] == 50.0


def test_filtres_bornes_ouvertes(transactions_synthetiques: pd.DataFrame):
    # La borne haute ouverte doit conserver les biens au-dela du curseur.
    large = filtrer(
        transactions_synthetiques,
        Filtres(surface=(0, 60), surface_max_ouvert=True),
    )
    assert len(large) == len(transactions_synthetiques)

    stricte = filtrer(
        transactions_synthetiques,
        Filtres(surface=(0, 60), surface_max_ouvert=False),
    )
    assert set(stricte["surface_bati"]) == {50.0}

    # Les annees retenues forment un ensemble, pas un intervalle : on peut
    # ecarter une annee creuse sans toucher a celles qui l'encadrent.
    periode = filtrer(transactions_synthetiques, Filtres(annees=(2021, 2023)))
    assert set(periode["annee"]) == {2021, 2023}


def test_couleurs_des_ventes():
    # Bornes robustes : l'echelle est calee sur les 5e-95e centiles, donc une
    # serie reguliere de 3000 a 7000 place le vert fonce et le rouge fonce aux
    # extremites utiles.
    echelle = EchelleCouleur.depuis([3000.0, 5000.0, 7000.0])
    assert echelle.couleur(echelle.bas) == "#00695c"  # vert fonce (le moins cher)
    assert echelle.couleur(5000.0) == "#ffe17a"  # jaune (milieu de gamme)
    assert echelle.couleur(echelle.haut) == "#a50026"  # rouge fonce (le plus cher)
    assert echelle.couleur(None) == GRIS_DONNEES_INSUFFISANTES
    assert echelle.couleur(pd.NA) == GRIS_DONNEES_INSUFFISANTES
    # Au-dela des bornes, la couleur sature au lieu de deborder de la palette.
    assert echelle.couleur(50_000.0) == "#a50026"

    assert bornes_couleur(pd.Series(dtype=float)) == (0.0, 1.0)
    assert formater_euros(pd.NA) == "n/d"


# --------------------------------------------------------------------------
# Geometries
# --------------------------------------------------------------------------


def test_contours_locaux_filtres_sur_les_sections_presentes(tmp_path: Path, monkeypatch):
    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": "78311000AB", "commune": "78311", "prefixe": "000", "code": "AB"},
                "geometry": {"type": "Polygon", "coordinates": [[[2.1, 48.9], [2.2, 48.9], [2.2, 49.0], [2.1, 48.9]]]},
            },
            {
                "type": "Feature",
                "properties": {"id": "78311000ZZ", "commune": "78311", "prefixe": "000", "code": "ZZ"},
                "geometry": {"type": "Polygon", "coordinates": [[[2.3, 48.9], [2.4, 48.9], [2.4, 49.0], [2.3, 48.9]]]},
            },
        ],
    }
    dossier = tmp_path / "geo"
    dossier.mkdir()
    (dossier / "78311-sections.geojson").write_text(json.dumps(collection), encoding="utf-8")
    monkeypatch.setenv("DVF_GEO_DIR", str(dossier))
    monkeypatch.setenv("DVF_CACHE_DIR", str(tmp_path / "cache"))

    resultat = geo.charger_sections(["78311"], ["78311000AB"], autoriser_reseau=False)

    codes = [e["properties"]["code_section"] for e in resultat.geojson["features"]]
    assert codes == ["78311000AB"]
    assert resultat.communes_absentes == []
    assert geo.etendue(resultat.geojson) is not None


def test_contours_deja_normalises_sont_relus(tmp_path: Path, monkeypatch):
    """Les contours deja normalises se relisent, comme ceux d'Etalab.

    `telecharger_contours_manquants` ecrit des contours **deja normalises**
    (`code_section`, `code_commune`, `section_courte`) dans `data/geo/`, pour
    etre committes et servis hors ligne. La normalisation doit reconnaitre ce
    schema en plus de ceux d'Etalab (`id`, `commune`, `code`), sans quoi ces
    communes seraient declarees « sans contour ».
    """

    entites = [
        {
            "type": "Feature",
            "properties": {
                "code_section": "75101000AB",
                "code_commune": "75101",
                "section_courte": "AB",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[2.3, 48.86], [2.35, 48.86], [2.35, 48.87], [2.3, 48.86]]],
            },
        }
    ]
    dossier = tmp_path / "geo"
    dossier.mkdir()
    (dossier / "75101-sections.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": entites}), encoding="utf-8"
    )
    monkeypatch.setenv("DVF_GEO_DIR", str(dossier))
    monkeypatch.setenv("DVF_CACHE_DIR", str(tmp_path / "cache"))

    resultat = geo.charger_sections(["75101"], ["75101000AB"], autoriser_reseau=False)

    assert [e["properties"]["code_section"] for e in resultat.geojson["features"]] == [
        "75101000AB"
    ]
    assert resultat.communes_absentes == []
    assert geo.sections_locales("75101") == {"75101000AB"}


def test_normalisation_des_contours_tolere_les_trois_schemas():
    """Etalab brut, API Carto IGN, et la sortie normalisee de ce module."""

    collection = {
        "features": [
            # Etalab : `id` complet.
            {
                "properties": {"id": "78311000AB", "commune": "78311", "code": "AB"},
                "geometry": {"type": "Polygon", "coordinates": [[[2.1, 48.9]]]},
            },
            # API Carto IGN : departement + commune separes, section seule.
            {
                "properties": {"code_dep": "78", "code_com": "311", "com_abs": "000", "section": "AC"},
                "geometry": {"type": "Polygon", "coordinates": [[[2.1, 48.9]]]},
            },
            # Sortie de ce module, relue depuis `data/geo/`.
            {
                "properties": {"code_section": "78311000AD", "code_commune": "78311"},
                "geometry": {"type": "Polygon", "coordinates": [[[2.1, 48.9]]]},
            },
        ]
    }
    codes = [
        e["properties"]["code_section"] for e in geo._normaliser_collection(collection, "78311")
    ]
    assert codes == ["78311000AB", "78311000AC", "78311000AD"]

    # Sans section identifiable, l'entite est ecartee plutot que rattachee au
    # hasard a une autre section.
    sans_section = {"features": [{"properties": {"commune": "78311"}, "geometry": {"type": "Polygon", "coordinates": []}}]}
    assert geo._normaliser_collection(sans_section, "78311") == []


def test_commune_sans_contour_est_signalee_sans_planter(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DVF_GEO_DIR", str(tmp_path / "vide"))
    monkeypatch.setenv("DVF_CACHE_DIR", str(tmp_path / "cache"))

    resultat = geo.charger_sections(["99999"], None, autoriser_reseau=False)

    assert resultat.communes_absentes == ["99999"]
    assert resultat.geojson["features"] == []


def test_gares_hors_ligne_ne_bloquent_pas(cache_gares, monkeypatch):
    gares, message = geo.charger_gares((48.9, 2.1, 48.95, 2.25), autoriser_reseau=False)
    assert gares == []
    assert message

    assert geo.charger_gares(None, autoriser_reseau=True) == ([], None)


# --------------------------------------------------------------------------
# Donnees reelles du depot
# --------------------------------------------------------------------------


#: Territoire livre avec le depot. Les deux tests ci-dessous portent sur lui :
#: ce sont les seules verifications de bout en bout sur des donnees reelles.
TERRITOIRE_LIVRE = "grand-paris"


@pytest.fixture(scope="module")
def ventes_du_depot():
    if not territoire_prepare(TERRITOIRE_LIVRE):
        pytest.skip(f"territoire {TERRITOIRE_LIVRE} non prepare")
    transactions, rapport = construire_transactions_territoire(TERRITOIRE_LIVRE)
    return transactions, rapport


# --------------------------------------------------------------------------
# Ventes retirees a la demande (droit d'opposition)
# --------------------------------------------------------------------------


def test_le_fichier_des_retraits_se_lit_et_refuse_une_ligne_douteuse(tmp_path):
    chemin = tmp_path / "retraits.csv"
    chemin.write_text(
        "# commentaire\n\n2024-03-15;75111;12  Rue OBERKAMPF \n2023-01-02;2A004;1 Cours Napoleon\n",
        encoding="utf-8",
    )
    assert ventes_retirees(str(chemin)) == {
        ("2024-03-15", "75111", "12 rue oberkampf"),
        ("2023-01-02", "2A004", "1 cours napoleon"),
    }
    assert ventes_retirees(str(tmp_path / "absent.csv")) == set()
    # Une ligne mal formee arrete la construction : la vente resterait en ligne.
    for douteuse in ("15/03/2024;75111;12 Rue Oberkampf", "2024-03-15;Paris;12 Rue Oberkampf",
                     "2024-03-15;75111"):
        chemin.write_text(douteuse + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            ventes_retirees(str(chemin))


def test_une_vente_retiree_disparait_et_elle_seule():
    mutations = pd.DataFrame({
        "date_mutation": pd.to_datetime(["2024-03-15", "2024-03-15", "2024-03-16", "2024-03-15"]),
        "code_commune": ["75111", "75111", "75111", "93048"],
        "adresse": ["12 Rue Oberkampf", "14 Rue Oberkampf", "12 Rue Oberkampf", "12 Rue Oberkampf"],
    })
    reste = retirer_ventes(mutations, {("2024-03-15", "75111", "12 rue oberkampf")})
    assert len(reste) == 3 and not (
        (reste["adresse"] == "12 Rue Oberkampf") & (reste["code_commune"] == "75111")
        & (reste["date_mutation"] == "2024-03-15")
    ).any()
    assert retirer_ventes(mutations, set()) is mutations


def test_le_site_applique_les_retraits(ventes_du_depot, tmp_path, monkeypatch):
    transactions, _ = ventes_du_depot
    adressees = transactions[transactions["adresse"].str.strip() != ""]
    vente = adressees.iloc[len(adressees) // 2]
    jour = f"{pd.Timestamp(vente['date_mutation']):%Y-%m-%d}"
    memes = transactions[
        (transactions["date_mutation"] == vente["date_mutation"])
        & (transactions["code_commune"] == vente["code_commune"])
        & (transactions["adresse"] == vente["adresse"])
    ]
    chemin = tmp_path / "retraits.csv"
    chemin.write_text(f"{jour};{vente['code_commune']};{vente['adresse'].upper()}\n", encoding="utf-8")
    monkeypatch.setenv("DVF_RETRAITS", str(chemin))
    apres, _ = construire_transactions_territoire(TERRITOIRE_LIVRE)
    assert len(apres) == len(transactions) - len(memes)
    assert not apres["id_mutation"].isin(memes["id_mutation"]).any()


def test_le_depot_ne_retire_que_des_ventes_bien_designees():
    """`data/retraits.csv` se lit : une ligne fautive casserait la construction."""

    ventes_retirees()


def test_donnees_du_depot_sont_exploitables(ventes_du_depot):
    transactions, rapport = ventes_du_depot

    assert len(transactions) > 100_000
    assert rapport.taux_retenu > 0.9
    assert transactions["prix_m2"].between(500, 30_000).all()
    assert transactions["code_section"].str.len().eq(10).all()
    assert transactions["date_mutation"].notna().all()

    contours = geo.charger_sections(
        transactions["code_commune"].unique(),
        transactions["code_section"].unique(),
        autoriser_reseau=False,
    )
    couvertes = {e["properties"]["code_section"] for e in contours.geojson["features"]}
    assert all(len(code) == 10 for code in couvertes)

    # Tout fichier present dans `data/geo/` doit etre exploitable hors ligne :
    # un GeoJSON committe mais illisible est un bug, pas un defaut
    # d'approvisionnement.
    for chemin in sorted(Path(geo.repertoire_geo()).rglob("*-sections.geojson")):
        insee = chemin.name[:5]
        assert geo.sections_locales(insee), f"{chemin.name} relu comme vide"
        assert insee in contours.sources, f"{insee} absent des sources alors que {chemin.name} existe"

    # Et l'inverse : aucune commune vendue ne doit manquer a la carte. Si un
    # jour une commune est ajoutee sans son cadastre, ce test le dira — c'est
    # `scripts/preparer_deploiement.py` qui la recupere.
    orphelines = set(transactions["code_section"]) - couvertes
    communes_sans_contour = {code[:5] for code in orphelines} - {code[:5] for code in couvertes}
    assert not communes_sans_contour, (
        f"Commune(s) sans aucun contour : {sorted(communes_sans_contour)[:5]}. "
        "Lancez `python scripts/preparer_deploiement.py`."
    )
    # Une section isolee peut en revanche manquer apres une mise a jour
    # semestrielle : sa codification a change cote DVF ou cote cadastre. Elle
    # est listee dans la pull request de mise a jour (`resumer_mise_a_jour.py`)
    # plutot que de la bloquer — tant qu'elle reste marginale.
    invisibles = int(transactions["code_section"].isin(orphelines).sum())
    assert invisibles <= 0.005 * len(transactions), (
        f"{len(orphelines)} section(s) sans contour, soit {invisibles} vente(s) absentes de la "
        f"carte : {sorted(orphelines)[:5]}. Lancez `python scripts/preparer_deploiement.py`."
    )


def test_rendement_du_depot_est_plausible_et_plus_stable(ventes_du_depot):
    """Le mode rendement doit rester lisible sur le territoire livre.

    Deux garde-fous : des taux dans un ordre de grandeur credible pour un
    marche immobilier, et une dispersion nettement inferieure a celle d'une
    comparaison de deux millesimes — c'est la raison d'etre du mode.
    """

    transactions, _ = ventes_du_depot
    rendement = rendement_sections(transactions, "prix_m2_median")
    fiables = rendement.loc[rendement["fiable"], "taux_annuel"]

    # La majorite des sections doit etre calculable, sinon la carte est grise
    # et le mode ne sert a rien.
    assert len(fiables) > 0.6 * len(rendement)
    assert fiables.between(-40, 40).all(), (
        "Taux hors de tout ordre de grandeur immobilier : "
        f"{fiables[~fiables.between(-40, 40)].round(1).tolist()[:5]}"
    )

    # Dispersion contre la comparaison brute de la derniere annee a la
    # precedente, sur les memes sections.
    annee_max = int(transactions["annee"].max())
    par_annee = (
        transactions.groupby(["code_section", "annee"])["prix_m2"].median().unstack("annee")
    )
    deux_millesimes = (
        (par_annee[annee_max] - par_annee[annee_max - 1]) / par_annee[annee_max - 1] * 100
    ).dropna()
    assert fiables.std() < deux_millesimes.std() / 2


# --------------------------------------------------------------------------
# Gares : quotas des serveurs publics et sources de repli
# --------------------------------------------------------------------------


class _FausseReponse:
    """Reponse HTTP minimale, suffisante pour les parseurs du module `geo`."""

    def __init__(self, charge: dict, code: int = 200):
        self._charge = charge
        self.status_code = code

    def raise_for_status(self):
        if self.status_code >= 400:
            erreur = Exception(f"{self.status_code} Client Error")
            erreur.response = self  # comme `requests.HTTPError`
            raise erreur

    def json(self):
        return self._charge


def _enregistrement_idfm(nom: str, res_com: str, mode: str = "METRO", **extra) -> dict:
    """Un enregistrement Opendatasoft tel que le rend le portail francilien."""

    return {
        "geometry": {"type": "Point", "coordinates": [2.1905, 48.9245]},
        "fields": {"nom_zdc": nom, "res_com": res_com, "mode": mode, **extra},
    }


class _FauxRequests:
    """Simule Overpass et les portails Opendatasoft.

    Chaque source nomme differemment la meme gare de Houilles : c'est ainsi que
    les tests savent laquelle a servi. Les pages Opendatasoft sont decoupees
    comme celles de l'API v1, `nhits` compris — sans quoi rien ne verifierait
    que la pagination va jusqu'au bout.
    """

    def __init__(
        self,
        overpass_code: int = 200,
        opendatasoft_code: int = 200,
        idfm_code: int = 200,
        remarque_overpass: str | None = None,
        gares_idfm: list[dict] | None = None,
        projets_idfm: list[dict] | None = None,
    ):
        self.overpass_code = overpass_code
        self.opendatasoft_code = opendatasoft_code
        self.idfm_code = idfm_code
        self.remarque_overpass = remarque_overpass
        self.gares_idfm = (
            gares_idfm
            if gares_idfm is not None
            else [_enregistrement_idfm("Houilles - Carrières", "TRAIN J", "TRAIN")]
        )
        self.projets_idfm = projets_idfm if projets_idfm is not None else []
        self.appels: list[str] = []
        self.requetes: list[str] = []
        self.jeux: list[str] = []

    def post(self, url, data=None, timeout=None, headers=None):
        self.appels.append(url)
        self.requetes.append((data or {}).get("data", ""))
        charge: dict = {
            "elements": [
                {
                    "lat": 48.9245,
                    "lon": 2.1905,
                    "tags": {"name": "Gare de Houilles", "railway": "station"},
                }
            ]
        }
        if self.remarque_overpass:
            charge["remark"] = self.remarque_overpass
        return _FausseReponse(charge, self.overpass_code)

    def get(self, url, params=None, timeout=None, headers=None):
        self.appels.append(url)
        params = params or {}
        self.jeux.append(str(params.get("dataset")))
        if geo.URL_IDFM in url:
            jeu = params.get("dataset")
            tous = self.gares_idfm if jeu == geo.JEU_IDFM_GARES else self.projets_idfm
            debut = int(params.get("start", 0))
            lot = int(params.get("rows", geo.LOT_OPENDATASOFT))
            return _FausseReponse(
                {"nhits": len(tous), "records": tous[debut : debut + lot]}, self.idfm_code
            )
        return _FausseReponse(
            {
                "nhits": 2,
                "records": [
                    {
                        "geometry": {"type": "Point", "coordinates": [2.1905, 48.9245]},
                        "fields": {"libelle": "Houilles Carrières-sur-Seine"},
                    },
                    {  # enregistrement sans geometrie : doit etre ignore
                        "fields": {"libelle": "Gare fantôme"},
                    },
                ],
            },
            self.opendatasoft_code,
        )


@pytest.fixture
def cache_gares(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isole `data/geo/` ET `data/cache/` : les gares se memorisent dans le
    premier, et un test ne doit jamais ecrire dans le depot."""

    dossier_geo = tmp_path / "geo"
    dossier_geo.mkdir()
    monkeypatch.setenv("DVF_GEO_DIR", str(dossier_geo))
    monkeypatch.setenv("DVF_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


BOITE = (48.90, 2.15, 48.95, 2.25)
#: Emprise hors d'Île-de-France : le referentiel francilien n'y a rien a dire,
#: et ne doit pas etre interroge.
BOITE_BRETAGNE = (48.05, -1.80, 48.20, -1.60)


def test_gares_idfm_prioritaire_en_ile_de_france(cache_gares, monkeypatch):
    """Sur un territoire francilien, le referentiel regional passe devant.

    C'est le seul qui soit exhaustif — le fichier livre par OpenStreetMap
    ignorait Bourg-la-Reine et les stations de Montreuil — et le seul a nommer
    les lignes desservies.
    """

    faux = _FauxRequests()
    monkeypatch.setattr(geo, "requests", faux)

    gares, message = geo.charger_gares(BOITE)

    assert [g["nom"] for g in gares] == ["Houilles - Carrières"]
    assert gares[0]["lignes"] == ["Train J"]
    assert gares[0]["statut"] == geo.STATUT_SERVICE
    assert "Île-de-France Mobilités" in message
    assert "téléchargement unique" in message
    # Aucun miroir Overpass n'a ete sollicite : la premiere source a suffi.
    assert not any("overpass" in url for url in faux.appels)


def test_gares_idfm_non_interroge_hors_ile_de_france(cache_gares, monkeypatch):
    """Ailleurs, il ne livrerait qu'un reseau tronque : on passe a OSM."""

    faux = _FauxRequests()
    monkeypatch.setattr(geo, "requests", faux)

    gares, message = geo.charger_gares(BOITE_BRETAGNE)

    assert not any(geo.URL_IDFM in url for url in faux.appels)
    assert "OpenStreetMap" in message
    # La gare renvoyee par le faux Overpass est hors emprise bretonne : ce qui
    # compte ici est la source interrogee, pas le contenu.
    assert gares == []


def test_gares_bascule_sur_osm_puis_sncf_quand_les_sources_echouent(cache_gares, monkeypatch):
    """Le cas signale en production : quota Overpass atteint sur tous les miroirs."""

    faux = _FauxRequests(idfm_code=500, overpass_code=429)
    monkeypatch.setattr(geo, "requests", faux)

    gares, message = geo.charger_gares(BOITE)

    assert [g["nom"] for g in gares] == ["Houilles Carrières-sur-Seine"]
    assert "SNCF Open Data" in message
    assert "429" in message and "500" in message  # les echecs restent visibles
    # Tous les miroirs Overpass ont ete tentes avant de basculer.
    assert sum(1 for url in faux.appels if "overpass" in url) == len(geo.URLS_OVERPASS)


def test_gares_toutes_sources_en_echec_reste_non_bloquant(cache_gares, monkeypatch):
    faux = _FauxRequests(idfm_code=500, overpass_code=429, opendatasoft_code=503)
    monkeypatch.setattr(geo, "requests", faux)

    gares, message = geo.charger_gares(BOITE)

    assert gares == []
    assert "Gares indisponibles" in message
    assert "429" in message and "503" in message


def test_reponse_overpass_partielle_est_un_echec(cache_gares, monkeypatch):
    """Une reponse Overpass partielle ne doit jamais etre memorisee.

    Au-dela du temps imparti, Overpass repond `200` avec un `remark`
    d'avertissement et les seuls objets qu'il a eu le temps de parcourir.
    Ecrite sur disque, cette liste tronquee marquerait l'emprise couverte et ne
    serait plus jamais reprise. Une reponse partielle compte donc pour un
    echec, et laisse la place a la source suivante.
    """

    faux = _FauxRequests(
        idfm_code=500, remarque_overpass="runtime error: Query timed out in 'recurse'"
    )
    monkeypatch.setattr(geo, "requests", faux)

    gares, message = geo.charger_gares(BOITE)

    # La source suivante a pris le relais, et l'emprise n'a pas ete figee sur
    # une reponse incomplete.
    assert [g["nom"] for g in gares] == ["Houilles Carrières-sur-Seine"]
    assert "partielle" in message
    assert sum(1 for url in faux.appels if "overpass" in url) == len(geo.URLS_OVERPASS)


def test_overpass_decoupe_une_large_emprise_en_tuiles(cache_gares, monkeypatch):
    """Une requete par tuile : c'est ce qui la maintient loin du temps imparti."""

    faux = _FauxRequests(idfm_code=500)
    monkeypatch.setattr(geo, "requests", faux)

    large = (48.0, -2.0, 48.6, -1.4)  # 0,6 degre de cote, hors Île-de-France
    geo.charger_gares(large)

    tuiles = len(geo._tuiles(large, geo.PAS_TUILE_OVERPASS))
    assert tuiles == 9
    assert sum(1 for url in faux.appels if "overpass" in url) == tuiles
    # Les arrets de tramway sont demandes, comme les gares a venir.
    assert "tram_stop" in faux.requetes[0]
    assert "construction:railway" in faux.requetes[0]


def test_opendatasoft_pagine_jusqu_au_dernier_enregistrement(cache_gares, monkeypatch):
    """Le portail francilien compte plus de mille enregistrements : la
    pagination va jusqu'a `nhits`, sans en perdre un seul."""

    monkeypatch.setattr(geo, "LOT_OPENDATASOFT", 2)
    faux = _FauxRequests(
        gares_idfm=[_enregistrement_idfm(f"Station {i}", "METRO 9") for i in range(5)]
    )
    monkeypatch.setattr(geo, "requests", faux)

    gares, _ = geo.charger_gares(BOITE)

    assert sorted(g["nom"] for g in gares) == [f"Station {i}" for i in range(5)]
    # Cinq enregistrements par pages de deux : trois pages, et pas une de moins.
    assert faux.jeux.count(geo.JEU_IDFM_GARES) == 3


def test_gares_a_venir_s_ajoutent_a_celles_en_service(cache_gares, monkeypatch):
    """Les gares du Grand Paris Express, et le pole qu'une ligne a venir rejoint.

    Une gare annoncee la ou rien ne s'arrete aujourd'hui est un point de plus,
    en projet ; annoncee sur un pole deja desservi, elle s'y fond — le pole
    existe, il gagne seulement une ligne.
    """

    faux = _FauxRequests(
        gares_idfm=[_enregistrement_idfm("Châtillon Montrouge", "METRO 13")],
        projets_idfm=[
            {
                "geometry": {"type": "Point", "coordinates": [2.1905, 48.9245]},
                "fields": {
                    "nom_arret": "Châtillon Montrouge", "res_com": "METRO 15",
                    "mode": "métro", "statut": "Travaux",
                },
            },
            {
                "geometry": {"type": "Point", "coordinates": [2.20, 48.93]},
                "fields": {
                    "nom_arret": "Vitry Centre", "res_com": "METRO 15",
                    "mode": "métro", "statut": "AVP",
                },
            },
            {  # un projet de bus n'est pas une gare
                "geometry": {"type": "Point", "coordinates": [2.21, 48.93]},
                "fields": {
                    "nom_arret": "Arrêt Altival", "res_com": "BUS ALTIVAL",
                    "mode": "bus", "statut": "Travaux",
                },
            },
        ],
    )
    monkeypatch.setattr(geo, "requests", faux)

    gares, message = geo.charger_gares(BOITE)
    par_nom = {g["nom"]: g for g in gares}

    assert set(par_nom) == {"Châtillon Montrouge", "Vitry Centre"}
    # Le pole existant garde son statut et annonce la ligne a venir, sans la
    # confondre avec celle qu'on y prend aujourd'hui.
    assert par_nom["Châtillon Montrouge"]["statut"] == geo.STATUT_SERVICE
    assert par_nom["Châtillon Montrouge"]["lignes"] == ["Métro 13"]
    assert par_nom["Châtillon Montrouge"]["lignes_a_venir"] == ["Métro 15"]
    # « Travaux » cote IDFM est un chantier ouvert ; les autres valeurs
    # (avant-projet, DUP, etudes prealables) sont des etapes d'etude.
    assert par_nom["Vitry Centre"]["statut"] == geo.STATUT_PROJET
    # Le mode decide de la taille du point, pas l'exploitant.
    assert par_nom["Vitry Centre"]["type"] == "subway"
    assert geo.rang_gare(par_nom["Vitry Centre"]) == geo.RANG_LEGER
    assert "1 à venir" in message


def test_echec_des_projets_ne_prive_pas_la_carte_du_reseau_actuel(cache_gares, monkeypatch):
    """Un supplement indisponible ne doit pas couter les gares en service."""

    class _ProjetsEnPanne(_FauxRequests):
        def get(self, url, params=None, timeout=None, headers=None):
            if (params or {}).get("dataset") == geo.JEU_IDFM_PROJETS:
                return _FausseReponse({}, 503)
            return super().get(url, params=params, timeout=timeout, headers=headers)

    faux = _ProjetsEnPanne()
    monkeypatch.setattr(geo, "requests", faux)

    gares, message = geo.charger_gares(BOITE)

    assert [g["nom"] for g in gares] == ["Houilles - Carrières"]
    assert "503" in message


def test_gares_telechargees_une_seule_fois(cache_gares, monkeypatch):
    """Le fichier `data/geo/gares.json` rend le telechargement definitif."""

    faux = _FauxRequests()
    monkeypatch.setattr(geo, "requests", faux)
    geo.charger_gares(BOITE)
    appels_initiaux = len(faux.appels)

    gares, message = geo.charger_gares(BOITE)

    assert len(gares) == 1
    assert "aucun appel réseau" in message
    assert len(faux.appels) == appels_initiaux
    # La memorisation survit a un redemarrage : le fichier porte l'emprise.
    contenu = json.loads((cache_gares / "geo" / "gares.json").read_text(encoding="utf-8"))
    assert contenu["emprises"] == [list(BOITE)]
    assert [g["nom"] for g in contenu["gares"]] == ["Houilles - Carrières"]
    # Le statut et les lignes sont memorises avec la gare : un fichier livre
    # dans le depot doit suffire a distinguer une gare a venir.
    assert contenu["gares"][0]["statut"] == geo.STATUT_SERVICE
    assert contenu["gares"][0]["lignes"] == ["Train J"]


def test_elargir_le_territoire_ne_retelecharge_que_la_nouvelle_emprise(cache_gares, monkeypatch):
    faux = _FauxRequests()
    monkeypatch.setattr(geo, "requests", faux)

    # Ce qui se compte ici est le nombre d'emprises interrogees, non celui des
    # requetes : une source pagine, une autre decoupe l'emprise en tuiles.
    geo.charger_gares(BOITE)
    apres_premiere = len(faux.appels)
    assert apres_premiere > 0

    # Ajout de communes : l'emprise s'elargit, le reseau est resollicite.
    plus_large = (48.80, 2.10, 48.99, 2.45)
    geo.charger_gares(plus_large)
    apres_seconde = len(faux.appels)
    assert apres_seconde > apres_premiere

    # Les deux emprises sont desormais couvertes : plus rien ne part.
    geo.charger_gares(plus_large)
    geo.charger_gares(BOITE)
    assert len(faux.appels) == apres_seconde


def test_gares_en_double_sont_fusionnees_sans_perdre_leur_reseau():
    """Deux sources livrent la meme gare a quelques dizaines de metres pres.

    La fusion n'en garde qu'un point : la position de la plus « lourde » et
    l'union des reseaux.
    """

    fusionnees = geo._dedoublonner_gares(
        [
            {"nom": "Sartrouville", "latitude": 48.9378, "longitude": 2.1573,
             "reseau": "RER", "type": "station"},
            {"nom": "sartrouville", "latitude": 48.9373, "longitude": 2.1575,
             "reseau": "Transilien", "type": "station"},
            # Meme nom, mais a cinq kilometres : deux gares distinctes.
            {"nom": "Sartrouville", "latitude": 48.9800, "longitude": 2.1573,
             "reseau": "", "type": "station"},
        ]
    )

    assert len(fusionnees) == 2
    principale = next(g for g in fusionnees if abs(g["latitude"] - 48.9378) < 1e-3)
    assert set(principale["reseau"].split(";")) == {"RER", "Transilien"}


def _gare(nom, lat, lon, lignes, statut=geo.STATUT_SERVICE, reseau="Métro", **extra):
    return {"nom": nom, "latitude": lat, "longitude": lon, "reseau": reseau,
            "type": "subway", "statut": statut, "lignes": lignes, **extra}


def test_une_gare_a_venir_deja_desservie_par_sa_ligne_n_est_plus_a_venir():
    """Le jeu des projets garde des mois une gare ouverte « en travaux ».

    Nanterre - La Folie (RER E) y figurait encore en 2026, deux ans apres son
    ouverture ; Quatre Routes (tramway 1) sous un autre nom que dans le jeu
    des gares en service. C'est la gare en service de la meme ligne, au meme
    endroit, qui fait foi.
    """

    gares = geo._dedoublonner_gares([
        _gare("Nanterre-La-Folie", 48.8976, 2.2237, ["RER E"], reseau="RER"),
        # Meme ligne, trois metres plus loin, autre nom : c'est le meme arret.
        _gare("Asnières Quatre Routes", 48.92725, 2.27440, ["Tram 1"], geo.STATUT_TRAVAUX, "Tramway"),
        _gare("Quatre Routes", 48.92725, 2.27437, ["Tram 1"], reseau="Tramway"),
        # Deux lignes annoncees, une seule ouverte : la gare reste a venir pour l'autre.
        _gare("Nanterre - La Folie", 48.8977, 2.2238, ["RER E", "Métro 15"], geo.STATUT_TRAVAUX),
        # Meme ligne a 270 m, autre nom : un arret distinct, encore a venir.
        _gare("Paul Bert", 48.9002, 2.5673, ["Tram 4"], geo.STATUT_TRAVAUX, "Tramway"),
        _gare("Hôpital Intercommunal", 48.9002, 2.5710, ["Tram 4"], reseau="Tramway"),
    ])
    par_nom = {g["nom"]: g for g in gares}

    assert "Asnières Quatre Routes" not in par_nom
    folie = par_nom["Nanterre-La-Folie"]
    assert (folie["statut"], folie["lignes"], folie["lignes_a_venir"]) == (
        geo.STATUT_SERVICE, ["RER E"], ["Métro 15"])
    assert par_nom["Paul Bert"]["statut"] == geo.STATUT_TRAVAUX
    # Un pole ne prend pas le dessin d'une ligne qui n'y roule pas encore.
    assert folie["reseau"] == "RER"
    # Idempotent : relu, le fichier ne change plus.
    assert geo._dedoublonner_gares(gares) == gares


def test_la_ligne_a_venir_d_un_pole_disparait_a_son_ouverture():
    """Le jour ou la source en service cite la ligne 15 aux Ardoines, elle
    n'y est plus annoncee : elle y roule."""

    avant = geo._dedoublonner_gares([
        _gare("Les Ardoines", 48.7822, 2.4096, ["RER C"], reseau="RER"),
        _gare("Les Ardoines", 48.7826, 2.4100, ["Métro 15"], geo.STATUT_TRAVAUX),
    ])
    assert avant[0]["lignes_a_venir"] == ["Métro 15"]

    apres = geo._dedoublonner_gares(avant + [
        # La station de metro, sous un nom un peu different, a 60 m.
        _gare("Les Ardoines Métro", 48.7827, 2.4102, ["Métro 15"]),
    ])
    ardoines = next(g for g in apres if g["nom"] == "Les Ardoines")
    assert ardoines["lignes_a_venir"] == []
    assert not [g for g in apres if g["statut"] != geo.STATUT_SERVICE]


def _fichier_gares(dossier: Path, gares: list[dict]) -> None:
    (dossier / "geo" / "gares.json").write_text(
        json.dumps({"emprises": [list(BOITE)], "gares": gares}), encoding="utf-8"
    )


def test_l_actualisation_bascule_en_service_les_gares_ouvertes(cache_gares, monkeypatch):
    """Le fichier versionne n'est jamais retelecharge par `charger_gares` : sans
    actualisation, une gare ouverte resterait « a venir » pour toujours."""

    _fichier_gares(cache_gares, [
        _gare("Houilles - Carrières", 48.9245, 2.1905, ["Train J"], reseau="Transilien"),
        _gare("Vitry Centre", 48.93, 2.20, ["Métro 15"], geo.STATUT_TRAVAUX),
    ])
    faux = _FauxRequests(gares_idfm=[
        _enregistrement_idfm("Houilles - Carrières", "TRAIN J", "TRAIN"),
        {"geometry": {"type": "Point", "coordinates": [2.2001, 48.9301]},
         "fields": {"nom_zdc": "Vitry Centre", "res_com": "METRO 15", "mode": "METRO"}},
    ])
    monkeypatch.setattr(geo, "requests", faux)

    rapport = geo.actualiser_gares()

    assert rapport.ecrit
    assert rapport.ouvertes == ["Vitry Centre — Métro 15"]
    gares, _ = geo._lire_fichier_gares()
    assert {g["nom"]: g["statut"] for g in gares} == {
        "Houilles - Carrières": geo.STATUT_SERVICE, "Vitry Centre": geo.STATUT_SERVICE}
    # Relancee sans rien de neuf, elle n'ecrit rien.
    assert not geo.actualiser_gares().ecrit


def test_l_actualisation_ignore_une_reponse_tronquee(cache_gares, monkeypatch):
    """Un reseau a trous serait pire qu'un reseau vieux de quelques mois."""

    connues = [_gare(f"Station {i}", 48.92, 2.16 + i / 1000, [f"Métro {i}"]) for i in range(10)]
    _fichier_gares(cache_gares, connues)
    monkeypatch.setattr(geo, "requests", _FauxRequests())  # une seule gare en service

    rapport = geo.actualiser_gares()

    assert not rapport.ecrit
    assert "tronquee" in " ".join(rapport.messages)
    assert len(geo._lire_fichier_gares()[0]) == 10


def test_l_actualisation_ne_change_pas_de_source(cache_gares, monkeypatch):
    """Si la source de reference echoue, rien n'est remplace : OpenStreetMap
    renommerait la moitie des gares sans qu'aucune n'ait change."""

    _fichier_gares(cache_gares, [_gare("Houilles - Carrières", 48.9245, 2.1905, ["Train J"])])
    faux = _FauxRequests(idfm_code=503)
    monkeypatch.setattr(geo, "requests", faux)

    rapport = geo.actualiser_gares()

    assert not rapport.ecrit and "503" in " ".join(rapport.messages)
    assert not any("overpass" in url for url in faux.appels)


def test_rang_de_gare_distingue_le_metro_du_train():
    """Le rang pilote la taille du point : ce sont les stations de metro qui
    saturaient la vue d'ensemble, pas les gares."""

    lourde = {"nom": "Sartrouville", "reseau": "RER;Transilien", "type": "station"}
    legere = {"nom": "Abbesses", "reseau": "", "type": "subway"}
    tram = {"nom": "Porte de Versailles", "reseau": "Tramway d'Île de France", "type": "station"}
    # Un pole desservi par le metro *et* le RER se voit de loin : c'est le RER
    # qui decide.
    mixte = {"nom": "Châtelet", "reseau": "Métro de Paris;RER", "type": "subway"}
    # `railway=station` sans reseau renseigne : le cas le plus frequent dans
    # OpenStreetMap, et c'est une gare — le metro sort avec `station=subway`.
    nue = {"nom": "Rueil-Malmaison", "reseau": "", "type": "station"}

    assert geo.rang_gare(lourde) == geo.RANG_LOURD
    assert geo.rang_gare(mixte) == geo.RANG_LOURD
    assert geo.rang_gare(nue) == geo.RANG_LOURD
    # `railway=tram_stop` : l'arret de tramway tel qu'OpenStreetMap le nomme,
    # sans tag `station` pour le preciser.
    arret_tram = {"nom": "Musée MAC VAL", "reseau": "", "type": "tram_stop"}

    assert geo.rang_gare(legere) == geo.RANG_LEGER
    assert geo.rang_gare(tram) == geo.RANG_LEGER
    assert geo.rang_gare(arret_tram) == geo.RANG_LEGER


def test_le_genre_de_gare_choisit_le_dessin_et_non_la_taille():
    """Le rang dit de combien de loin on veut voir l'arret, le genre ce qu'on y prend.

    Les deux ne se recouvrent pas : un pole desservi par le RER **et** le
    metro est lourd — on veut le reperer de loin — et c'est bien un train
    qu'on y prend en premier. Un arret de tramway, lui, est leger mais porte
    son propre dessin.
    """

    train = {"nom": "Sartrouville", "reseau": "RER;Transilien", "type": "station"}
    metro = {"nom": "Abbesses", "reseau": "", "type": "subway"}
    tram = {"nom": "Porte de Versailles", "reseau": "Tramway d'Île de France", "type": "station"}
    arret_tram = {"nom": "Musée MAC VAL", "reseau": "", "type": "tram_stop"}
    mixte = {"nom": "Châtelet", "reseau": "Métro de Paris;RER", "type": "subway"}
    nue = {"nom": "Rueil-Malmaison", "reseau": "", "type": "station"}

    assert geo.genre_gare(train) == geo.GENRE_TRAIN
    assert geo.genre_gare(mixte) == geo.GENRE_TRAIN
    # Sans rien pour la qualifier, une gare est une gare.
    assert geo.genre_gare(nue) == geo.GENRE_TRAIN
    assert geo.genre_gare(metro) == geo.GENRE_METRO
    assert geo.genre_gare(tram) == geo.GENRE_TRAM
    assert geo.genre_gare(arret_tram) == geo.GENRE_TRAM
    # Un arret desservi par le metro et le tram se dessine « M » : c'est la
    # ligne qui porte le plus loin, comme sur un panneau de correspondances.
    assert geo.genre_gare({"reseau": "Métro;Tram", "type": "subway"}) == geo.GENRE_METRO
    # Et le rang, lui, ne s'en trouve pas change.
    assert geo.rang_gare(tram) == geo.RANG_LEGER


def test_le_fichier_de_gares_du_depot_ne_contient_aucun_doublon():
    """Garde-fou sur le fichier versionne, pas seulement sur l'algorithme."""

    gares, _ = geo._lire_fichier_gares()
    if not gares:
        pytest.skip("aucun fichier de gares dans le depot")
    assert geo._dedoublonner_gares(gares) == gares


#: Gares de petite couronne attendues dans le fichier livre. Elles sont nommees
#: une a une parce qu'un simple comptage ne dirait rien d'un trou geographique.
GARES_ATTENDUES = (
    ("Bourg-la-Reine", "RER B"),
    ("Laplace", "RER B"),
    ("Robespierre", "Métro 9"),
    ("Croix de Chavaux", "Métro 9"),
    ("Mairie de Montreuil", "Métro 9"),
    ("Gallieni", "Métro 3"),
)


def test_le_fichier_de_gares_du_depot_couvre_la_petite_couronne():
    """Le reseau livre doit desservir la banlieue, pas seulement Paris."""

    gares, _ = geo._lire_fichier_gares()
    if not gares:
        pytest.skip("aucun fichier de gares dans le depot")

    par_nom = {g["nom"]: g for g in gares}
    for nom, ligne in GARES_ATTENDUES:
        assert nom in par_nom, f"{nom} absente du fichier de gares livre"
        assert ligne in par_nom[nom]["lignes"], f"{nom} : ligne {ligne} non renseignee"

    # Le semis doit rester dense au-dela du periphérique : un reseau tronque
    # serait dense dans Paris et vide autour.
    hors_paris = [g for g in gares if not (48.81 < g["latitude"] < 48.91 and 2.25 < g["longitude"] < 2.42)]
    assert len(hors_paris) > len(gares) / 2


def test_le_fichier_de_gares_du_depot_porte_le_grand_paris_express():
    """Les gares a venir sont livrees avec le reste, statut compris."""

    gares, _ = geo._lire_fichier_gares()
    if not gares:
        pytest.skip("aucun fichier de gares dans le depot")

    a_venir = [g for g in gares if g["statut"] != geo.STATUT_SERVICE]
    assert {g["statut"] for g in a_venir} <= {geo.STATUT_TRAVAUX, geo.STATUT_PROJET}

    ligne_15 = [g for g in gares if "Métro 15" in g["lignes"] + g["lignes_a_venir"]]
    # Trente-six des quarante et une gares de la ligne tiennent dans l'emprise
    # du Grand Paris ; on en exige largement plus que la poignee qu'un fichier
    # tronque laisserait passer.
    assert len(ligne_15) > 30
    # Certaines sont des sites neufs, d'autres des poles deja desservis : les
    # deux cas doivent etre representes, sinon la fusion est passee a cote.
    assert any(g["statut"] == geo.STATUT_TRAVAUX for g in ligne_15)
    assert any(g["statut"] == geo.STATUT_SERVICE for g in ligne_15)


def test_conversion_numerique_tolere_une_cellule_illisible():
    """Une cellule ni numerique ni decimale-virgule ne doit pas tout arreter.

    Le repli « virgule decimale » rend un `Float64` nullable, qui doit pouvoir
    s'ecrire dans une colonne `float64` meme quand une valeur manque — ce qui
    arrive dans les DVF reelles.
    """

    from src.ingestion import _vers_numerique

    assert list(_vers_numerique(pd.Series(["-", "12,5"]))) == [pytest.approx(float("nan"), nan_ok=True), 12.5]
    assert pd.Series(_vers_numerique(pd.Series(["abc", "3"]))).tolist()[1] == 3.0
    assert _vers_numerique(pd.Series(["nan", "nan"])).isna().all()


def test_nom_de_gare_tolerant_au_schema():
    assert geo._nom_depuis_champs({"nom_long": "Nanterre-Université"}) == "Nanterre-Université"
    assert geo._nom_depuis_champs({"nom_de_la_gare": "Rueil"}) == "Rueil"
    assert geo._nom_depuis_champs({"code_uic": "87381509"}) == "Gare"


def test_fichier_gares_versionne_prioritaire_sur_le_reseau(tmp_path: Path, monkeypatch):
    """En production, le depot fait foi : aucun appel reseau ne doit partir."""

    dossier = tmp_path / "geo"
    dossier.mkdir()
    (dossier / "gares.json").write_text(
        json.dumps({
            "emprises": [list(BOITE)],
            "gares": [
                {"nom": "Gare de Houilles", "latitude": 48.9245, "longitude": 2.1905},
                {"nom": "Gare de Marseille", "latitude": 43.30, "longitude": 5.38},
                {"nom": "Entrée invalide", "latitude": "?", "longitude": None},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("DVF_GEO_DIR", str(dossier))
    monkeypatch.setenv("DVF_CACHE_DIR", str(tmp_path / "cache"))
    faux = _FauxRequests()
    monkeypatch.setattr(geo, "requests", faux)

    gares, message = geo.charger_gares(BOITE)

    # Hors emprise et enregistrements invalides ecartes, reseau jamais sollicite.
    assert [g["nom"] for g in gares] == ["Gare de Houilles"]
    assert "gares.json" in message
    assert faux.appels == []
    # Sans statut, une gare est en service ; sans lignes, on ne les connait pas.
    assert gares[0]["statut"] == geo.STATUT_SERVICE
    assert gares[0]["lignes"] == []


def test_voies_dominantes_donnent_un_repere_a_une_section_sans_nom():
    """Le cadastre ne nomme pas ses sections : le repere vient des adresses.

    On verifie les trois choses qui rendent ce libelle lisible : le numero de
    voirie disparait, les voies sont citees de la plus fournie a la moins, et
    la liste s'arrete au nombre demande.
    """

    ventes = pd.DataFrame(
        {
            "code_section": ["S1"] * 7 + ["S2"] * 2,
            "adresse": [
                "12 Rue Des Lilas",
                "14 Rue Des Lilas",
                "3 Bis Rue Des Lilas",
                "7 Avenue Gambetta",
                "9 Avenue Gambetta",
                "1 Place Du Marche",
                "5 B Impasse Verte",
                "2 Quai De Seine",
                None,
            ],
        }
    )
    reperes = voies_dominantes(ventes, "code_section", nb_voies=2)
    assert reperes["S1"] == "Rue Des Lilas, Avenue Gambetta"
    assert reperes["S2"] == "Quai De Seine"

    # Trois voies par defaut, dans l'ordre decroissant des ventes.
    assert voies_dominantes(ventes, "code_section")["S1"].split(", ") == [
        "Rue Des Lilas", "Avenue Gambetta", "Impasse Verte",
    ]
    assert voies_dominantes(pd.DataFrame(), "code_section").empty


def test_point_d_etiquette_tombe_dans_le_plus_grand_morceau():
    """Une commune en deux morceaux s'etiquette sur le principal, pas entre eux."""

    grand = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]
    petit = [[9.0, 9.0], [9.2, 9.0], [9.2, 9.2], [9.0, 9.2], [9.0, 9.0]]
    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "properties": {"code_commune": "99001"},
                "geometry": {"type": "MultiPolygon", "coordinates": [[grand], [petit]]},
            },
            {
                "properties": {"code_commune": "99002"},
                "geometry": {"type": "Polygon", "coordinates": [petit]},
            },
            # Sans code ni geometrie : ignoree plutot que fatale.
            {"properties": {}, "geometry": {"type": "Polygon", "coordinates": [grand]}},
        ],
    }
    points = geo.points_representatifs(collection)

    assert set(points) == {"99001", "99002"}
    lon, lat, aire = points["99001"]
    assert (lon, lat) == pytest.approx((1.0, 1.0))
    assert aire == pytest.approx(4.0)
    # Le poids departage les etiquettes qui se chevauchent.
    assert points["99002"][2] < aire


def test_les_libelles_des_donnees_sont_echappes_avant_d_entrer_dans_la_carte():
    """Un libelle DVF ne doit pas pouvoir injecter de balise dans la page.

    Les infobulles sont composees dans le navigateur, a partir de
    colonnes de texte brut : c'est donc `carte.js` qui porte l'echappement, et
    il doit couvrir **tous** les libelles qui viennent des donnees — adresse,
    type de bien, nom de gare, entete de zone. En oublier un ne casserait
    rien de visible : la faille ne s'ouvrirait que sur la ligne qui contient
    la balise.
    """

    source = (
        Path(__file__).resolve().parents[1] / "site" / "carte" / "carte.js"
    ).read_text(encoding="utf-8")

    # Les libelles issus des tuiles de ventes et des couches.
    for expression in (
        "echapper(tuile.ad_l[tuile.ad[v]]",
        "echapper(natureDe(tuile, v))",
        "echapper(adresse)",
        "echapper(couche.entetes[indice])",
        "echapper(proprietes.nom)",
    ):
        assert expression in source, expression

    # L'echappement lui-meme couvre les cinq caracteres qui comptent.
    assert '/[&<>"\']/g' in source
    for remplacement in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert remplacement in source, remplacement


# --------------------------------------------------------------------------
# Échelle de couleurs : linéaire, absolue, à bornes robustes
# --------------------------------------------------------------------------


@pytest.fixture
def medianes_avec_paris() -> pd.Series:
    """Banlieue (4 000–7 000 €/m²) + quelques sections parisiennes très chères."""

    banlieue = [4000 + 200 * i for i in range(16)]  # 4 000 -> 7 000
    paris = [11_000, 12_500, 13_000, 14_000, 15_500]
    return pd.Series(banlieue + paris, dtype=float)


def test_echelle_bornee_aux_centiles_et_non_aux_extremes(medianes_avec_paris):
    """Une section hors norme ne doit pas etirer le degrade des autres.

    Sans bornes robustes, l'unique section a 15 500 EUR/m2 fixerait le haut de
    l'echelle et ecraserait les seize sections de banlieue dans le vert.
    """

    echelle = EchelleCouleur.depuis(medianes_avec_paris)

    assert echelle.bas > medianes_avec_paris.min()
    assert echelle.haut < medianes_avec_paris.max()
    # Au-dela des bornes, la couleur sature : la position depasse 1 mais
    # l'interpolation la ramene au bout de la palette.
    assert echelle.couleur(medianes_avec_paris.max()) == PALETTE_SEQUENTIELLE[-1]
    assert echelle.couleur(medianes_avec_paris.min()) == PALETTE_SEQUENTIELLE[0]


def test_echelle_absolue_une_couleur_designe_toujours_le_meme_prix():
    """La contrepartie assumee du mode absolu : la stabilite.

    Ajouter Paris a la selection ne doit pas repeindre une section de banlieue
    d'une teinte a l'autre, alors qu'une echelle par rang la deplacerait.
    """

    banlieue = pd.Series([4000.0 + 200 * i for i in range(16)])
    avec_paris = pd.concat([banlieue, pd.Series([12_000.0, 15_000.0, 16_000.0])], ignore_index=True)

    # Les bornes bougent (le 95e centile monte), mais 6 000 EUR/m2 reste du
    # meme cote de l'echelle, et la couleur reste comparable d'une vue a
    # l'autre — ce qu'une echelle par rang ne garantit pas.
    sans = EchelleCouleur.depuis(banlieue)
    avec = EchelleCouleur.depuis(avec_paris)
    assert sans.position(6000.0) > 0.5
    assert avec.position(6000.0) < sans.position(6000.0)


def test_limite_connue_de_l_echelle_absolue(medianes_avec_paris):
    """Trace explicite du compromis retenu.

    Sur un territoire tres heterogene, la mediane ne tombe pas au milieu du
    degrade : les sections bon marche restent dans le bas de la gamme. C'est le
    prix de la comparabilite, et ce test existe pour que le choix reste un
    choix documente plutot qu'une surprise.
    """

    echelle = EchelleCouleur.depuis(medianes_avec_paris)
    assert echelle.position(float(medianes_avec_paris.median())) < 0.4


def test_graduations_de_legende_jalonnent_les_bornes(medianes_avec_paris):
    echelle = EchelleCouleur.depuis(medianes_avec_paris)
    graduations = echelle.graduations(5)

    assert len(graduations) == 5
    assert graduations == sorted(graduations)
    assert graduations[0] == pytest.approx(echelle.bas)
    assert graduations[-1] == pytest.approx(echelle.haut)
    # Regulierement espacees : c'est ce que promet une echelle lineaire.
    ecarts = [b - a for a, b in zip(graduations, graduations[1:])]
    assert all(ecart == pytest.approx(ecarts[0]) for ecart in ecarts)
    # Deux graduations suffisent au minimum, meme si on en demande moins.
    assert len(echelle.graduations(1)) == 2


def test_echelle_cas_limites():
    vide = EchelleCouleur.depuis(pd.Series(dtype=float))
    assert vide.vide is True
    assert vide.couleur(5000.0) == GRIS_DONNEES_INSUFFISANTES

    # Une seule valeur, ou plusieurs valeurs identiques : aucune amplitude a
    # representer, la couleur se cale au milieu plutot que de diviser par zero.
    unique = EchelleCouleur.depuis(pd.Series([5000.0]))
    assert unique.position(5000.0) == pytest.approx(0.5)
    assert EchelleCouleur.depuis(pd.Series([7.0, 7.0, 7.0])).position(7.0) == pytest.approx(0.5)

    # Valeur manquante : gris, jamais une couleur arbitraire.
    echelle = EchelleCouleur.depuis(pd.Series([1.0, 2.0]))
    assert echelle.couleur(None) == GRIS_DONNEES_INSUFFISANTES
    assert echelle.couleur(pd.NA) == GRIS_DONNEES_INSUFFISANTES
    assert echelle.couleur(float("nan")) == GRIS_DONNEES_INSUFFISANTES


def test_repartition_de_la_palette_privilegie_le_jaune_et_l_orange():
    """Le vert ne doit pas monopoliser le dégradé : c'est entre vert et rouge
    que se situe la majorité des sections, donc là qu'il faut de la nuance."""

    import colorsys

    from src.stats import PALETTE_SEQUENTIELLE, _interpoler

    def famille(hexa: str) -> str:
        r, v, b = (int(hexa[i : i + 2], 16) / 255 for i in (1, 3, 5))
        teinte, _, saturation = colorsys.rgb_to_hls(r, v, b)
        degres = teinte * 360
        if saturation < 0.12:
            return "gris"
        if 90 <= degres <= 200:
            return "vert"
        if 61 <= degres < 90:
            return "vert-jaune"
        if 45 <= degres <= 61:
            return "jaune"
        if 20 <= degres < 45:
            return "orange"
        return "rouge"

    parts: dict[str, int] = {}
    for centieme in range(101):
        cle = famille(_interpoler(PALETTE_SEQUENTIELLE, centieme / 100))
        parts[cle] = parts.get(cle, 0) + 1

    assert parts.get("vert", 0) <= 32
    assert parts.get("jaune", 0) + parts.get("orange", 0) >= 35
    # Les extrémités restent celles voulues.
    assert _interpoler(PALETTE_SEQUENTIELLE, 0.0) == "#00695c"
    assert _interpoler(PALETTE_SEQUENTIELLE, 1.0) == "#a50026"


def test_gares_restreintes_aux_sections_chargees(tmp_path: Path):
    """L'emprise rectangulaire déborde largement des sections analysées."""

    carre = {
        "type": "Feature",
        "properties": {"id": "78311000AB", "commune": "78311", "prefixe": "000", "code": "AB"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[2.10, 48.90], [2.20, 48.90], [2.20, 48.95], [2.10, 48.95], [2.10, 48.90]]],
        },
    }
    geojson = {"type": "FeatureCollection", "features": [carre]}

    gares = [
        {"nom": "Dedans", "latitude": 48.92, "longitude": 2.15},
        {"nom": "Dans l'emprise mais hors section", "latitude": 48.97, "longitude": 2.25},
        {"nom": "Sur un autre continent", "latitude": 43.30, "longitude": 5.40},
        {"nom": "Coordonnées illisibles", "latitude": "?", "longitude": None},
    ]

    retenues = geo.gares_dans_sections(gares, geojson)

    assert [g["nom"] for g in retenues] == ["Dedans"]
    # Sans contour, rien ne peut être situé.
    assert geo.gares_dans_sections(gares, {"type": "FeatureCollection", "features": []}) == []


def test_gares_dans_sections_gere_trous_et_multipolygones():
    """Un trou dans une section exclut ce qu'il contient."""

    troue = {
        "type": "Feature",
        "properties": {"id": "78311000AB"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]],
                [[4.0, 4.0], [6.0, 4.0], [6.0, 6.0], [4.0, 6.0], [4.0, 4.0]],
            ],
        },
    }
    multiple = {
        "type": "Feature",
        "properties": {"id": "78311000AC"},
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                [[[20.0, 0.0], [22.0, 0.0], [22.0, 2.0], [20.0, 2.0], [20.0, 0.0]]],
                [[[30.0, 0.0], [32.0, 0.0], [32.0, 2.0], [30.0, 2.0], [30.0, 0.0]]],
            ],
        },
    }
    geojson = {"type": "FeatureCollection", "features": [troue, multiple]}

    points = [
        {"nom": "pleine matière", "latitude": 2.0, "longitude": 2.0},
        {"nom": "dans le trou", "latitude": 5.0, "longitude": 5.0},
        {"nom": "premier polygone", "latitude": 1.0, "longitude": 21.0},
        {"nom": "second polygone", "latitude": 1.0, "longitude": 31.0},
        {"nom": "entre les deux", "latitude": 1.0, "longitude": 26.0},
    ]

    assert [g["nom"] for g in geo.gares_dans_sections(points, geojson)] == [
        "pleine matière",
        "premier polygone",
        "second polygone",
    ]


# --------------------------------------------------------------------------
# Grisage des sections à faible volume
# --------------------------------------------------------------------------


@pytest.fixture
def stats_volumes_contrastes() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code_section": ["A", "B", "C", "D"],
            "nb_transactions": [50, 3, 1, 40],
            "prix_m2_median": [4000.0, 15_000.0, 2000.0, 7000.0],
        }
    )


def test_sections_peu_fournies_sont_grisees(stats_volumes_contrastes):
    couleurs, _, grisees = couleurs_sections(
        stats_volumes_contrastes, "prix_m2_median", seuil_volume=3
    )

    assert grisees == {"B", "C"}
    assert couleurs["B"] == GRIS_DONNEES_INSUFFISANTES
    assert couleurs["C"] == GRIS_DONNEES_INSUFFISANTES
    assert couleurs["A"] != GRIS_DONNEES_INSUFFISANTES
    assert couleurs["D"] != GRIS_DONNEES_INSUFFISANTES


def test_sections_grisees_n_etirent_plus_l_echelle(stats_volumes_contrastes):
    """Une vente hors norme dans une section à 1 transaction ne doit pas
    repeindre toutes les autres."""

    _, sans_seuil, _ = couleurs_sections(
        stats_volumes_contrastes, "prix_m2_median", seuil_volume=0
    )
    _, avec_seuil, _ = couleurs_sections(
        stats_volumes_contrastes, "prix_m2_median", seuil_volume=3
    )

    # Sans seuil, les bornes sont tirées par les sections à 1 et 3 ventes.
    assert sans_seuil.bas < avec_seuil.bas
    assert avec_seuil.haut < sans_seuil.haut
    # Les deux sections fiables occupent alors les extrémités du dégradé.
    assert avec_seuil.position(4000.0) < 0.05
    assert avec_seuil.position(7000.0) > 0.95


def test_seuil_zero_ne_grise_rien(stats_volumes_contrastes):
    couleurs, _, grisees = couleurs_sections(
        stats_volumes_contrastes, "prix_m2_median", seuil_volume=0
    )
    assert grisees == set()
    assert GRIS_DONNEES_INSUFFISANTES not in couleurs.values()


def test_toutes_les_sections_sous_le_seuil(stats_volumes_contrastes):
    """Tout est grisé, mais la légende garde des bornes exploitables."""

    couleurs, echelle, grisees = couleurs_sections(
        stats_volumes_contrastes, "prix_m2_median", seuil_volume=100
    )

    assert grisees == {"A", "B", "C", "D"}
    assert set(couleurs.values()) == {GRIS_DONNEES_INSUFFISANTES}
    assert echelle.haut > echelle.bas  # calée sur l'ensemble, pas vide


def test_couleurs_sections_sans_donnee():
    vide = pd.DataFrame(columns=["code_section", "nb_transactions", "prix_m2_median"])
    couleurs, echelle, grisees = couleurs_sections(vide, "prix_m2_median")

    assert couleurs == {}
    assert grisees == set()
    assert echelle.vide
