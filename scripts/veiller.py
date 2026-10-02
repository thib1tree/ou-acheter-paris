#!/usr/bin/env python3
"""Veille : ce qui pourrait casser le site sans que personne ne s'en aperçoive.

    python scripts/veiller.py [--site https://www.ou-acheter-paris.fr/]

Lancé chaque semaine par `.github/workflows/veille.yml`. Rien à faire tant
que tout va bien ; sinon, le workflow ouvre une issue (étiquette `veille`),
et la referme de lui-même quand tout est rentré dans l'ordre. Sont vérifiés :

- le site en ligne : la page, le manifeste et les fichiers qu'il désigne
  (`verifier_deploiement.py`) ;
- la fraîcheur des données en ligne : Etalab publie tous les six mois, avec
  six mois de décalage. Des ventes arrêtées depuis plus de quinze mois
  disent qu'une mise à jour s'est perdue (Etalab ne publie plus, ou ailleurs ;
  une pull request attend ; le déploiement échoue) ;
- les fonds de carte : chaque serveur de tuiles sert-il encore une tuile ?
- le jeton `JETON_MISE_A_JOUR` : présent, valide, et loin de son expiration ;
- les pull requests ouvertes depuis trop longtemps, et la CI de `main` ;
- les workflows : GitHub coupe ceux qui sont programmés après 60 jours sans
  activité dans un dépôt public. La veille les réactive (et, ce faisant,
  repousse l'échéance) ;
- MapLibre GL JS, copié dans `site/carte/vendor/` hors de portée de
  Dependabot : une faille connue de cette version (base OSV) est un
  problème ; une version plus récente, une simple information ;
- la taille du dépôt : chaque millésime de données y ajoute quelques dizaines
  de mégaoctets. Au-delà de `TAILLE_DEPOT_MAX`, il est temps de sortir les
  données de l'historique (Git LFS, ou fichiers attachés aux releases) ;
- la version de Python des workflows (`.python-version`) : un an avant sa fin
  de vie, la sortie `python` propose la plus récente des versions parues
  depuis plus d'un an. Le workflow en fait une pull request, que la fusion
  automatique fusionne si la CI est verte.

Le rapport, en Markdown, sort sur la sortie standard ; dans GitHub Actions,
les sorties `probleme` (`oui`/`non`) et `python` vont dans `$GITHUB_OUTPUT`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
sys.path.insert(0, str(RACINE / "scripts"))

import verifier_deploiement  # noqa: E402
from github_api import Api, ErreurApi  # noqa: E402

ADRESSE_SITE = "https://www.ou-acheter-paris.fr/"
FRAICHEUR_MAX = dt.timedelta(days=460)
PR_EN_ATTENTE_MAX = dt.timedelta(days=21)
PREAVIS_JETON = dt.timedelta(days=30)
PREAVIS_PYTHON = dt.timedelta(days=365)
RECUL_PYTHON = dt.timedelta(days=365)
URL_CYCLES_PYTHON = "https://peps.python.org/api/release-cycle.json"
URL_OSV = "https://api.osv.dev/v1/query"
URL_MAPLIBRE = "https://registry.npmjs.org/maplibre-gl/latest"
PROVENANCE_MAPLIBRE = RACINE / "site" / "carte" / "vendor" / "PROVENANCE.txt"
#: GitHub recommande de rester sous le gigaoctet ; on prévient avant.
TAILLE_DEPOT_MAX = 750 * 1024 * 1024
#: Une tuile de Paris (zoom 12), servie par tous les fonds.
TUILE_TEST = {"z": 12, "x": 2074, "y": 1409}
ENTETES = {"User-Agent": "ou-acheter-paris-veille/1.0 (+https://github.com/thib1tree/ou-acheter-paris)"}


class Rapport:
    def __init__(self):
        self.problemes: list[str] = []
        self.constats: list[str] = []

    def probleme(self, texte: str) -> None:
        self.problemes.append(texte)

    def ok(self, texte: str) -> None:
        self.constats.append(texte)

    def markdown(self, jour: dt.date) -> str:
        lignes = [f"## Veille du {jour:%d/%m/%Y}", ""]
        lignes += [f"- ⚠️ {p}" for p in self.problemes]
        lignes += [f"- ✅ {c}" for c in self.constats]
        return "\n".join(lignes) + "\n"


def _date(texte: str) -> dt.date:
    """`2025-12-31`, `2025-12-31T10:00:00Z`, `2029-10` (fin du mois omis : le 1er)."""

    texte = texte.strip()[:10]
    return dt.date.fromisoformat(texte if len(texte) == 10 else texte[:7] + "-01")


def _lire(adresse: str, essais: int = 2) -> tuple[int, dict[str, str], bytes]:
    for essai in range(essais):
        try:
            with urllib.request.urlopen(urllib.request.Request(adresse, headers=ENTETES), timeout=30) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except Exception as erreur:  # noqa: BLE001 - toute panne se vaut ici
            if essai == essais - 1:
                raise erreur
            time.sleep(30)
    raise AssertionError("inatteignable")


def _json(adresse: str, donnees: dict | None = None):
    """GET, ou POST de `donnees` en JSON ; rend le corps de la réponse décodé."""

    corps = json.dumps(donnees).encode() if donnees is not None else None
    requete = urllib.request.Request(adresse, data=corps, headers={
        **ENTETES, **({"Content-Type": "application/json"} if corps else {}),
    })
    with urllib.request.urlopen(requete, timeout=30) as reponse:
        return json.loads(reponse.read() or b"{}")


# --------------------------------------------------------------------------
# Le site et ses sources
# --------------------------------------------------------------------------


def verifier_site(rapport: Rapport, adresse: str, aujourd_hui: dt.date) -> None:
    racine = adresse.rstrip("/")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            verifier_deploiement.main(racine)
    except SystemExit as erreur:
        rapport.probleme(f"Le site {adresse} ne répond pas comme prévu : {erreur}")
        return
    except Exception as erreur:  # noqa: BLE001
        rapport.probleme(f"Le site {adresse} ne répond pas : {erreur}")
        return
    manifeste = json.loads(verifier_deploiement.lire(f"{racine}/manifeste.json", delai=60)[2])
    verifier_fraicheur(rapport, manifeste, aujourd_hui)


def verifier_fraicheur(rapport: Rapport, manifeste: dict, aujourd_hui: dt.date) -> None:
    fin = _date((manifeste.get("periode") or {}).get("fin", "1970-01-01"))
    genere = (manifeste.get("genere_le") or "?")[:10]
    if aujourd_hui - fin > FRAICHEUR_MAX:
        rapport.probleme(
            f"Les données en ligne s'arrêtent au {fin:%d/%m/%Y} (site construit le {genere}) : "
            "une mise à jour d'Etalab s'est perdue. Voir l'onglet Actions (« Mise à jour des "
            "données ») et les pull requests ouvertes."
        )
    else:
        rapport.ok(f"Site en ligne ; ventes jusqu'au {fin:%d/%m/%Y}, site construit le {genere}.")


def verifier_fonds(rapport: Rapport, fonds: dict[str, dict]) -> None:
    for nom, fond in fonds.items():
        adresse = fond["url"].format(**TUILE_TEST)
        try:
            statut, entetes, corps = _lire(adresse)
            if statut != 200 or not entetes.get("content-type", "").startswith("image/") or not corps:
                raise ValueError(f"HTTP {statut}, {entetes.get('content-type')}")
        except Exception as erreur:  # noqa: BLE001
            rapport.probleme(
                f"Le fond de carte « {nom} » ne sert plus de tuiles ({erreur}) : son adresse a "
                "peut-être changé (`FONDS_DE_CARTE`, `src/charge.py`). La carte reste lisible "
                "sans lui."
            )
        else:
            rapport.ok(f"Fond de carte « {nom} » : tuiles servies.")


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


def verifier_jeton(rapport: Rapport, jeton: str, depot: str, aujourd_hui: dt.date) -> None:
    conseil = (
        "Créer un jeton à droits fins (Settings → Developer settings → Fine-grained tokens) "
        f"sur le seul dépôt {depot}, droits *Contents*, *Pull requests* et *Workflows* en "
        "écriture, avec la plus longue durée proposée, puis le coller dans le secret "
        "`JETON_MISE_A_JOUR` du dépôt."
    )
    if not jeton:
        rapport.probleme(
            "Le secret `JETON_MISE_A_JOUR` est absent : les mises à jour de Dependabot qui "
            f"touchent aux workflows ne peuvent pas être fusionnées. {conseil}"
        )
        return
    try:
        _, entetes, _ = Api(jeton, depot).appeler("GET", f"/repos/{depot}")
    except ErreurApi as erreur:
        rapport.probleme(f"Le jeton `JETON_MISE_A_JOUR` est refusé ({erreur}) : expiré ou révoqué. {conseil}")
        return
    expiration = entetes.get("github-authentication-token-expiration")
    if not expiration:
        rapport.ok("Jeton `JETON_MISE_A_JOUR` valide, sans date d'expiration.")
        return
    fin = _date(expiration)
    if fin - aujourd_hui <= PREAVIS_JETON:
        rapport.probleme(f"Le jeton `JETON_MISE_A_JOUR` expire le {fin:%d/%m/%Y}. {conseil}")
    else:
        rapport.ok(f"Jeton `JETON_MISE_A_JOUR` valide jusqu'au {fin:%d/%m/%Y}.")


def verifier_pull_requests(rapport: Rapport, api: Api, maintenant: dt.datetime) -> None:
    anciennes = [
        pr for pr in api.get(api.chemin("/pulls?state=open&per_page=100")) or []
        if maintenant - dt.datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00")) > PR_EN_ATTENTE_MAX
    ]
    for pr in anciennes:
        rapport.probleme(
            f"La pull request [#{pr['number']} « {pr['title']} »]({pr['html_url']}) attend depuis "
            f"le {pr['created_at'][:10]} : CI rouge, conflit, étiquette `a-verifier`, ou "
            "contribution à relire."
        )
    if not anciennes:
        rapport.ok("Aucune pull request en souffrance.")


def verifier_ci(rapport: Rapport, api: Api) -> None:
    base = api.get(api.chemin())["default_branch"]
    runs = api.get(api.chemin(f"/actions/workflows/ci.yml/runs?per_page=10&branch={base}")) or {}
    runs = [r for r in runs.get("workflow_runs") or [] if r.get("event") in ("push", "workflow_dispatch")]
    termines = [r for r in runs if r.get("status") == "completed"]
    if termines and termines[0].get("conclusion") != "success":
        rapport.probleme(
            f"La dernière CI de `{base}` a échoué ({termines[0]['html_url']}) : le site en ligne "
            "n'est plus mis à jour, et plus rien n'est fusionné automatiquement."
        )
    else:
        rapport.ok(f"CI de `{base}` verte.")


def reactiver_workflows(rapport: Rapport, api: Api) -> None:
    """Réactive les workflows coupés pour inactivité, et entretient les autres.

    Réactiver un workflow actif ne change rien, sinon qu'il repousse l'échéance
    des 60 jours. Un workflow coupé à la main le reste.
    """

    reactives = []
    for workflow in (api.get(api.chemin("/actions/workflows?per_page=100")) or {}).get("workflows", []):
        if not workflow.get("path", "").startswith(".github/workflows/"):
            continue
        if workflow.get("state") not in ("active", "disabled_inactivity"):
            continue
        try:
            api.appeler("PUT", api.chemin(f"/actions/workflows/{workflow['id']}/enable"))
        except ErreurApi as erreur:
            rapport.probleme(f"Le workflow « {workflow['name']} » n'a pas pu être entretenu : {erreur}")
            continue
        if workflow["state"] == "disabled_inactivity":
            reactives.append(workflow["name"])
    rapport.ok(
        "Workflows actifs" + (f" (réactivés : {', '.join(reactives)})." if reactives else ".")
    )


# --------------------------------------------------------------------------
# Version de Python
# --------------------------------------------------------------------------


def python_cible(cycles: dict[str, dict], actuelle: str, aujourd_hui: dt.date) -> str | None:
    """La version à adopter, ou None si `actuelle` a encore plus d'un an de vie.

    La cible est la plus récente des versions parues depuis plus d'un an : les
    bibliothèques (pandas, pyarrow) ont eu le temps de la suivre.
    """

    def cle(version: str) -> tuple[int, ...]:
        return tuple(int(n) for n in version.split("."))

    info = cycles.get(actuelle)
    if info and info.get("end_of_life") and _date(info["end_of_life"]) - aujourd_hui > PREAVIS_PYTHON:
        return None
    murs = [
        version for version, c in cycles.items()
        if c.get("first_release") and c.get("status") in ("bugfix", "security")
        and aujourd_hui - _date(c["first_release"]) >= RECUL_PYTHON
    ]
    cible = max(murs, key=cle, default=None)
    return cible if cible and cle(cible) > cle(actuelle) else None


def version_maplibre(provenance: Path = PROVENANCE_MAPLIBRE) -> str:
    """La version copiée dans le dépôt, lue dans `PROVENANCE.txt`."""

    premiere = provenance.read_text(encoding="utf-8").splitlines()[0]
    return premiere.split("MapLibre GL JS", 1)[1].split()[0]


def verifier_maplibre(rapport: Rapport, lire=_json, version: str | None = None) -> None:
    """Failles connues de la version copiée (OSV), et version la plus récente."""

    version = version or version_maplibre()
    try:
        reponse = lire(URL_OSV, {"package": {"name": "maplibre-gl", "ecosystem": "npm"},
                                 "version": version})
    except Exception as erreur:  # noqa: BLE001 - la veille suivante reessaiera
        rapport.ok(f"MapLibre GL JS {version} (base de failles OSV injoignable : {erreur}).")
        return
    failles = [f.get("id", "?") for f in (reponse or {}).get("vulns") or []]
    if failles:
        rapport.probleme(
            f"MapLibre GL JS {version}, copié dans `site/carte/vendor/`, a des failles connues "
            f"({', '.join(failles[:5])}) : le remplacer par une version corrigée "
            "(voir `PROVENANCE.txt`), tests à l'appui."
        )
        return
    try:
        recente = lire(URL_MAPLIBRE).get("version", "")
    except Exception:  # noqa: BLE001
        recente = ""
    if recente and recente != version:
        rapport.ok(f"MapLibre GL JS {version} : aucune faille connue (version {recente} parue).")
    else:
        rapport.ok(f"MapLibre GL JS {version} : aucune faille connue.")


def verifier_taille_depot(rapport: Rapport, api: Api) -> None:
    """La taille du dépôt (champ `size` de l'API, en kio)."""

    try:
        taille = int(api.get(api.chemin()).get("size") or 0) * 1024
    except (ErreurApi, ValueError, AttributeError) as erreur:
        rapport.ok(f"Taille du dépôt inconnue ({erreur}).")
        return
    lisible = f"{taille / 2**20:.0f} Mio"
    if taille > TAILLE_DEPOT_MAX:
        rapport.probleme(
            f"Le dépôt pèse {lisible} : chaque millésime y ajoute ses données. Il est temps "
            "de les sortir de l'historique (Git LFS, ou fichiers attachés aux releases)."
        )
    else:
        rapport.ok(f"Dépôt de {lisible}.")


def verifier_python(rapport: Rapport, aujourd_hui: dt.date) -> str | None:
    actuelle = (RACINE / ".python-version").read_text(encoding="utf-8").strip()
    try:
        cycles = json.loads(_lire(URL_CYCLES_PYTHON)[2])
    except Exception as erreur:  # noqa: BLE001 - la veille suivante reessaiera
        rapport.ok(f"Python {actuelle} (calendrier des versions illisible : {erreur}).")
        return None
    cible = python_cible(cycles, actuelle, aujourd_hui)
    if cible:
        rapport.ok(f"Python {actuelle} approche de sa fin de vie : passage à {cible} proposé.")
    else:
        rapport.ok(f"Python {actuelle} encore maintenu.")
    return cible


def main(arguments: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parseur.add_argument("--site", default=os.environ.get("ADRESSE_SITE") or ADRESSE_SITE)
    options = parseur.parse_args(arguments)

    from src.charge import FONDS_DE_CARTE

    maintenant = dt.datetime.now(dt.timezone.utc)
    aujourd_hui = maintenant.date()
    api = Api()
    rapport = Rapport()
    verifier_site(rapport, options.site, aujourd_hui)
    verifier_fonds(rapport, FONDS_DE_CARTE)
    verifier_jeton(rapport, os.environ.get("JETON_MISE_A_JOUR", ""), api.depot, aujourd_hui)
    verifier_pull_requests(rapport, api, maintenant)
    verifier_ci(rapport, api)
    reactiver_workflows(rapport, api)
    verifier_maplibre(rapport)
    verifier_taille_depot(rapport, api)
    cible = verifier_python(rapport, aujourd_hui)

    print(rapport.markdown(aujourd_hui))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as flux:
            flux.write(f"probleme={'oui' if rapport.problemes else 'non'}\n")
            flux.write(f"python={cible or ''}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
