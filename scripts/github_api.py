"""Un client minimal de l'API REST de GitHub, pour les workflows de maintenance.

Bibliotheque standard seulement : la fusion automatique et la veille n'ont
besoin ni de `gh` ni de dependances. Le jeton vient de l'environnement
(`GH_TOKEN`), le depot aussi (`GITHUB_REPOSITORY`), comme dans GitHub Actions.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

RACINE_API = "https://api.github.com"


class ErreurApi(RuntimeError):
    def __init__(self, statut: int, message: str):
        super().__init__(f"HTTP {statut} : {message}")
        self.statut = statut


class Api:
    """`appeler("GET", "/repos/o/d/pulls")` rend `(statut, en-tetes, corps JSON)`."""

    def __init__(self, jeton: str | None = None, depot: str | None = None):
        self.jeton = jeton if jeton is not None else os.environ.get("GH_TOKEN", "")
        self.depot = depot or os.environ.get("GITHUB_REPOSITORY", "")

    def appeler(self, methode: str, chemin: str, donnees: dict | None = None):
        adresse = chemin if chemin.startswith("http") else RACINE_API + chemin
        corps = json.dumps(donnees).encode() if donnees is not None else None
        requete = urllib.request.Request(adresse, data=corps, method=methode, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ou-acheter-paris-maintenance",
            **({"Authorization": f"Bearer {self.jeton}"} if self.jeton else {}),
            **({"Content-Type": "application/json"} if corps is not None else {}),
        })
        try:
            with urllib.request.urlopen(requete, timeout=60) as reponse:
                brut = reponse.read()
                entetes = {k.lower(): v for k, v in reponse.headers.items()}
                return reponse.status, entetes, json.loads(brut) if brut.strip() else None
        except urllib.error.HTTPError as erreur:
            message = erreur.read().decode("utf-8", "replace")[:500]
            raise ErreurApi(erreur.code, message) from None

    def get(self, chemin: str):
        return self.appeler("GET", chemin)[2]

    def chemin(self, suite: str = "") -> str:
        """Le chemin d'API du depot courant, suivi de `suite`."""

        return f"/repos/{self.depot}{suite}"
