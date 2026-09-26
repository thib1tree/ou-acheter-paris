#!/usr/bin/env python3
"""Etalab a-t-il publie de nouvelles DVF geolocalisees depuis la derniere preparation ?

    python scripts/verifier_millesime.py [--territoire grand-paris] [--etat FICHIER]
    python scripts/verifier_millesime.py --adopter FICHIER [--territoire grand-paris]

Sans option, compare ce qu'Etalab publie aujourd'hui a l'empreinte versionnee
(`data/territoires/<cle>/source.json`) et dit ce qui a change. `--etat`
enregistre l'etat releve ; `--adopter` le recopie dans `source.json`, une fois
la preparation reussie — c'est l'etat *releve avant* la preparation qui est
adopte, pas celui du moment, pour qu'une publication survenue entre-temps
soit vue au passage suivant.

Dans GitHub Actions, les sorties `nouveau` (`oui`/`non`), `annees`
(`2021-2025`), `millesime` et `raisons` sont ecrites dans `$GITHUB_OUTPUT`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import millesime, territoires  # noqa: E402
from src.ingestion import repertoire_territoire  # noqa: E402


def fichier_source(cle: str) -> Path:
    return Path(repertoire_territoire(cle)) / "source.json"


def ecrire_sorties(sorties: dict[str, str]) -> None:
    chemin = os.environ.get("GITHUB_OUTPUT")
    if not chemin:
        return
    with open(chemin, "a", encoding="utf-8") as flux:
        for cle, valeur in sorties.items():
            # Une fin de ligne venue d'un index distant ajouterait une sortie.
            valeur = " ".join(str(valeur).splitlines())
            flux.write(f"{cle}={valeur}\n")


def main(arguments: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parseur.add_argument("--territoire", default=territoires.CLE_GRAND_PARIS)
    parseur.add_argument("--etat", help="enregistre l'etat releve dans ce fichier")
    parseur.add_argument("--adopter", help="recopie cet etat dans source.json")
    options = parseur.parse_args(arguments)
    source = fichier_source(options.territoire)

    if options.adopter:
        etat = millesime.EtatDistant(**json.loads(Path(options.adopter).read_text(encoding="utf-8")))
        source.write_text(
            json.dumps(millesime.source_versionnee(etat), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{source} : millesime {etat.millesime}, annees {etat.annees[0]}-{etat.annees[-1]}")
        return 0

    departements = [
        d for d in territoires.territoire(options.territoire).departements
        if d not in territoires.DEPARTEMENTS_SANS_DVF
    ]
    etat = millesime.interroger(departements)
    ancienne = json.loads(source.read_text(encoding="utf-8")) if source.exists() else None
    raisons = millesime.differences(etat, ancienne)

    print(f"Millesime publie : {etat.millesime} (donnees jusqu'au {etat.fin_couverture})")
    print(f"Annees publiees : {etat.annees_publiees} ; fenetre retenue : {etat.annees}")
    print("Changements : " + ("; ".join(raisons) if raisons else "aucun"))
    if options.etat:
        Path(options.etat).write_text(json.dumps(etat.__dict__, ensure_ascii=False), encoding="utf-8")
    ecrire_sorties({
        "nouveau": "oui" if raisons else "non",
        "annees": f"{etat.annees[0]}-{etat.annees[-1]}" if etat.annees == list(
            range(etat.annees[0], etat.annees[-1] + 1)) else ",".join(map(str, etat.annees)),
        "millesime": etat.millesime,
        "raisons": " ; ".join(raisons),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
