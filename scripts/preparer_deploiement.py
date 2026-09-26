#!/usr/bin/env python3
"""Rassemble dans `data/geo/` tout ce que l'application doit trouver hors ligne.

À lancer **depuis un poste connecté**, après avoir préparé un territoire, puis
à committer :

    python scripts/preparer_deploiement.py [--territoire grand-paris ...]
    git add data/geo && git commit -m "Contours et gares du territoire analysé"

Sans argument, tous les territoires déjà préparés sont traités.

Deux choses sont récupérées :

* les **contours cadastraux** des communes qui n'en ont pas encore. Sans eux,
  les sections de ces communes sont absentes de la carte alors que leurs ventes
  comptent dans les statistiques — un écart qui passe inaperçu ;
* les **gares** de l'emprise analysée, téléchargées une seule fois.

Un fichier déjà complet n'est pas retouché ; un fichier **incomplet** (millésime
cadastral antérieur, fichier tronqué) est retéléchargé et réécrit. Le script se
termine avec un code de retour non nul s'il manque encore quelque chose.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import geo  # noqa: E402
from src.ingestion import charger_mutations_territoire, territoires_prepares  # noqa: E402


def main(cles: list[str] | None = None) -> int:
    cles = cles or territoires_prepares()
    if not cles:
        print(
            "Aucun territoire prepare. Lancez d'abord "
            "`python scripts/preparer_territoire.py --territoire <cle>`.",
            file=sys.stderr,
        )
        return 1

    morceaux = []
    for cle in cles:
        mutations, _ = charger_mutations_territoire(cle)
        morceaux.append(mutations[["code_commune", "code_section"]])
    transactions = pd.concat(morceaux, ignore_index=True)

    communes = sorted(transactions["code_commune"].dropna().unique())
    sections = transactions["code_section"].dropna().unique()
    print(
        f"Territoire(s) {', '.join(cles)} | {len(communes)} commune(s) | "
        f"{len(sections)} sections\n"
    )

    # --- Contours cadastraux ------------------------------------------------
    # Un fichier present mais incomplet est retelecharge : c'est le cas d'un
    # millesime anterieur a une refonte du decoupage, ou d'un fichier tronque.
    a_traiter = []
    for insee in communes:
        attendues = {s for s in sections if s.startswith(insee)}
        if attendues - geo.sections_locales(insee):
            a_traiter.append(insee)

    if a_traiter:
        print(f"Contours a recuperer pour {len(a_traiter)} commune(s) : {', '.join(a_traiter)}")
        ecrites, echecs = geo.telecharger_contours_manquants(communes, sections)
        for insee, nombre in sorted(ecrites.items()):
            print(f"  OK  {insee} - {nombre} sections -> {Path(geo.fichier_contours(insee)).name}")
        for insee, raison in sorted(echecs.items()):
            print(f"  PROBLEME {insee} - {raison}", file=sys.stderr)
    else:
        print("Contours cadastraux : deja tous presents et complets.")

    resultat = geo.charger_sections(communes, sections, autoriser_reseau=True)
    couvertes = {e["properties"]["code_section"] for e in resultat.geojson["features"]}
    orphelines = set(sections) - couvertes
    if orphelines:
        ventes = int(transactions["code_section"].isin(orphelines).sum())
        print(
            f"\nATTENTION : {len(orphelines)} section(s) sans contour, soit {ventes} vente(s) "
            "comptees dans les statistiques mais absentes de la carte.",
            file=sys.stderr,
        )
        # Detail par commune : indispensable pour distinguer un simple defaut de
        # telechargement d'une divergence de codification entre DVF et cadastre.
        for insee in sorted({code[:5] for code in orphelines}):
            absentes = sorted(code for code in orphelines if code.startswith(insee))
            disponibles = sorted(geo.sections_locales(insee))
            print(
                f"  {insee} : {len(absentes)} section(s) introuvable(s) "
                f"{absentes[:6]}{' ...' if len(absentes) > 6 else ''}",
                file=sys.stderr,
            )
            print(
                f"      cadastre disponible pour cette commune : {len(disponibles)} section(s) "
                f"{disponibles[:6]}{' ...' if len(disponibles) > 6 else ''}",
                file=sys.stderr,
            )

    # --- Gares --------------------------------------------------------------
    emprise = geo.etendue(resultat.geojson)
    if not emprise:
        print("\nAucun contour disponible : gares non recuperables.", file=sys.stderr)
        return 1

    gares, message = geo.charger_gares(emprise, autoriser_reseau=True)
    print(f"\nGares : {message or 'aucun message'}")
    if gares:
        affichees = geo.gares_dans_sections(gares, resultat.geojson)
        print(f"  {len(affichees)} gare(s) a l'interieur des sections chargees, sur {len(gares)}.")

    print('\nA committer : git add data/geo && git commit -m "Contours et gares"')
    return 1 if orphelines or not gares else 0


if __name__ == "__main__":
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument(
        "--territoire", action="append", dest="territoires",
        help="Cle d'un territoire prepare ; repetable. Par defaut, tous.",
    )
    raise SystemExit(main(analyseur.parse_args().territoires))
