#!/usr/bin/env python3
"""Actualise les gares versionnees (`data/geo/gares.json`) depuis leurs sources.

    python scripts/actualiser_gares.py [--resume FICHIER]

Les gares ne sont telechargees qu'une fois par `preparer_deploiement.py` :
c'est ce qui rend la construction du site independante du reseau. Ce script
les retelecharge et remplace le fichier, pour qu'une gare ouverte depuis ne
reste pas affichee « a venir ». Il est lance chaque mois par le workflow
`reseau.yml`, qui ouvre une pull request si quelque chose a change.

`--resume` ecrit la description de cette pull request (Markdown). Dans GitHub
Actions, la sortie `change` (`oui`/`non`) est ecrite dans `$GITHUB_OUTPUT`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import geo  # noqa: E402


def _liste(titre: str, elements: list[str], plafond: int = 60) -> list[str]:
    if not elements:
        return []
    lignes = [f"**{titre}** ({len(elements)})", ""]
    lignes += [f"- {e}" for e in elements[:plafond]]
    if len(elements) > plafond:
        lignes.append(f"- … et {len(elements) - plafond} autre(s)")
    return lignes + [""]


def resume(rapport: geo.ActualisationGares) -> str:
    lignes = [
        "Actualisation mensuelle des gares (`data/geo/gares.json`), depuis les jeux "
        "d'Île-de-France Mobilités : gares en service et arrêts en projet.",
        "",
        f"Gares : {rapport.avant} avant, {rapport.apres} après.",
        "",
    ]
    lignes += _liste(
        "Lignes qui ne sont plus « à venir »", rapport.ouvertes
    )
    if rapport.ouvertes:
        lignes += [
            "_Mises en service le plus souvent (la gare en service de la même ligne "
            "les remplace) ; parfois retirées ou renommées dans le jeu des projets._",
            "",
        ]
    lignes += _liste("Lignes nouvellement annoncées", rapport.annoncees)
    lignes += _liste("Gares en service apparues", rapport.apparues)
    lignes += _liste("Gares en service disparues ou renommées", rapport.disparues)
    lignes += ["**Journal**", ""] + [f"- {m}" for m in rapport.messages] + [""]
    lignes.append("La CI de cette pull request publie une prévisualisation ; la pull request est "
                  "fusionnée automatiquement si elle est verte.")
    return "\n".join(lignes) + "\n"


def main(arguments: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parseur.add_argument("--resume", help="ecrit la description de la pull request dans ce fichier")
    options = parseur.parse_args(arguments)

    rapport = geo.actualiser_gares()
    texte = resume(rapport)
    print(texte)
    if options.resume:
        Path(options.resume).write_text(texte, encoding="utf-8")
    sortie = os.environ.get("GITHUB_OUTPUT")
    if sortie:
        with open(sortie, "a", encoding="utf-8") as flux:
            flux.write(f"change={'oui' if rapport.ecrit else 'non'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
