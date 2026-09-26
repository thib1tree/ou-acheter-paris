"""La mise a jour semestrielle des donnees : detection, fenetre d'annees, resume.

Tout se joue hors ligne, sur des pages d'index enregistrees au format du
serveur d'Etalab (`files.data.gouv.fr`, releve le 25/09/2026).
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


def _index(titre: str, lignes: list[tuple[str, str, str]]) -> str:
    """Une page d'index telle que la sert files.data.gouv.fr."""

    corps = "".join(
        f'<tr>\n<td><a href="{titre}{nom}">{nom}</a></td>\n<td>{taille}</td>\n<td>{date}</td>\n</tr>\n'
        for nom, taille, date in lignes
    )
    return (
        f'<!DOCTYPE html><html><head><title>{titre}</title></head><body><h1>Index of {titre}</h1>'
        '<hr><table id="list"><thead><tr><th>Filename</th><th>File Size</th><th>Date</th></tr>'
        f'</thead><tbody><tr><td><a href="../">..</a></td></tr>\n{corps}</tbody></table></body></html>'
    )


def _serveur(millesimes, annees, taille=2056139, date="2026-05-18T13:14:14.000Z", couverture="2025-12-31"):
    pages = {
        millesime.URL_GEO_DVF: _index("/geo-dvf/", [(f"{m}/", "", "") for m in millesimes] + [("latest/", "", "")]),
        millesime.URL_LATEST: _index("/geo-dvf/latest/csv/", [(f"{a}/", "", "") for a in annees]),
    }
    for annee in annees:
        pages[f"{millesime.URL_LATEST}{annee}/departements/"] = _index(
            f"/geo-dvf/latest/csv/{annee}/departements/",
            [(f"{d}.csv.gz", str(taille), date) for d in ("75", "92", "971")],
        )

    def lire_json(adresse):
        if couverture is None:
            raise OSError("API indisponible")
        return {"temporal_coverage": {"start": "2020-07-01", "end": couverture}}

    return pages.__getitem__, lire_json


def test_la_page_d_index_se_lit():
    lire, _ = _serveur(["2025-12"], [2021, 2025])
    entrees = millesime.lire_index(lire(f"{millesime.URL_LATEST}2025/departements/"))
    assert entrees[0] == millesime.Entree("75.csv.gz", 2056139, "2026-05-18T13:14:14.000Z")
    dossiers = millesime.lire_index(lire(millesime.URL_GEO_DVF))
    assert [e.nom for e in dossiers] == ["2025-12", "latest"]
    assert dossiers[0].taille is None


@pytest.mark.parametrize("nom, fin", [
    ("2025-12", dt.date(2025, 12, 31)),
    ("2026-06", dt.date(2026, 6, 30)),
    ("2024-02", dt.date(2024, 2, 29)),
    ("2025-04-15", dt.date(2025, 4, 15)),
    ("latest", None),
])
def test_le_nom_du_dossier_dit_la_fin_des_donnees(nom, fin):
    assert millesime.fin_du_dossier(nom) == fin


@pytest.mark.parametrize("publiees, fin, attendues", [
    # Aujourd'hui : cinq annees completes, pas d'annee en cours.
    ([2021, 2022, 2023, 2024, 2025], "2025-12-31", [2021, 2022, 2023, 2024, 2025]),
    # Publication de printemps : l'annee en cours arrive, partielle.
    ([2021, 2022, 2023, 2024, 2025, 2026], "2026-06-30", [2021, 2022, 2023, 2024, 2025, 2026]),
    # Six annees completes publiees : la plus ancienne sort de la fenetre.
    ([2020, 2021, 2022, 2023, 2024, 2025, 2026], "2026-06-30", [2021, 2022, 2023, 2024, 2025, 2026]),
    ([2021, 2022, 2023, 2024, 2025, 2026], "2026-12-31", [2022, 2023, 2024, 2025, 2026]),
    # Un dossier d'annee en avance sur la couverture declaree n'est pas pris.
    ([2022, 2023, 2024, 2025, 2026, 2027], "2025-12-31", [2022, 2023, 2024, 2025]),
])
def test_la_fenetre_glisse_d_elle_meme(publiees, fin, attendues):
    assert millesime.fenetre(publiees, dt.date.fromisoformat(fin)) == attendues


def test_l_etat_distant_est_releve_pour_les_departements_du_territoire():
    lire, lire_json = _serveur(["2025-06", "2025-12"], [2020, 2021, 2022, 2023, 2024, 2025])
    etat = millesime.interroger(["75", "92", "93"], lire, lire_json)
    assert etat.millesime == "2025-12"
    assert etat.fin_couverture == "2025-12-31"
    assert etat.annees == [2021, 2022, 2023, 2024, 2025]
    assert etat.fichiers["2025/75"] == {"taille": 2056139, "date": "2026-05-18T13:14:14.000Z"}
    assert etat.fichiers["2025/93"] == {"absent": True}
    assert "2020/75" not in etat.fichiers


def test_sans_l_api_la_couverture_se_deduit_du_dossier():
    lire, lire_json = _serveur(["2026-06"], [2021, 2022, 2023, 2024, 2025, 2026], couverture=None)
    etat = millesime.interroger(["75"], lire, lire_json)
    assert etat.fin_couverture == "2026-06-30"
    assert etat.annees[-1] == 2026


def test_un_serveur_muet_arrete_tout():
    lire, lire_json = _serveur([], [2025])
    with pytest.raises(millesime.MillesimeIllisible):
        millesime.interroger(["75"], lire, lire_json)


def test_rien_de_neuf_ne_declenche_rien_et_une_revision_si():
    lire, lire_json = _serveur(["2025-12"], [2021, 2022, 2023, 2024, 2025])
    etat = millesime.interroger(["75", "92"], lire, lire_json)
    source = millesime.source_versionnee(etat, dt.date(2026, 9, 25))
    assert millesime.differences(etat, source) == []
    assert millesime.differences(etat, None)

    # Meme millesime, fichier republie : c'est une revision.
    lire, lire_json = _serveur(["2025-12"], [2021, 2022, 2023, 2024, 2025], taille=2060000)
    revise = millesime.interroger(["75", "92"], lire, lire_json)
    raisons = millesime.differences(revise, source)
    assert len(raisons) == 1 and "revise" in raisons[0]

    # Nouveau millesime : la fenetre glisse.
    lire, lire_json = _serveur(["2025-12", "2026-06"], [2021, 2022, 2023, 2024, 2025, 2026],
                               couverture="2026-06-30")
    nouveau = millesime.interroger(["75", "92"], lire, lire_json)
    raisons = " ; ".join(millesime.differences(nouveau, source))
    assert "2025-12 -> 2026-06" in raisons and "annees" in raisons


def test_le_script_ecrit_les_sorties_de_github_actions(tmp_path, monkeypatch):
    lire, lire_json = _serveur(["2025-12"], [2021, 2022, 2023, 2024, 2025])
    etat = millesime.interroger(["75"], lire, lire_json)
    monkeypatch.setattr(millesime, "interroger", lambda departements: etat)
    (tmp_path / "grand-paris").mkdir()
    monkeypatch.setattr(verifier_millesime, "fichier_source", lambda cle: tmp_path / cle / "source.json")
    sorties = tmp_path / "sorties.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(sorties))

    assert verifier_millesime.main(["--etat", str(tmp_path / "etat.json")]) == 0
    lues = dict(ligne.split("=", 1) for ligne in sorties.read_text().splitlines())
    assert lues["nouveau"] == "oui" and lues["annees"] == "2021-2025" and lues["millesime"] == "2025-12"

    # Adopte apres une preparation reussie : le mois suivant, rien de neuf.
    assert verifier_millesime.main(["--adopter", str(tmp_path / "etat.json")]) == 0
    source = json.loads((tmp_path / "grand-paris" / "source.json").read_text())
    assert source["millesime"] == "2025-12" and source["annees"] == [2021, 2022, 2023, 2024, 2025]
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
    # A annees egales, rien n'a bouge : la hausse vient de la nouvelle annee.
    ligne_75 = next(ligne for ligne in texte.splitlines() if ligne.startswith("| 75 |"))
    assert ligne_75.endswith("| +0,0 % |")
    assert "93001 (Commune 93001)" in texte
    assert "1 section(s) sans contour" in texte and "`93001000A1` (4)" in texte
