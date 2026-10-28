"""Quel millesime des DVF geolocalisees Etalab publie-t-il, et a-t-il change ?

Etalab republie les DVF geolocalisees deux fois par an (« semiannual » selon
data.gouv.fr). Depuis la livraison d'octobre 2026, ce sont des ressources du
jeu de donnees, **une par annee, France entiere** :

    full-2021-s2.csv.gz, full-2022.csv.gz, ..., full-2026-s1.csv.gz

hebergees sur `static.data.gouv.fr` dans un dossier horodate qui change a
chaque depot. Une livraison d'avril couvre cinq annees civiles ; une livraison
d'octobre, cinq ans glissants : le second semestre de la plus ancienne annee
(`-s2`), quatre annees pleines, le premier semestre de l'annee en cours
(`-s1`). (L'ancienne arborescence par departement, `files.data.gouv.fr/geo-dvf/`,
est figee sur la livraison d'avril 2026 et doit disparaitre fin 2026.)

Ce module lit la **liste des ressources** du jeu sur l'API de data.gouv.fr :
leur nom dit l'annee et le semestre couverts, leur adresse, taille et
empreinte SHA-1 disent si un fichier a ete republie. L'empreinte ainsi relevee
est versionnee dans `data/territoires/<cle>/source.json` : le workflow de mise
a jour la compare a celle du jour, et ne fait rien si rien n'a bouge.

La **fin de couverture** se deduit du nom du fichier le plus recent
(`full-2026-s1` : jusqu'au 30 juin 2026). La couverture que declarent les
metadonnees du jeu (`temporal_coverage`) n'est pas mise a jour avec les
fichiers : elle n'est pas lue. Le **millesime** est le mois de cette fin
(`2026-06`), comme l'etaient les dossiers de l'ancienne arborescence.

La **fenetre d'annees** se deduit de ce qui est publie, sans rien ecrire en
dur : les annees touchees par les cinq ans qui precedent la fin de
couverture. Une annee peut donc n'y figurer que pour un semestre.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

URL_JEU = "https://www.data.gouv.fr/api/1/datasets/demandes-de-valeurs-foncieres-geolocalisees/"
URL_PAGE_JEU = "https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees"

#: Duree de la fenetre glissante, en annees.
ANNEES_GLISSANTES = 5

DELAI = 60
ENTETES = {"User-Agent": "cartographie-dvf/1.0 (mise a jour des donnees)"}

_FICHIER = re.compile(r"/full-(?P<annee>\d{4})(?:-(?P<semestre>s[12]))?\.csv\.gz$")


class MillesimeIllisible(RuntimeError):
    """data.gouv.fr ne dit pas ce qu'il publie : mieux vaut s'arreter."""


@dataclass(frozen=True)
class Periode:
    """Ce que couvre un fichier : une annee, ou l'un de ses semestres."""

    annee: int
    semestre: str | None = None  # "s1", "s2" ou None (annee entiere)

    @property
    def cle(self) -> str:
        return f"{self.annee}-{self.semestre}" if self.semestre else str(self.annee)

    @property
    def debut(self) -> dt.date:
        return dt.date(self.annee, 7 if self.semestre == "s2" else 1, 1)

    @property
    def fin(self) -> dt.date:
        return dt.date(self.annee, 6, 30) if self.semestre == "s1" else dt.date(self.annee, 12, 31)

    @classmethod
    def depuis_adresse(cls, adresse: str) -> "Periode | None":
        m = _FICHIER.search(adresse or "")
        return cls(int(m.group("annee")), m.group("semestre")) if m else None

    @classmethod
    def depuis_cle(cls, cle: str) -> "Periode":
        annee, _, semestre = cle.partition("-")
        return cls(int(annee), semestre or None)


def fenetre(periodes: list[Periode], fin_couverture: dt.date, duree: int = ANNEES_GLISSANTES) -> list[Periode]:
    """Les periodes publiees qui touchent les `duree` ans avant `fin_couverture`.

    Une meme annee publiee deux fois (un semestre et l'annee entiere, le temps
    d'une transition) n'est gardee qu'une fois, sous sa forme la plus large.
    """

    # Le premier jour du mois qui suit la fin de couverture, `duree` ans plus tot.
    mois = fin_couverture.month % 12 + 1
    debut = dt.date(fin_couverture.year - duree + (mois == 1), mois, 1)
    retenues: dict[int, Periode] = {}
    for periode in periodes:
        if periode.fin < debut or periode.debut > fin_couverture:
            continue
        deja = retenues.get(periode.annee)
        if deja is None or (periode.fin - periode.debut) > (deja.fin - deja.debut):
            retenues[periode.annee] = periode
    return [retenues[a] for a in sorted(retenues)]


@dataclass
class EtatDistant:
    """Ce qu'Etalab publie aujourd'hui."""

    millesime: str
    fin_couverture: str
    annees_publiees: list[int]
    annees: list[int]
    #: Par periode retenue (`2026-s1`, `2025`…) : adresse, taille, sha1, date.
    fichiers: dict[str, dict] = field(default_factory=dict)

    def empreinte(self) -> dict:
        return {"millesime": self.millesime, "annees": self.annees, "fichiers": self.fichiers}


def _lire_json_http(adresse: str, essais: int = 4) -> dict:
    import time

    import requests

    for essai in range(1, essais + 1):
        try:
            reponse = requests.get(adresse, timeout=DELAI, headers=ENTETES)
            reponse.raise_for_status()
            return reponse.json()
        except requests.ConnectionError:
            # Une connexion coupee n'est pas une reponse : on redemande.
            if essai == essais:
                raise
            time.sleep(2 ** essai)
    raise AssertionError("inatteignable")


def _fichier(ressource: dict) -> dict:
    somme = ressource.get("checksum") or {}
    return {
        "url": ressource.get("url"),
        "taille": ressource.get("filesize"),
        "sha1": somme.get("value") if somme.get("type") == "sha1" else None,
        "date": ressource.get("last_modified"),
    }


def interroger(lire_json: Callable[[str], dict] | None = None) -> EtatDistant:
    """Releve le millesime publie, sa couverture, et chaque fichier utile.

    `lire_json` rend le JSON d'une adresse (par defaut `requests`) ; les tests
    le remplacent par une reponse enregistree.
    """

    lire_json = lire_json or _lire_json_http
    jeu = lire_json(URL_JEU)

    publies: dict[Periode, dict] = {}
    for ressource in jeu.get("resources") or []:
        if ressource.get("type", "main") != "main":
            continue
        periode = Periode.depuis_adresse(str(ressource.get("url", "")))
        if periode is not None:
            publies[periode] = ressource
    if not publies:
        raise MillesimeIllisible(f"aucun fichier full-<annee>.csv.gz dans {URL_JEU}")

    fin = max(p.fin for p in publies)
    retenues = fenetre(list(publies), fin)
    return EtatDistant(
        millesime=f"{fin:%Y-%m}",
        fin_couverture=fin.isoformat(),
        annees_publiees=sorted({p.annee for p in publies}),
        annees=[p.annee for p in retenues],
        fichiers={p.cle: _fichier(publies[p]) for p in retenues},
    )


def fichiers_des_annees(etat: EtatDistant, annees: list[int]) -> dict[str, dict]:
    """Les fichiers de l'etat qui couvrent les annees demandees."""

    voulues = set(annees)
    return {
        cle: fichier for cle, fichier in etat.fichiers.items()
        if Periode.depuis_cle(cle).annee in voulues
    }


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
    if set(anciens) != set(etat.fichiers):
        raisons.append(f"fichiers : {len(anciens)} -> {len(etat.fichiers)} "
                       f"({', '.join(sorted(etat.fichiers))})")
    revises = sorted(
        cle for cle, fichier in etat.fichiers.items()
        if cle in anciens and anciens[cle] != fichier
    )
    if revises:
        raisons.append(f"{len(revises)} fichier(s) revise(s) : " + ", ".join(revises))
    return raisons


def source_versionnee(etat: EtatDistant, aujourd_hui: dt.date | None = None) -> dict:
    """Contenu de `source.json`, a ecrire une fois la preparation reussie."""

    return {
        **asdict(etat),
        "verifie_le": (aujourd_hui or dt.date.today()).isoformat(),
        "source": URL_PAGE_JEU,
    }
