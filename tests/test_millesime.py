"""La mise a jour semestrielle des donnees : detection, fenetre d'annees, resume.

Tout se joue hors ligne, sur des reponses de l'API de data.gouv.fr au format
releve le 27/10/2026 (livraison d'octobre 2026 : un fichier par annee).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
sys.path.insert(0, str(RACINE / "scripts"))

import resumer_mise_a_jour  # noqa: E402
import verifier_millesime  # noqa: E402

from src import millesime  # noqa: E402

STATIQUE = "https://static.data.gouv.fr/resources/demandes-de-valeurs-foncieres-geolocalisees"


def _ressource(nom: str, horodatage="20261020-114721", taille=116267867, sha1="a6e76cd8", titre=None):
    return {
        "title": titre or f"DVF {nom}",
        "type": "main",
        "format": "csv.gz",
        "url": f"{STATIQUE}/{horodatage}/{nom}",
        "filesize": taille,
        "checksum": {"type": "sha1", "value": sha1},
        "last_modified": "2026-10-20T11:47:28.564000+00:00",
    }


def _api(periodes, couverture="2025-12-31", **options):
    """Le jeu tel que le decrit l'API : un fichier par periode, et les autres ressources."""

    ressources = [_ressource(f"full-{p}.csv.gz", **options) for p in periodes]
    ressources += [
        # Le fichier unique sur cinq ans, la documentation, l'ancienne arborescence.
        _ressource("dvf.csv.gz", taille=609458453),
        {"title": "Documentation", "type": "documentation", "url": "https://www.data.gouv.fr/fr/datasets/x/"},
        {"title": "[ARCHIVE] Données au format CSV", "type": "main", "format": "url",
         "url": "https://files.data.gouv.fr/geo-dvf/latest/csv/"},
    ]
    reponse = {"temporal_coverage": {"start": "2020-07-01", "end": couverture}, "resources": ressources}
    return lambda adresse: reponse


OCTOBRE_2026 = ["2021-s2", "2022", "2023", "2024", "2025", "2026-s1"]
AVRIL_2026 = ["2021", "2022", "2023", "2024", "2025"]


@pytest.mark.parametrize("adresse, cle, debut, fin", [
    (f"{STATIQUE}/x/full-2025.csv.gz", "2025", dt.date(2025, 1, 1), dt.date(2025, 12, 31)),
    (f"{STATIQUE}/x/full-2026-s1.csv.gz", "2026-s1", dt.date(2026, 1, 1), dt.date(2026, 6, 30)),
    (f"{STATIQUE}/x/full-2021-s2.csv.gz", "2021-s2", dt.date(2021, 7, 1), dt.date(2021, 12, 31)),
])
def test_le_nom_du_fichier_dit_ce_qu_il_couvre(adresse, cle, debut, fin):
    periode = millesime.Periode.depuis_adresse(adresse)
    assert (periode.cle, periode.debut, periode.fin) == (cle, debut, fin)
    assert millesime.Periode.depuis_cle(cle) == periode


@pytest.mark.parametrize("adresse", [
    f"{STATIQUE}/x/dvf.csv.gz", "https://files.data.gouv.fr/geo-dvf/latest/csv/", "",
])
def test_les_autres_ressources_ne_sont_pas_des_periodes(adresse):
    assert millesime.Periode.depuis_adresse(adresse) is None


def _periodes(cles):
    return [millesime.Periode.depuis_cle(c) for c in cles]


@pytest.mark.parametrize("publiees, fin, attendues", [
    # Livraison d'avril : cinq annees civiles.
    (AVRIL_2026, "2025-12-31", AVRIL_2026),
    # Livraison d'octobre : cinq ans glissants, de juillet a juin.
    (OCTOBRE_2026, "2026-06-30", OCTOBRE_2026),
    # Avril suivant : 2021 sort, 2026 devient entiere.
    (["2022", "2023", "2024", "2025", "2026"], "2026-12-31", ["2022", "2023", "2024", "2025", "2026"]),
    # Une annee trop ancienne, encore publiee, sort de la fenetre.
    (["2020", *AVRIL_2026], "2025-12-31", AVRIL_2026),
    # Une meme annee publiee deux fois, le temps d'une transition : la plus large.
    (["2025", "2026-s1", "2026"], "2026-12-31", ["2025", "2026"]),
])
def test_la_fenetre_glisse_d_elle_meme(publiees, fin, attendues):
    retenues = millesime.fenetre(_periodes(publiees), dt.date.fromisoformat(fin))
    assert [p.cle for p in retenues] == attendues


def test_l_etat_distant_est_releve_dans_la_liste_des_ressources():
    etat = millesime.interroger(_api(OCTOBRE_2026))
    # La couverture declaree par le jeu (2025-12-31) est en retard sur les
    # fichiers : c'est le plus recent d'entre eux qui fait foi.
    assert etat.millesime == "2026-06"
    assert etat.fin_couverture == "2026-06-30"
    assert etat.annees == [2021, 2022, 2023, 2024, 2025, 2026]
    assert list(etat.fichiers) == OCTOBRE_2026
    assert etat.fichiers["2026-s1"] == {
        "url": f"{STATIQUE}/20261020-114721/full-2026-s1.csv.gz",
        "taille": 116267867, "sha1": "a6e76cd8", "date": "2026-10-20T11:47:28.564000+00:00",
    }
    assert millesime.fichiers_des_annees(etat, [2021, 2026]).keys() == {"2021-s2", "2026-s1"}


def test_un_jeu_sans_fichier_annuel_arrete_tout():
    with pytest.raises(millesime.MillesimeIllisible):
        millesime.interroger(_api([]))


def test_rien_de_neuf_ne_declenche_rien_et_une_revision_si():
    etat = millesime.interroger(_api(AVRIL_2026))
    source = millesime.source_versionnee(etat, dt.date(2026, 9, 25))
    assert millesime.differences(etat, source) == []
    assert millesime.differences(etat, None)

    # Meme millesime, fichiers republies : c'est une revision.
    revise = millesime.interroger(_api(AVRIL_2026, horodatage="20260601-000000", sha1="ffff"))
    raisons = millesime.differences(revise, source)
    assert len(raisons) == 1 and "5 fichier(s) revise(s)" in raisons[0]

    # Nouvelle livraison : la fenetre glisse.
    nouveau = millesime.interroger(_api(OCTOBRE_2026))
    raisons = " ; ".join(millesime.differences(nouveau, source))
    assert "2025-12 -> 2026-06" in raisons and "annees" in raisons and "2026-s1" in raisons


def test_une_empreinte_de_l_ancienne_arborescence_est_depassee():
    """`source.json` d'avant octobre 2026 : fichiers par departement."""

    ancienne = {"millesime": "2025-12", "annees": [2021, 2022, 2023, 2024, 2025],
                "fichiers": {"2021/75": {"taille": 2089677, "date": "2026-05-18T12:00:48.000Z"}}}
    raisons = millesime.differences(millesime.interroger(_api(OCTOBRE_2026)), ancienne)
    assert raisons and "millesime 2025-12 -> 2026-06" in raisons[0]


def test_le_script_ecrit_les_sorties_de_github_actions(tmp_path, monkeypatch):
    etat = millesime.interroger(_api(OCTOBRE_2026))
    monkeypatch.setattr(millesime, "interroger", lambda: etat)
    (tmp_path / "grand-paris").mkdir()
    monkeypatch.setattr(verifier_millesime, "fichier_source", lambda cle: tmp_path / cle / "source.json")
    sorties = tmp_path / "sorties.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(sorties))

    assert verifier_millesime.main(["--etat", str(tmp_path / "etat.json")]) == 0
    lues = dict(ligne.split("=", 1) for ligne in sorties.read_text().splitlines())
    assert lues["nouveau"] == "oui" and lues["annees"] == "2021-2026" and lues["millesime"] == "2026-06"

    # Adopte apres une preparation reussie : le mois suivant, rien de neuf.
    assert verifier_millesime.main(["--adopter", str(tmp_path / "etat.json")]) == 0
    source = json.loads((tmp_path / "grand-paris" / "source.json").read_text())
    assert source["millesime"] == "2026-06" and source["annees"] == [2021, 2022, 2023, 2024, 2025, 2026]
    assert source["fichiers"]["2021-s2"]["url"].endswith("/full-2021-s2.csv.gz")
    sorties.write_text("")
    assert verifier_millesime.main([]) == 0
    assert "nouveau=non" in sorties.read_text()


# --------------------------------------------------------------------------
# Description de la pull request
# --------------------------------------------------------------------------


def _ventes(annees, prix, communes=("75101", "92012")):
    lignes = []
    for annee in annees:
        for i, commune in enumerate(communes):
            for k in range(10):
                lignes.append({
                    "annee": annee, "code_commune": commune, "nom_commune": f"Commune {commune}",
                    "code_section": f"{commune}000A{k % 3}", "prix_m2": prix + 100 * i + k,
                    "date_mutation": pd.Timestamp(annee, 1 + k, 1),
                })
    return pd.DataFrame(lignes)


def test_la_description_dit_ce_qui_change():
    anciennes = _ventes([2021, 2022, 2023, 2024, 2025], 5000)
    nouvelles = pd.concat([
        _ventes([2022, 2023, 2024, 2025], 5000),
        _ventes([2026], 5400, communes=("75101", "92012", "93001")),
    ])
    texte = resumer_mise_a_jour.resumer(
        anciennes, nouvelles, {"93001000A1": 4}, "2026-06", "millesime 2025-12 -> 2026-06"
    )
    assert "millésime 2026-06" in texte
    assert "du 01/01/2021 au 01/10/2025 → **du 01/01/2022 au 01/10/2026**" in texte
    assert "| 2021 | 20 | — | retirée |" in texte
    assert "| 2026 | — | 30 | nouvelle |" in texte
    # A periode egale (2022 a 2025), rien n'a bouge : la hausse vient de la nouvelle annee.
    assert "« À période égale »" in texte and "(du 01/01/2022 au 01/10/2025)" in texte
    ligne_75 = next(ligne for ligne in texte.splitlines() if ligne.startswith("| 75 |"))
    assert ligne_75.endswith("| +0,0 % |")
    assert "93001 (Commune 93001)" in texte
    assert "1 section(s) sans contour" in texte and "`93001000A1` (4)" in texte


def test_un_glissement_d_un_semestre_ne_leve_aucun_garde_fou():
    """Livraison d'octobre : 2021 n'est plus couverte que de juillet a decembre."""

    anciennes = _ventes([2021, 2022, 2023, 2024, 2025], 5000)
    nouvelles = pd.concat([_ventes([2021, 2022, 2023, 2024, 2025], 5000), _ventes([2026], 5400)])
    nouvelles = nouvelles[(nouvelles["annee"] > 2021) | (nouvelles["date_mutation"].dt.month >= 7)]
    nouvelles = nouvelles[(nouvelles["annee"] < 2026) | (nouvelles["date_mutation"].dt.month <= 6)]
    assert resumer_mise_a_jour.garde_fous(anciennes, nouvelles, {}) == []

    # Une vraie revision, en revanche, se voit encore.
    revisees = nouvelles.assign(prix_m2=nouvelles["prix_m2"] * 1.2)
    alertes = resumer_mise_a_jour.garde_fous(anciennes, revisees, {})
    assert any("à période égale" in alerte for alerte in alertes)
