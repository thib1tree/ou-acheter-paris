#!/usr/bin/env python3
"""Verifie un site deploye et dit comment Cloudflare le sert.

    python scripts/verifier_deploiement.py https://<projet>.pages.dev

Lance par le workflow de deploiement juste apres `wrangler pages deploy`. Il
echoue si la page ou le manifeste ne repondent pas, ou si un fichier que le
manifeste designe manque. Il ecrit aussi, en Markdown, les en-tetes que
Cloudflare pose reellement (compression, cache) : ce sont des comportements
de l'hebergeur que la documentation ne garantit pas dans le detail, et qu'il
vaut mieux constater que supposer.

Aucune dependance : la bibliotheque standard suffit.
"""

from __future__ import annotations

import gzip
import json
import sys
import time
import urllib.error
import urllib.request

#: gzip seulement : la bibliotheque standard sait le decompresser, et la
#: colonne « Compression » du rapport dit si Cloudflare compresse le fichier.
ENTETES = {"User-Agent": "verification-deploiement/1.0", "Accept-Encoding": "gzip"}


def decompresser(entetes: dict[str, str], corps: bytes) -> bytes:
    """Le corps tel que le lirait le navigateur (`urllib` ne decompresse pas)."""

    encodage = entetes.get("content-encoding", "identity").strip().lower()
    if encodage in ("", "identity"):
        return corps
    if encodage == "gzip":
        return gzip.decompress(corps)
    raise SystemExit(f"Encodage inattendu : {encodage}")


#: Temps laisse a un deploiement pour repondre. Le tout premier deploiement
#: d'un projet Pages attend son certificat TLS : pendant quelques minutes,
#: la poignee de main echoue (« handshake failure ») alors que tout est en
#: ligne. Ensuite, le certificat couvre tous les deploiements suivants.
DELAI_DEPLOIEMENT = 300


def lire(adresse: str, delai: float = DELAI_DEPLOIEMENT) -> tuple[int, dict[str, str], bytes]:
    """GET, repete jusqu'a `delai` secondes tant que le site ne repond pas."""

    limite = time.monotonic() + delai
    attente = 5.0
    while True:
        try:
            requete = urllib.request.Request(adresse, headers=ENTETES)
            with urllib.request.urlopen(requete, timeout=30) as reponse:
                entetes = {k.lower(): v for k, v in reponse.headers.items()}
                return reponse.status, entetes, decompresser(entetes, reponse.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as erreur:
            if time.monotonic() + attente > limite:
                raise SystemExit(f"{adresse} ne repond pas apres {delai:.0f} s : {erreur}")
            print(f"{adresse} : {erreur} ; nouvel essai dans {attente:.0f} s", file=sys.stderr)
            time.sleep(attente)
            attente = min(attente * 2, 60.0)


#: En-tetes de securite que la page doit porter (voir `EN_TETES` dans
#: `construire_site.py`). Un `_headers` mal lu par Cloudflare ne casserait
#: rien de visible : c'est ici qu'on s'en apercoit.
ENTETES_PAGE = (
    "content-security-policy", "x-content-type-options", "x-frame-options", "referrer-policy",
)


def verifier_securite(page: dict[str, str], donnees: dict[str, str]) -> None:
    manquants = [nom for nom in ENTETES_PAGE if nom not in page]
    if manquants:
        raise SystemExit("En-têtes de sécurité absents de la page : " + ", ".join(manquants))
    if "script-src 'self'" not in page["content-security-policy"]:
        raise SystemExit("La politique de sécurité de la page autorise d'autres scripts.")
    # Conditions de reutilisation des DVF : pas d'indexation des donnees.
    if "noindex" not in donnees.get("x-robots-tag", ""):
        raise SystemExit("Les données ne portent pas « X-Robots-Tag: noindex ».")


def main(racine: str) -> int:
    racine = racine.rstrip("/")
    lignes = [f"### Site déployé : {racine}", "", "| Fichier | Statut | Type | Compression | Cache |",
              "|---|---|---|---|---|"]

    servis: dict[str, dict[str, str]] = {}

    def constater(chemin: str) -> bytes:
        statut, entetes, corps = lire(f"{racine}/{chemin}")
        servis[chemin] = entetes
        lignes.append(
            f"| `{chemin}` | {statut} | {entetes.get('content-type', '')} "
            f"| {entetes.get('content-encoding', 'aucune')} | {entetes.get('cache-control', '')} |"
        )
        return corps

    page = constater("")
    if b'id="zone-carte"' not in page:
        raise SystemExit("La page d'accueil n'est pas celle du site.")
    manifeste = json.loads(constater("manifeste.json"))
    if manifeste.get("version") != 1:
        raise SystemExit("Manifeste illisible.")
    fichiers = manifeste["fichiers"]
    for nom in ("sections", "defaut", "ventes"):
        constater(fichiers[nom])
    verifier_securite(servis[""], servis[fichiers["ventes"]])
    for nom in sorted(set(fichiers) - {"sections", "defaut", "ventes"}):
        statut, _, _ = lire(f"{racine}/{fichiers[nom]}")
        if statut != 200:
            raise SystemExit(f"{fichiers[nom]} : HTTP {statut}")

    periode = manifeste.get("periode") or {}
    lignes += ["", f"Données du {periode.get('debut')} au {periode.get('fin')}, "
               f"{manifeste.get('nb_ventes')} ventes, générées le {manifeste.get('genere_le')}."]
    print("\n".join(lignes))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
