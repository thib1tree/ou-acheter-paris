#!/usr/bin/env python3
"""Remet en production le dernier déploiement sain, après un déploiement raté.

    python scripts/retablir_production.py PROJET ADRESSE_DU_DEPLOIEMENT_RATE

Lancé par la CI quand la vérification du site en production échoue (le site a
pourtant passé la même vérification à l'adresse de contrôle juste avant :
c'est le cas rare). Il demande à Cloudflare Pages de revenir au déploiement de
production précédent, réussi, autre que celui qui vient d'échouer. La CI reste
rouge : la veille le signale.

Attend `CLOUDFLARE_API_TOKEN` et `CLOUDFLARE_ACCOUNT_ID` dans l'environnement.
Bibliothèque standard seulement.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

API = "https://api.cloudflare.com/client/v4"


def deploiement_sain(deploiements: list[dict], adresse_ratee: str) -> dict | None:
    """Le plus récent déploiement de production réussi, hors celui qui a raté."""

    ratee = adresse_ratee.rstrip("/")
    candidats = [
        d for d in deploiements
        if d.get("environment") == "production"
        and (d.get("latest_stage") or {}).get("status") == "success"
        and (d.get("url") or "").rstrip("/") != ratee
    ]
    return max(candidats, key=lambda d: d.get("created_on", ""), default=None)


def _appeler(methode: str, chemin: str) -> dict:
    requete = urllib.request.Request(API + chemin, method=methode, headers={
        "Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(requete, timeout=60) as reponse:
        return json.loads(reponse.read())


def main(projet: str, adresse_ratee: str) -> int:
    base = f"/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}/pages/projects/{projet}/deployments"
    deploiements = _appeler("GET", f"{base}?env=production").get("result") or []
    cible = deploiement_sain(deploiements, adresse_ratee)
    if cible is None:
        print("Aucun déploiement de production sain à rétablir.")
        return 1
    _appeler("POST", f"{base}/{cible['id']}/rollback")
    print(f"Production rétablie sur le déploiement du {cible.get('created_on', '?')[:16]} ({cible.get('url')}).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1], sys.argv[2]))
