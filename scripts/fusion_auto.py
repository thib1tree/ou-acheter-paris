#!/usr/bin/env python3
"""Fusionne les pull requests automatiques dont la CI est verte, une à la fois.

    python scripts/fusion_auto.py [--simulation]

Lancé par `.github/workflows/fusion-auto.yml` à la fin de chaque CI, et une
fois par jour en rattrapage. Sont concernées les seules pull requests nées
d'une automatisation, sur une branche du dépôt :

- Dependabot (`dependabot/…`) : actions de GitHub et dépendances Python ;
- les workflows du dépôt : données (`donnees/…`), gares (`reseau/…`), version
  de Python (`maintenance/…`).

Toute autre pull request (une contribution, un changement fait à la main)
attend une fusion humaine. L'étiquette `a-verifier` retient aussi la fusion :
la mise à jour des données la pose quand un garde-fou est levé.

Une fusion par passage, et seulement si la CI de `main` est verte : le
passage suivant n'a lieu qu'après la CI du `main` ainsi modifié, si bien que
chaque fusion est vérifiée, et déployée, avant la suivante. Un `main` rouge
arrête tout — la veille (`veiller.py`) le signale.

Une CI rouge est relancée une fois (un test peut échouer par accident) ; une
pull request sans CI (ouverte avec le jeton par défaut de GitHub Actions, qui
n'en déclenche pas) la reçoit à la main. Sans jeton personnel
(`JETON_MISE_A_JOUR`), la fusion se fait avec ce même jeton par défaut, et la
CI de `main` est alors lancée à la main elle aussi.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_api import Api, ErreurApi  # noqa: E402

BRANCHES_AUTOMATIQUES = ("donnees/", "reseau/", "maintenance/")
DEPENDABOT = "dependabot[bot]"
ETIQUETTE_RETENUE = "a-verifier"
WORKFLOW_CI = "ci.yml"
#: Une CI rouge est relancée jusqu'à ce numéro de tentative, exclu.
TENTATIVES_MAX = 2


def motif_de_refus(pr: dict, depot: str) -> str | None:
    """Pourquoi cette pull request n'est pas fusionnée d'office (None : elle l'est)."""

    tete = pr.get("head") or {}
    if (tete.get("repo") or {}).get("full_name") != depot:
        return "branche hors du dépôt"
    if pr.get("draft"):
        return "brouillon"
    branche = tete.get("ref", "")
    auteur = (pr.get("user") or {}).get("login")
    if branche.startswith("dependabot/"):
        if auteur != DEPENDABOT:
            return "branche dependabot/ poussée par un autre que Dependabot"
    elif not branche.startswith(BRANCHES_AUTOMATIQUES):
        return "pull request faite à la main"
    if any(e.get("name") == ETIQUETTE_RETENUE for e in pr.get("labels") or []):
        return f"étiquette {ETIQUETTE_RETENUE}"
    return None


def derniere_ci(api: Api, requete: str) -> dict | None:
    runs = api.get(api.chemin(f"/actions/workflows/{WORKFLOW_CI}/runs?per_page=20&{requete}"))
    runs = (runs or {}).get("workflow_runs") or []
    return max(runs, key=lambda r: (r.get("created_at", ""), r.get("id", 0)), default=None)


def etat_ci(run: dict | None) -> str:
    """`absente`, `en_cours`, `verte` ou `rouge`."""

    if run is None:
        return "absente"
    if run.get("status") != "completed":
        return "en_cours"
    return "verte" if run.get("conclusion") == "success" else "rouge"


def sante_de_la_base(api: Api, base: str) -> str:
    """État de la dernière CI de la branche principale (push ou lancement manuel)."""

    runs = api.get(api.chemin(f"/actions/workflows/{WORKFLOW_CI}/runs?per_page=10&branch={base}"))
    runs = [
        r for r in (runs or {}).get("workflow_runs") or []
        if r.get("event") in ("push", "workflow_dispatch")
    ]
    if not runs:
        return "verte"
    return etat_ci(max(runs, key=lambda r: (r.get("created_at", ""), r.get("id", 0))))


def balayer(api: Api, api_fusion: Api | None = None, simulation: bool = False, journal=print) -> int | None:
    """Un passage. Rend le numéro de la pull request fusionnée, s'il y en a une.

    `api` (le jeton par défaut de GitHub Actions) lit, relance et lance les CI ;
    `api_fusion` (le jeton personnel, s'il existe) fusionne : lui seul peut
    fusionner une pull request qui touche aux workflows, et sa fusion lance
    d'elle-même la CI de `main`.
    """

    avec_jeton = api_fusion is not None
    api_fusion = api_fusion or api

    base = api.get(api.chemin())["default_branch"]
    sante = sante_de_la_base(api, base)
    journal(f"CI de {base} : {sante}")
    prs = api.get(api.chemin("/pulls?state=open&per_page=100&sort=created&direction=asc")) or []

    fusionnee = None
    for pr in prs:
        numero, titre, sha = pr["number"], pr["title"], pr["head"]["sha"]
        refus = motif_de_refus(pr, api.depot)
        if refus:
            journal(f"#{numero} laissée de côté : {refus}")
            continue
        run = derniere_ci(api, f"head_sha={sha}")
        etat = etat_ci(run)
        journal(f"#{numero} « {titre} » : CI {etat}")
        if simulation:
            continue
        if etat == "verte" and fusionnee is None and sante == "verte":
            try:
                api_fusion.appeler("PUT", api.chemin(f"/pulls/{numero}/merge"), {
                    "merge_method": "squash", "sha": sha, "commit_title": f"{titre} (#{numero})",
                })
            except ErreurApi as erreur:
                # Un conflit, ou un jeton sans le droit de toucher aux workflows :
                # la pull request reste ouverte, la veille la signalera si elle dure.
                journal(f"#{numero} : fusion refusée ({erreur})")
                continue
            fusionnee = numero
            journal(f"#{numero} fusionnée")
            try:
                api.appeler("DELETE", api.chemin(f"/git/refs/heads/{pr['head']['ref']}"))
            except ErreurApi:
                pass  # déjà supprimée par GitHub (réglage du dépôt)
        elif etat == "rouge" and run.get("run_attempt", 1) < TENTATIVES_MAX:
            _tenter(journal, f"#{numero} : CI relancée une fois", api.appeler,
                    "POST", api.chemin(f"/actions/runs/{run['id']}/rerun-failed-jobs"))
        elif etat == "absente":
            _tenter(journal, f"#{numero} : CI lancée", api.appeler,
                    "POST", api.chemin(f"/actions/workflows/{WORKFLOW_CI}/dispatches"),
                    {"ref": pr["head"]["ref"]})

    # Poussé par le jeton par défaut, le `main` fusionné ne lancerait pas la CI,
    # donc pas le déploiement.
    if fusionnee is not None and not avec_jeton:
        _tenter(journal, f"CI de {base} lancée", api.appeler,
                "POST", api.chemin(f"/actions/workflows/{WORKFLOW_CI}/dispatches"), {"ref": base})
    return fusionnee


def _tenter(journal, succes: str, appel, *arguments) -> None:
    """Un appel dont l'échec n'arrête pas le passage : le suivant réessaiera."""

    try:
        appel(*arguments)
    except ErreurApi as erreur:
        journal(f"{succes.split(' : ')[0]} : échec ({erreur})")
    else:
        journal(succes)


def main(arguments: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parseur.add_argument("--simulation", action="store_true", help="dit ce qui serait fait, sans rien faire")
    options = parseur.parse_args(arguments)
    jeton = os.environ.get("JETON_MISE_A_JOUR", "")
    balayer(Api(), Api(jeton) if jeton else None, simulation=options.simulation,
            journal=lambda ligne: print(f"- {ligne}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
