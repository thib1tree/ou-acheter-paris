"""Les chiffres du navigateur sont ceux de Python.

Le site statique recalcule les statistiques dans le navigateur
(`site/calcul.js`). Ce test produit un jeu de reference avec le code Python
qui les calculait jusqu'ici (`src/charge.py`, `src/stats.py`), fait tourner
le JavaScript sous Node sur le meme fichier binaire (`src/binaire.py`), et
compare tout : codes, titres, couleurs, legendes, resumes, et chaque chiffre
de chaque infobulle.

Les chiffres doivent etre egaux « a l'arrondi pres » : un nombre entier (prix
arrondi a l'euro, effectif) doit etre identique ; un taux ou un R² ne peut
differer que dans ses derniers bits (le logarithme de Node et celui de numpy
ne sont pas tenus d'arrondir pareil au dernier chiffre binaire). Les couleurs
et les textes, eux, doivent etre identiques au caractere pres.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

from src import binaire, charge  # noqa: E402
from src.ingestion import construire_transactions_territoire, territoire_prepare  # noqa: E402
from src.stats import CLE_COMMUNE, CLE_SECTION, Filtres  # noqa: E402

METRIQUES = [cle for cle, _ in charge.METRIQUES_PROPOSEES]

if shutil.which("node") is None:  # pragma: no cover - depend de l'environnement
    pytest.skip("Node absent", allow_module_level=True)


def _filtres_js(filtres: Filtres) -> dict:
    return {
        "annees": list(filtres.annees) if filtres.annees else None,
        "surface": list(filtres.surface) if filtres.surface else None,
        "ouvert": bool(filtres.surface_max_ouvert),
        "types": list(filtres.types_bien) if filtres.types_bien else None,
        "etats": list(filtres.etats) if filtres.etats else None,
    }


def _executer_node(tmp_path: Path, transactions: pd.DataFrame, scenarios, reperes) -> list:
    libelles = charge.libelles_zones(transactions)
    contenu, meta = binaire.encoder_ventes(transactions, libelles)
    (tmp_path / "ventes.bin").write_bytes(contenu)
    (tmp_path / "ventes.json").write_text(json.dumps(meta, ensure_ascii=False))
    (tmp_path / "regles.json").write_text(json.dumps(charge.regles_calcul(), ensure_ascii=False))
    (tmp_path / "reperes.json").write_text(json.dumps(reperes, ensure_ascii=False))
    (tmp_path / "scenarios.json").write_text(json.dumps(
        [{"filtres": _filtres_js(f), "metriques": metriques} for f, metriques in scenarios]
    ))
    subprocess.run(
        ["node", str(RACINE / "tests" / "parite.js"), str(tmp_path)],
        check=True, capture_output=True, text=True, timeout=600,
    )
    return json.loads((tmp_path / "resultat.json").read_text())


def _egaux(python, javascript, ou: str, ecarts: list[str]) -> None:
    """Compare deux arbres JSON ; les nombres a l'arrondi pres."""

    if isinstance(python, dict):
        assert isinstance(javascript, dict), ou
        if set(python) != set(javascript):
            ecarts.append(f"{ou} : cles {sorted(python)} != {sorted(javascript)}")
            return
        for cle in python:
            _egaux(python[cle], javascript[cle], f"{ou}.{cle}", ecarts)
    elif isinstance(python, (list, tuple)):
        if not isinstance(javascript, list) or len(python) != len(javascript):
            ecarts.append(f"{ou} : longueurs {len(python)} != {len(javascript or [])}")
            return
        for i, (a, b) in enumerate(zip(python, javascript)):
            _egaux(a, b, f"{ou}[{i}]", ecarts)
            if len(ecarts) > 20:
                return
    elif isinstance(python, bool) or python is None or isinstance(python, str):
        if python != javascript:
            ecarts.append(f"{ou} : {python!r} != {javascript!r}")
    elif isinstance(python, (int, float)):
        if javascript is None or not np.isclose(python, javascript, rtol=1e-9, atol=1e-9):
            ecarts.append(f"{ou} : {python!r} != {javascript!r}")
        elif float(python).is_integer() != float(javascript).is_integer():
            ecarts.append(f"{ou} : arrondi {python!r} != {javascript!r}")
    else:  # pragma: no cover
        raise TypeError(type(python))


def _comparer(transactions, scenarios, reperes, tmp_path) -> None:
    libelles = charge.libelles_zones(transactions)
    obtenus = _executer_node(tmp_path, transactions, scenarios, reperes)
    ecarts: list[str] = []
    for (filtres, metriques), parMetrique in zip(scenarios, obtenus):
        for metrique in metriques:
            couches, echelle, nb = charge.calculer_couches(
                transactions, filtres, metrique, (CLE_SECTION, CLE_COMMUNE), reperes, libelles
            )
            attendu = json.loads(json.dumps(
                {"couches": couches, "echelle_points": echelle, "nb": nb}
            ))
            _egaux(attendu, parMetrique[metrique], f"{filtres} / {metrique}", ecarts)
    assert not ecarts, "\n".join(ecarts[:20])


# --------------------------------------------------------------------------
# Donnees reelles
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def transactions():
    if not territoire_prepare("grand-paris"):
        pytest.skip("territoire non livre")
    ventes, _ = construire_transactions_territoire("grand-paris")
    return ventes


def test_le_navigateur_retrouve_les_chiffres_de_python(transactions, tmp_path):
    defaut = charge.filtres_par_defaut(transactions)
    annees = defaut.annees
    scenarios = [
        # L'ouverture du site, toutes metriques.
        (defaut, METRIQUES),
        # Une annee decochee, surface bornee des deux cotes.
        (Filtres(annees=annees[1:], surface=(30.0, 80.0), surface_max_ouvert=False,
                 types_bien=defaut.types_bien), METRIQUES),
        # Les maisons seules : beaucoup de zones grisees.
        (Filtres(annees=annees, surface=(0.0, float(charge.plafond_surface(transactions))),
                 surface_max_ouvert=True, types_bien=["Maison"]), METRIQUES),
        # Deux annees : l'evolution annuelle n'est calculable nulle part.
        (Filtres(annees=annees[-2:], surface=(100.0, 120.0), surface_max_ouvert=False,
                 types_bien=["Appartement"]), ["prix_m2_median", "rendement"]),
        # Le neuf seul (VEFA), puis l'ancien seul.
        (Filtres(annees=annees, surface=defaut.surface, surface_max_ouvert=True,
                 types_bien=defaut.types_bien, etats=["Neuf (VEFA)"]), METRIQUES),
        (Filtres(annees=annees[1:], surface=(20.0, 90.0), surface_max_ouvert=False,
                 types_bien=["Appartement"], etats=["Ancien"]), ["prix_m2_median", "rendement"]),
    ]
    _comparer(transactions, scenarios, charge.reperes_voies(transactions), tmp_path)


# --------------------------------------------------------------------------
# Cas limites, sur un jeu synthetique
# --------------------------------------------------------------------------


def _synthetique() -> pd.DataFrame:
    """Quelques centaines de ventes qui visent les cas delicats du portage.

    Medianes a mi-chemin (effectifs pairs), moyennes en ,5 exact a arrondir
    au pair, centimes, surfaces decimales, une section a cheval sur deux
    communes, une section a une seule vente, des annees creuses.
    """

    generateur = np.random.default_rng(7)
    lignes = []
    communes = {"92012": "Boulogne-Billancourt", "92040": "Issy-les-Moulineaux"}
    sections = ["92012000AB", "92012000AC", "92040000AD", "92040000AE"]
    for i in range(420):
        section = sections[i % 4]
        commune = section[:5] if i % 37 else "92040"  # quelques ventes a cheval
        surface = float(generateur.choice([20, 35.5, 48.25, 60, 75, 90.5, 120]))
        valeur = float(generateur.integers(80, 900)) * 1000 + float(generateur.choice([0, 0.5, 0.25, 12.34]))
        lignes.append({
            "id_mutation": f"m{i}",
            "date_mutation": pd.Timestamp(2020 + (i % 6), 1 + i % 12, 1),
            "annee": 2020 + (i % 6) if i % 11 else 2022,
            "code_commune": commune,
            "nom_commune": communes[commune],
            "code_section": section,
            "section_courte": section[-2:],
            "type_bien": ["Appartement", "Maison", "Mixte"][i % 3 if i % 5 else 0],
            "etat": "Neuf (VEFA)" if i % 7 == 0 else "Ancien",
            "adresse": f"{i % 9} rue {['de Paris', 'Gambetta', 'Victor Hugo'][i % 3]}",
            "valeur_fonciere": valeur,
            "surface_bati": surface,
        })
    lignes.append({**lignes[0], "id_mutation": "seule", "code_section": "92040000ZZ",
                   "section_courte": "ZZ", "code_commune": "92040", "nom_commune": communes["92040"]})
    ventes = pd.DataFrame(lignes)
    ventes["annee"] = ventes["annee"].astype("Int64")
    ventes["prix_m2"] = (ventes["valeur_fonciere"] / ventes["surface_bati"]).round(0)
    return ventes


def test_les_cas_limites_sont_identiques(tmp_path):
    ventes = _synthetique()
    tout = Filtres(annees=None, surface=None, types_bien=None)
    scenarios = [
        (tout, METRIQUES),
        (Filtres(annees=(2021, 2023, 2025), surface=(30.0, 90.0), surface_max_ouvert=False,
                 types_bien=["Appartement", "Mixte"]), METRIQUES),
        (Filtres(annees=(2024,), surface=(48.25, 48.25), surface_max_ouvert=False,
                 types_bien=["Maison"]), METRIQUES),
        (Filtres(annees=None, surface=None, types_bien=None, etats=["Neuf (VEFA)"]), METRIQUES),
        (Filtres(annees=(2022, 2023), surface=(30.0, 90.0), surface_max_ouvert=False,
                 types_bien=["Appartement"], etats=["Ancien"]), METRIQUES),
        # Aucune vente ne passe : cartes vides, legendes lisibles.
        (Filtres(annees=(2021,), surface=(5000.0, 6000.0), surface_max_ouvert=False,
                 types_bien=None), METRIQUES),
    ]
    _comparer(ventes, scenarios, charge.reperes_voies(ventes), tmp_path)


def test_une_valeur_qui_ne_tient_pas_dans_le_format_fait_echouer_l_ecriture():
    """Mieux vaut un site qui ne se construit pas qu'un prix tronque."""

    ventes = _synthetique()
    ventes.loc[0, "valeur_fonciere"] = 123456.789
    ventes["prix_m2"] = (ventes["valeur_fonciere"] / ventes["surface_bati"]).round(0)
    with pytest.raises(binaire.FormatImpossible):
        binaire.encoder_ventes(ventes, {})


def test_les_colonnes_sont_alignees_pour_une_lecture_sans_copie():
    _, meta = binaire.encoder_ventes(_synthetique(), {})
    largeurs = {"u1": 1, "u2": 2, "u4": 4}
    for colonne in meta["colonnes"]:
        assert colonne["debut"] % largeurs[colonne["type"]] == 0, colonne
