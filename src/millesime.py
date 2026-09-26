"""Quel millesime des DVF geolocalisees Etalab publie-t-il, et a-t-il change ?

Etalab republie les DVF geolocalisees deux fois par an (« semiannual » selon
data.gouv.fr), dans un dossier nomme par la date d'arret des donnees
(`https://files.data.gouv.fr/geo-dvf/2025-12/`), recopie dans `latest/`. Il
arrive aussi qu'un meme millesime soit revise : les fichiers changent sans que
le dossier change de nom.

Ce module lit les **pages d'index** du serveur, les seules a dire les deux
choses : le nom des dossiers de millesime, et pour chaque fichier
departement-annee sa taille et sa date. (Une requete HEAD sur un dossier rend
404 ; sur un fichier, elle redirige vers le stockage, et ne dit rien du
millesime.) L'empreinte ainsi relevee est versionnee dans
`data/territoires/<cle>/source.json` : le workflow de mise a jour la compare a
celle du jour, et ne fait rien si rien n'a bouge.

La **fenetre d'annees** se deduit de ce qui est publie, sans rien ecrire en
dur : les cinq dernieres annees completes, plus l'annee en cours si elle est
publiee. Une annee est complete si la couverture des donnees va jusqu'a son
31 decembre ; la fin de couverture est lue dans les metadonnees du jeu sur
data.gouv.fr, et a defaut deduite du nom du dossier (`2025-12` : jusqu'au
31 decembre 2025).
"""

from __future__ import annotations

import calendar
import datetime as dt
import html
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

URL_GEO_DVF = "https://files.data.gouv.fr/geo-dvf/"
URL_LATEST = URL_GEO_DVF + "latest/csv/"
URL_JEU = "https://www.data.gouv.fr/api/1/datasets/demandes-de-valeurs-foncieres-geolocalisees/"

#: Nombre d'annees completes gardees dans la fenetre.
ANNEES_COMPLETES = 5

DELAI = 60
ENTETES = {"User-Agent": "cartographie-dvf/1.0 (mise a jour des donnees)"}

_LIGNE = re.compile(
    r'<a href="(?P<href>[^"]+)">(?P<nom>[^<]+)</a></td>\s*<td>(?P<taille>[^<]*)</td>\s*<td>(?P<date>[^<]*)</td>',
    re.S,
)
_MILLESIME = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")


class MillesimeIllisible(RuntimeError):
    """Le serveur d'Etalab ne dit pas ce qu'il publie : mieux vaut s'arreter."""


@dataclass
class Entree:
    nom: str
    taille: int | None
    date: str | None


def lire_index(page: str) -> list[Entree]:
    """Entrees d'une page d'index de files.data.gouv.fr (dossiers et fichiers)."""

    entrees = []
    for m in _LIGNE.finditer(page):
        nom = html.unescape(m.group("nom")).strip()
        taille = m.group("taille").strip()
        entrees.append(Entree(
            nom=nom.rstrip("/"),
            taille=int(taille) if taille.isdigit() else None,
            date=m.group("date").strip() or None,
        ))
    return entrees


def fin_du_dossier(nom: str) -> dt.date | None:
    """`2025-12` -> 31/12/2025 ; `2025-06-30` -> 30/06/2025."""

    m = _MILLESIME.match(nom)
    if not m:
        return None
    annee, mois = int(m.group(1)), int(m.group(2))
    jour = int(m.group(3)) if m.group(3) else calendar.monthrange(annee, mois)[1]
    return dt.date(annee, mois, jour)


def fenetre(annees_publiees: list[int], fin_couverture: dt.date, nombre: int = ANNEES_COMPLETES) -> list[int]:
    """Les `nombre` dernieres annees completes, plus l'annee partielle publiee."""

    completes = sorted(a for a in annees_publiees if dt.date(a, 12, 31) <= fin_couverture)
    partielles = sorted(
        a for a in annees_publiees
        if dt.date(a, 12, 31) > fin_couverture and a == fin_couverture.year
    )
    return completes[-nombre:] + partielles


@dataclass
class EtatDistant:
    """Ce qu'Etalab publie aujourd'hui pour les departements d'un territoire."""

    millesime: str
    fin_couverture: str
    annees_publiees: list[int]
    annees: list[int]
    fichiers: dict[str, dict] = field(default_factory=dict)

    def empreinte(self) -> dict:
        return {"millesime": self.millesime, "annees": self.annees, "fichiers": self.fichiers}


def _lire_http(adresse: str) -> str:
    import requests

    reponse = requests.get(adresse, timeout=DELAI, headers=ENTETES)
    reponse.raise_for_status()
    return reponse.text


def _lire_json_http(adresse: str) -> dict:
    import requests

    reponse = requests.get(adresse, timeout=DELAI, headers=ENTETES)
    reponse.raise_for_status()
    return reponse.json()


def interroger(
    departements: list[str],
    lire: Callable[[str], str] | None = None,
    lire_json: Callable[[str], dict] | None = None,
) -> EtatDistant:
    """Releve le millesime publie, sa couverture, et chaque fichier utile.

    `lire` et `lire_json` rendent le texte et le JSON d'une adresse (par
    defaut `requests`) ; les tests les remplacent par des pages enregistrees.
    """

    lire = lire or _lire_http
    lire_json = lire_json or _lire_json_http

    dossiers = [e.nom for e in lire_index(lire(URL_GEO_DVF)) if fin_du_dossier(e.nom)]
    if not dossiers:
        raise MillesimeIllisible(f"aucun dossier de millesime dans {URL_GEO_DVF}")
    millesime = max(dossiers, key=fin_du_dossier)

    # La couverture declaree par data.gouv.fr fait foi ; le nom du dossier
    # n'est qu'un repli.
    fin = fin_du_dossier(millesime)
    try:
        couverture = (lire_json(URL_JEU).get("temporal_coverage") or {}).get("end")
        if couverture:
            fin = dt.date.fromisoformat(str(couverture)[:10])
    except Exception:  # noqa: BLE001 - l'API indisponible n'empeche pas de conclure
        pass

    annees_publiees = sorted(
        int(e.nom) for e in lire_index(lire(URL_LATEST)) if re.fullmatch(r"\d{4}", e.nom)
    )
    if not annees_publiees:
        raise MillesimeIllisible(f"aucune annee dans {URL_LATEST}")
    annees = fenetre(annees_publiees, fin)
    if not annees:
        raise MillesimeIllisible(f"aucune annee complete avant le {fin:%d/%m/%Y}")

    fichiers: dict[str, dict] = {}
    for annee in annees:
        entrees = {e.nom: e for e in lire_index(lire(f"{URL_LATEST}{annee}/departements/"))}
        for departement in departements:
            entree = entrees.get(f"{departement}.csv.gz")
            fichiers[f"{annee}/{departement}"] = (
                {"taille": entree.taille, "date": entree.date} if entree else {"absent": True}
            )
    return EtatDistant(
        millesime=millesime, fin_couverture=fin.isoformat(),
        annees_publiees=annees_publiees, annees=annees, fichiers=fichiers,
    )


def differences(etat: EtatDistant, source: dict | None) -> list[str]:
    """Ce qui a change depuis la preparation versionnee ; vide si rien."""

    if not source:
        return ["aucune empreinte versionnee (premiere verification)"]
    raisons = []
    if source.get("millesime") != etat.millesime:
        raisons.append(f"millesime {source.get('millesime')} -> {etat.millesime}")
    if list(source.get("annees") or []) != etat.annees:
        raisons.append(f"annees {source.get('annees')} -> {etat.annees}")
    anciens = source.get("fichiers") or {}
    revises = sorted(
        cle for cle, fichier in etat.fichiers.items()
        if cle in anciens and anciens[cle] != fichier
    )
    if revises:
        raisons.append(f"{len(revises)} fichier(s) revise(s) : " + ", ".join(revises[:8])
                       + (" ..." if len(revises) > 8 else ""))
    return raisons


def source_versionnee(etat: EtatDistant, aujourd_hui: dt.date | None = None) -> dict:
    """Contenu de `source.json`, a ecrire une fois la preparation reussie."""

    return {
        **asdict(etat),
        "verifie_le": (aujourd_hui or dt.date.today()).isoformat(),
        "source": URL_LATEST,
    }
