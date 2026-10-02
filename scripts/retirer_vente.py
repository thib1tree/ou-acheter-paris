#!/usr/bin/env python3
"""Retrouve une vente et la retire de la carte (droit d'opposition, RGPD).

    python scripts/retirer_vente.py --commune Montreuil --adresse "rue de paris" [--date 2024-03]
    python scripts/retirer_vente.py ... --ajouter

Sans `--ajouter`, le script liste les ventes qui correspondent, telles que
l'infobulle les montre. Avec, il ecrit leurs lignes dans `data/retraits.csv`
(voir `src.ingestion.ventes_retirees`) : la vente disparait de la carte et des
statistiques a la construction suivante, donc des la fusion sur `main`.

Une demande vise d'ordinaire une seule vente : `--ajouter` refuse d'en retirer
plus de `--max` (5 par defaut) d'un coup, de peur d'effacer une rue entiere sur
une recherche trop large.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

import pandas as pd  # noqa: E402

from src import territoires  # noqa: E402
from src.ingestion import (  # noqa: E402
    _adresse_comparable,
    chemin_retraits,
    fichier_mutations,
    ventes_retirees,
)


def chercher(mutations: pd.DataFrame, commune: str, adresse: str, date: str | None) -> pd.DataFrame:
    """Les ventes de `commune` (code INSEE ou nom) dont l'adresse contient `adresse`."""

    commune = commune.strip()
    if commune[:1].isdigit():
        masque = mutations["code_commune"].astype(str) == commune
    else:
        masque = mutations["nom_commune"].astype(str).str.casefold().str.contains(
            commune.casefold(), regex=False)
    masque &= mutations["adresse"].map(_adresse_comparable).str.contains(
        _adresse_comparable(adresse), regex=False)
    if date:
        masque &= pd.to_datetime(mutations["date_mutation"]).dt.strftime("%Y-%m-%d").str.startswith(date)
    return mutations[masque].sort_values("date_mutation")


def ligne_retrait(vente) -> str:
    return f"{pd.Timestamp(vente.date_mutation):%Y-%m-%d};{vente.code_commune};{vente.adresse}"


def ventes_retirees_contient(retraits: set[tuple[str, str, str]], ligne: str) -> bool:
    jour, commune, adresse = ligne.split(";", 2)
    return (jour, commune, _adresse_comparable(adresse)) in retraits


def main(arguments: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parseur.add_argument("--territoire", default=territoires.CLE_GRAND_PARIS)
    parseur.add_argument("--commune", required=True, help="code INSEE (75111) ou nom (Montreuil)")
    parseur.add_argument("--adresse", required=True, help="tout ou partie de l'adresse")
    parseur.add_argument("--date", help="AAAA, AAAA-MM ou AAAA-MM-JJ")
    parseur.add_argument("--ajouter", action="store_true", help="ecrit les ventes trouvees dans data/retraits.csv")
    parseur.add_argument("--max", type=int, default=5, help="nombre de ventes au-dela duquel --ajouter refuse")
    options = parseur.parse_args(arguments)

    mutations = pd.read_parquet(
        fichier_mutations(options.territoire),
        columns=["date_mutation", "code_commune", "nom_commune", "adresse", "type_bien",
                 "surface_bati", "valeur_fonciere"],
    )
    trouvees = chercher(mutations, options.commune, options.adresse, options.date)
    if trouvees.empty:
        print("Aucune vente ne correspond.")
        return 1
    deja = ventes_retirees()
    for vente in trouvees.itertuples():
        ligne = ligne_retrait(vente)
        etat = " (déjà retirée)" if ventes_retirees_contient(deja, ligne) else ""
        prix = f"{vente.valeur_fonciere:,.0f}".replace(",", "\u202f")
        print(f"{ligne}  ·  {vente.nom_commune}, {vente.type_bien}, "
              f"{vente.surface_bati:g} m², {prix} €{etat}")

    if not options.ajouter:
        print(f"\n{len(trouvees)} vente(s). Relancer avec --ajouter pour les retirer de la carte.")
        return 0
    if len(trouvees) > options.max:
        print(f"\n{len(trouvees)} ventes : précisez la recherche (--date, adresse complète) "
              f"ou relevez --max.", file=sys.stderr)
        return 2
    nouvelles = [ligne_retrait(v) for v in trouvees.itertuples()
                 if not ventes_retirees_contient(deja, ligne_retrait(v))]
    chemin = Path(chemin_retraits())
    texte = chemin.read_text(encoding="utf-8") if chemin.exists() else ""
    if texte and not texte.endswith("\n"):
        texte += "\n"
    chemin.write_text(texte + "".join(f"{ligne}\n" for ligne in nouvelles), encoding="utf-8")
    print(f"\n{len(nouvelles)} ligne(s) ajoutée(s) à {chemin}. "
          "Reconstruire le site, puis fusionner sur main.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
