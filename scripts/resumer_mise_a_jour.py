#!/usr/bin/env python3
"""Rédige la description de la pull request de mise à jour des données.

    python scripts/resumer_mise_a_jour.py --ancien ANCIEN.parquet \
        [--territoire grand-paris] [--millesime 2026-06] [--raisons "..."]

Compare l'ancien parquet (celui de `main`, extrait par `git show`) au
nouveau, tous deux passés par les mêmes règles de nettoyage que le site, et
écrit en Markdown ce qu'un relecteur doit voir avant de fusionner :

- la période couverte, avant et après ;
- le nombre de ventes exploitables par année ;
- le prix médian au m² par département, sur toute la fenêtre et à années
  égales (pour distinguer une révision des données d'un simple glissement
  de fenêtre) ;
- les communes apparues ou disparues, et les sections sans contour, dont les
  ventes comptent mais qui ne sont pas dessinées ;
- les garde-fous : des écarts qu'un glissement de fenêtre n'explique pas.

La pull request est fusionnée sans relecture quand la CI est verte (voir
`.github/workflows/fusion-auto.yml`), sauf si un garde-fou est levé : dans
GitHub Actions, la sortie `a_verifier` vaut alors `oui`, et le workflow pose
l'étiquette `a-verifier`, qui retient la fusion.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

import pandas as pd  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from src import geo, territoires  # noqa: E402
from src.ingestion import (  # noqa: E402
    COLONNES_LUES,
    OptionsNettoyage,
    RapportQualite,
    appliquer_filtres_qualite,
    fichier_mutations,
)


def ventes_nettoyees(chemin: str | Path) -> pd.DataFrame:
    """Les ventes exploitables d'un parquet, avec les règles du site."""

    presentes = set(pq.read_schema(chemin).names)
    mutations = pd.read_parquet(chemin, columns=[c for c in COLONNES_LUES if c in presentes])
    return appliquer_filtres_qualite(mutations, OptionsNettoyage(), RapportQualite())


def _entier(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _ecart(avant, apres) -> str:
    if not avant or pd.isna(avant) or pd.isna(apres):
        return "—"
    return f"{(apres / avant - 1) * 100:+.1f} %".replace(".", ",")


def _periode(ventes: pd.DataFrame) -> str:
    dates = pd.to_datetime(ventes["date_mutation"], errors="coerce").dropna()
    if dates.empty:
        return "aucune vente"
    return f"du {dates.min():%d/%m/%Y} au {dates.max():%d/%m/%Y}"


def _departement(ventes: pd.DataFrame) -> pd.Series:
    return ventes["code_commune"].astype(str).map(geo.departement_de)


#: Garde-fous. Chaque mise à jour fait glisser la fenêtre : une année complète
#: sort, une année (parfois partielle) entre. Le nombre de ventes varie donc
#: d'une vingtaine de pour cent, et les prix sur toute la fenêtre aussi. À
#: années égales, en revanche, Etalab ne fait que de petites révisions.
BAISSE_VENTES_MAX = 0.30  # ventes exploitables, sur toute la fenêtre
BAISSE_ANNEE_MAX = 0.10  # ventes d'une même année, avant et après
ECART_PRIX_EGAL_MAX = 0.05  # prix médian d'un département, à années égales
COMMUNES_DISPARUES_MAX = 3  # une fusion de communes en fait disparaître une ou deux
PART_SANS_CONTOUR_MAX = 0.005  # ventes comptées mais non dessinées


def garde_fous(
    anciennes: pd.DataFrame, nouvelles: pd.DataFrame, orphelines: dict[str, int]
) -> list[str]:
    """Ce qui, dans la mise à jour, mérite un œil humain avant publication."""

    alertes = []
    if len(nouvelles) < (1 - BAISSE_VENTES_MAX) * len(anciennes):
        alertes.append(f"ventes exploitables {_ecart(len(anciennes), len(nouvelles))}")

    egales = sorted(set(anciennes["annee"].dropna()) & set(nouvelles["annee"].dropna()))
    avant = anciennes["annee"].value_counts()
    apres = nouvelles["annee"].value_counts()
    for annee in egales:
        if apres.get(annee, 0) < (1 - BAISSE_ANNEE_MAX) * avant.get(annee, 0):
            alertes.append(f"ventes de {int(annee)} {_ecart(avant[annee], apres.get(annee, 0))}")

    egal_a = anciennes[anciennes["annee"].isin(egales)]
    egal_b = nouvelles[nouvelles["annee"].isin(egales)]
    med_a = egal_a.groupby(_departement(egal_a))["prix_m2"].median()
    med_b = egal_b.groupby(_departement(egal_b))["prix_m2"].median()
    for dep in sorted(set(med_a.index) & set(med_b.index)):
        if med_a[dep] and abs(med_b[dep] / med_a[dep] - 1) > ECART_PRIX_EGAL_MAX:
            alertes.append(f"prix médian du {dep} à années égales {_ecart(med_a[dep], med_b[dep])}")

    disparues = set(anciennes["code_commune"].dropna()) - set(nouvelles["code_commune"].dropna())
    if len(disparues) > COMMUNES_DISPARUES_MAX:
        alertes.append(f"{len(disparues)} communes disparues")

    sans_contour = sum(orphelines.values())
    if len(nouvelles) and sans_contour > PART_SANS_CONTOUR_MAX * len(nouvelles):
        alertes.append(f"{_entier(sans_contour)} ventes dans des sections sans contour")
    return alertes


def resumer(
    anciennes: pd.DataFrame,
    nouvelles: pd.DataFrame,
    orphelines: dict[str, int],
    millesime: str = "",
    raisons: str = "",
) -> str:
    lignes = [f"## Mise à jour des données DVF{' — millésime ' + millesime if millesime else ''}", ""]
    if raisons:
        lignes += [f"Détecté : {raisons}.", ""]
    lignes += [
        f"**Période couverte** : {_periode(anciennes)} → **{_periode(nouvelles)}**.",
        f"**Ventes exploitables** : {_entier(len(anciennes))} → **{_entier(len(nouvelles))}** "
        f"({_ecart(len(anciennes), len(nouvelles))}).",
        "",
        "### Ventes par année",
        "",
        "| Année | Avant | Après | Écart |",
        "|---|---:|---:|---:|",
    ]
    avant = anciennes["annee"].value_counts()
    apres = nouvelles["annee"].value_counts()
    for annee in sorted(set(avant.index) | set(apres.index)):
        a, b = int(avant.get(annee, 0)), int(apres.get(annee, 0))
        lignes.append(f"| {int(annee)} | {_entier(a) if a else '—'} | {_entier(b) if b else '—'} "
                      f"| {_ecart(a, b) if a and b else ('nouvelle' if b else 'retirée')} |")

    communes = sorted(set(anciennes["annee"].dropna().astype(int)) & set(nouvelles["annee"].dropna().astype(int)))
    lignes += [
        "",
        "### Prix médian au m² par département",
        "",
        f"« À années égales » compare les seules années présentes des deux côtés "
        f"({', '.join(map(str, communes)) or 'aucune'}) : un écart y signale une révision "
        "des données par Etalab, et non le glissement de la fenêtre.",
        "",
        "| Département | Avant | Après | Écart | À années égales |",
        "|---|---:|---:|---:|---:|",
    ]
    med_avant = anciennes.groupby(_departement(anciennes))["prix_m2"].median()
    med_apres = nouvelles.groupby(_departement(nouvelles))["prix_m2"].median()
    egal_a = anciennes[anciennes["annee"].isin(communes)]
    egal_b = nouvelles[nouvelles["annee"].isin(communes)]
    med_egal_a = egal_a.groupby(_departement(egal_a))["prix_m2"].median()
    med_egal_b = egal_b.groupby(_departement(egal_b))["prix_m2"].median()
    for dep in sorted(set(med_avant.index) | set(med_apres.index)):
        a, b = med_avant.get(dep), med_apres.get(dep)
        lignes.append(
            f"| {dep} | {_entier(a) + ' €' if pd.notna(a) else '—'} "
            f"| {_entier(b) + ' €' if pd.notna(b) else '—'} | {_ecart(a, b)} "
            f"| {_ecart(med_egal_a.get(dep), med_egal_b.get(dep))} |"
        )

    anciennes_communes = set(anciennes["code_commune"].dropna())
    nouvelles_communes = set(nouvelles["code_commune"].dropna())
    noms = nouvelles.drop_duplicates("code_commune").set_index("code_commune")["nom_commune"]
    apparues = sorted(nouvelles_communes - anciennes_communes)
    disparues = sorted(anciennes_communes - nouvelles_communes)
    lignes += ["", "### Communes et contours", ""]
    lignes.append("- Communes apparues : " + (
        ", ".join(f"{c} ({noms.get(c, '?')})" for c in apparues) if apparues else "aucune"))
    lignes.append("- Communes disparues : " + (", ".join(disparues) if disparues else "aucune"))
    if orphelines:
        total = sum(orphelines.values())
        detail = ", ".join(f"`{s}` ({n})" for s, n in sorted(orphelines.items())[:20])
        lignes.append(
            f"- **{len(orphelines)} section(s) sans contour** ({_entier(total)} ventes comptées "
            f"mais non dessinées) : {detail}{' …' if len(orphelines) > 20 else ''}"
        )
    else:
        lignes.append("- Sections sans contour : aucune")

    alertes = garde_fous(anciennes, nouvelles, orphelines)
    lignes += ["", "### Garde-fous", ""]
    if alertes:
        lignes += [f"- ⚠️ {alerte}" for alerte in alertes]
        lignes += [
            "",
            "**Fusion automatique retenue** (étiquette `a-verifier`). Vérifier la "
            "prévisualisation, puis fusionner à la main — ou fermer la pull request.",
        ]
    else:
        lignes += [
            "Aucun écart inhabituel : cette pull request sera fusionnée automatiquement "
            "dès que la CI sera verte, et le site republié.",
        ]
    return "\n".join(lignes) + "\n"


def sections_orphelines(ventes: pd.DataFrame) -> dict[str, int]:
    """Sections portant des ventes mais sans contour dans `data/geo/`."""

    communes = tuple(sorted(ventes["code_commune"].dropna().unique()))
    sections = tuple(sorted(ventes["code_section"].dropna().unique()))
    resultat = geo.charger_sections(communes, sections, autoriser_reseau=False)
    dessinees = {
        e["properties"]["code_section"] for e in resultat.geojson.get("features", [])
        if "code_section" in e.get("properties", {})
    }
    manquantes = ventes[~ventes["code_section"].isin(dessinees)]["code_section"].dropna()
    return {str(k): int(v) for k, v in manquantes.value_counts().items()}


def main(arguments: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parseur.add_argument("--ancien", required=True, help="parquet d'avant la mise à jour")
    parseur.add_argument("--territoire", default=territoires.CLE_GRAND_PARIS)
    parseur.add_argument("--millesime", default="")
    parseur.add_argument("--raisons", default="")
    options = parseur.parse_args(arguments)

    anciennes = ventes_nettoyees(options.ancien)
    nouvelles = ventes_nettoyees(fichier_mutations(options.territoire))
    orphelines = sections_orphelines(nouvelles)
    print(resumer(anciennes, nouvelles, orphelines, options.millesime, options.raisons))
    if os.environ.get("GITHUB_OUTPUT"):
        a_verifier = "oui" if garde_fous(anciennes, nouvelles, orphelines) else "non"
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as flux:
            flux.write(f"a_verifier={a_verifier}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
