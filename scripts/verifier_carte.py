#!/usr/bin/env python3
"""Ouvre un site déployé dans un vrai navigateur, et vérifie que la carte vit.

    python scripts/verifier_carte.py https://<deploiement>.<projet>.pages.dev

Les tests (`tests/test_navigateur.py`) éprouvent le site servi en local. Ce
script fait le parcours essentiel sur le site **tel que Cloudflare le sert**,
avant qu'il ne passe en production (voir le job `deployer` de la CI) :

- la page s'ouvre sans erreur JavaScript ;
- les communes sont dessinées et colorées, les statistiques d'ouverture
  calculées, et le pied de page cite la période du manifeste ;
- un filtre relance le calcul dans le navigateur (le Worker, les ventes) ;
- au zoom des rues, les ventes arrivent en tuiles et se dessinent.

Il échoue au premier manquement. Les fonds de carte ne comptent pas : la carte
doit vivre sans eux, et leur santé est l'affaire de la veille.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verifier_deploiement  # noqa: E402

ATTENTE_MS = 120_000
TRANCHE_MS = 5_000


def verifier(adresse: str) -> list[str]:
    from playwright.sync_api import sync_playwright

    racine = adresse.rstrip("/")
    # Lu comme le lit verifier_deploiement.py : avec un User-Agent (Cloudflare
    # refuse celui de urllib par defaut, « Python-urllib »), et en reessayant.
    manifeste = json.loads(verifier_deploiement.lire(f"{racine}/manifeste.json", delai=60)[2])
    debut = "/".join(reversed(manifeste["periode"]["debut"].split("-")))
    fin = "/".join(reversed(manifeste["periode"]["fin"].split("-")))

    options = {"args": ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]}
    if os.environ.get("CHROMIUM_EXECUTABLE"):
        options["executable_path"] = os.environ["CHROMIUM_EXECUTABLE"]
    constats = []
    with sync_playwright() as playwright:
        navigateur = playwright.chromium.launch(**options)
        page = navigateur.new_page(viewport={"width": 1400, "height": 900})
        erreurs: list[str] = []
        page.on("pageerror", lambda erreur: erreurs.append(str(erreur)))

        def attendre(expression: str, etape: str, argument=None) -> None:
            # Par tranches de 5 s : une erreur JavaScript arrete tout sans
            # attendre la fin du delai.
            for _ in range(ATTENTE_MS // TRANCHE_MS):
                if erreurs:
                    raise SystemExit(f"{etape} : erreur JavaScript : {erreurs}")
                try:
                    page.wait_for_function(
                        f"([a]) => {{ const carte = window.carteDvf; return !!carte && ({expression})(carte, a); }}",
                        arg=[argument], timeout=TRANCHE_MS,
                    )
                except Exception:  # noqa: BLE001 - tranche ecoulee
                    continue
                if erreurs:
                    raise SystemExit(f"{etape} : erreur JavaScript : {erreurs}")
                constats.append(etape)
                return
            raise SystemExit(f"{etape} : rien après {ATTENTE_MS // 1000} s.")

        page.goto(racine + "/")
        attendre(
            "(carte) => carte.isStyleLoaded() && carte.isSourceLoaded('communes')"
            " && carte.queryRenderedFeatures({layers: ['zones-communes']}).some("
            "(e) => carte.getFeatureState({source: 'communes', id: e.id}).couleur)"
            " && window.siteDvf.calculs >= 1",
            "Communes colorées à l'ouverture",
        )
        pied = page.locator("#pied").inner_text()
        if f"ventes du {debut} au {fin}" not in pied:
            raise SystemExit(f"Le pied de page ne cite pas la période du manifeste ({debut} – {fin}) : {pied!r}")
        constats.append(f"Pied de page : ventes du {debut} au {fin}")

        calculs = page.evaluate("() => window.siteDvf.calculs")
        page.locator("#filtre-annees button").first.click()
        attendre("(carte, n) => window.siteDvf.calculs > n && window.siteDvf.complet",
                 "Filtre recalculé dans le navigateur", calculs)
        page.locator("#filtre-annees button").first.click()

        page.evaluate("() => window.carteDvf.jumpTo({center: [2.3488, 48.8534], zoom: 16.5},"
                      " {originalEvent: true})")
        attendre("(carte) => carte.queryRenderedFeatures({layers: ['transactions']}).length > 0",
                 "Ventes dessinées au zoom des rues")
        navigateur.close()
    return constats


def main(adresse: str) -> int:
    constats = verifier(adresse)
    print(f"### Carte vérifiée dans un navigateur : {adresse}\n")
    print("\n".join(f"- {c}" for c in constats))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
