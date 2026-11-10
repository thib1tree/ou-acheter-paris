"""Les chiffres d'une page par commune : « Prix de l'immobilier à … ».

La carte repond a « ou acheter ? » pour qui la manipule ; ces pages
repondent, en texte, a la question qu'on pose a un moteur de recherche :
« combien coute un logement a Montreuil ? ». Elles ne montrent que des
**statistiques** — jamais une vente — et c'est ce qui les rend indexables :
les conditions de reutilisation des DVF interdisent de laisser indexer les
ventes elles-memes (voir `EN_TETES` dans `scripts/construire_site.py`).

Deux fenetres de temps, et deux seulement, pour qu'on sache toujours de quoi
l'on parle :

* **les douze derniers mois publies** donnent le prix du moment, et son
  evolution sur un an (comparee aux douze mois d'avant) ;
* **toute la periode** (cinq ans) nourrit les tableaux de detail — par annee,
  par nombre de pieces, par quartier — ou douze mois ne suffiraient pas.

Tout ce module est fait de fonctions pures sur les ventes nettoyees : la mise
en page est ailleurs (`src/pages.py`).
"""

from __future__ import annotations

import math
import re
import unicodedata

import numpy as np
import pandas as pd

from src.ingestion import ETAT_ANCIEN, ETAT_NEUF
from src.stats import CLE_COMMUNE, CLE_SECTION, etats_des_ventes

#: Nombre de ventes en deca duquel aucune statistique n'est publiee. Plus
#: strict que le seuil de grisage de la carte (5) : une mediane sur une
#: poignee de ventes *est* le prix de l'une d'elles, et ces pages, a la
#: difference des donnees de la carte, sont lues par les moteurs de
#: recherche. Onze est le seuil d'usage du secret statistique.
SEUIL_PUBLICATION = 11

#: Nombre de ventes exige, sur chacune des deux annees comparees, pour
#: annoncer une evolution. En dessous, l'ecart entre deux medianes tient
#: surtout au hasard des biens vendus.
SEUIL_EVOLUTION = 30

#: Les deux marches d'un territoire, dans l'ordre ou les pages les presentent.
TYPES = ("Appartement", "Maison")

#: Dernier choix du nombre de pieces : « 5 et plus ».
PIECES_MAX = 5

#: Communes proches citees au bas d'une page.
NB_VOISINES = 8

DEPARTEMENTS = {
    "75": "Paris",
    "77": "Seine-et-Marne",
    "78": "Yvelines",
    "91": "Essonne",
    "92": "Hauts-de-Seine",
    "93": "Seine-Saint-Denis",
    "94": "Val-de-Marne",
    "95": "Val-d'Oise",
}

MOIS = (
    "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
    "septembre", "octobre", "novembre", "décembre",
)


# --------------------------------------------------------------------------
# Noms et adresses
# --------------------------------------------------------------------------


def nom_affiche(nom: str) -> str:
    """« Paris 11e Arrondissement » → « Paris 11e » ; les autres tels quels."""

    return re.sub(r"\s+Arrondissement$", "", str(nom).strip())


def identifiant(nom: str) -> str:
    """Le nom tel qu'il s'ecrit dans une adresse : « L'Haÿ-les-Roses » →
    `l-hay-les-roses`."""

    sans_accents = unicodedata.normalize("NFD", nom_affiche(nom))
    sans_accents = "".join(c for c in sans_accents if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", sans_accents.lower()).strip("-")


def identifiants(noms: dict[str, str]) -> dict[str, str]:
    """Un identifiant par commune (`code -> identifiant`), tous distincts.

    Deux communes homonymes prendraient le numero de leur departement ; il
    n'y en a pas dans l'agglomeration, mais une adresse ne doit jamais
    designer deux pages.
    """

    par_identifiant: dict[str, list[str]] = {}
    for code, nom in noms.items():
        par_identifiant.setdefault(identifiant(nom), []).append(code)
    sortie = {}
    for cle, codes in par_identifiant.items():
        for code in codes:
            sortie[code] = cle if len(codes) == 1 else f"{cle}-{code[:2]}"
    if len(set(sortie.values())) != len(sortie) or not all(sortie.values()):
        raise ValueError("identifiants de communes en double ou vides")
    return sortie


def a_commune(nom: str) -> str:
    """« à Montreuil », « au Blanc-Mesnil », « aux Lilas », « à La Courneuve »."""

    nom = nom_affiche(nom)
    if nom.startswith("Le "):
        return "au " + nom[3:]
    if nom.startswith("Les "):
        return "aux " + nom[4:]
    return "à " + nom


def departement(code_commune: str) -> tuple[str, str]:
    """Numero et nom du departement d'une commune."""

    numero = str(code_commune)[:2]
    return numero, DEPARTEMENTS.get(numero, "")


def mois_fr(jour: pd.Timestamp) -> str:
    return f"{MOIS[jour.month - 1]} {jour.year}"


# --------------------------------------------------------------------------
# Fenetres de temps
# --------------------------------------------------------------------------


def fenetres(fin: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Debut des douze derniers mois publies, et debut des douze d'avant.

    `fin` est la derniere date de vente du territoire : les fenetres sont les
    memes pour toutes les communes, et deux pages se comparent.
    """

    lendemain = pd.Timestamp(fin).normalize() + pd.Timedelta(days=1)
    return lendemain - pd.DateOffset(months=12), lendemain - pd.DateOffset(months=24)


# --------------------------------------------------------------------------
# Statistiques
# --------------------------------------------------------------------------


def _arrondi(valeur: float) -> int | None:
    return None if valeur is None or math.isnan(valeur) else int(np.round(valeur))


def resume(ventes: pd.DataFrame, seuil: int = SEUIL_PUBLICATION) -> dict:
    """Effectif d'un lot de ventes et, s'il atteint le seuil, ses medianes.

    L'effectif est toujours rendu : dire « 4 ventes, trop peu pour une
    mediane » vaut mieux qu'un blanc. Les medianes, elles, valent `None` sous
    le seuil.
    """

    n = int(len(ventes))
    if n < seuil:
        return {"n": n, "m2": None, "prix": None, "surface": None}
    return {
        "n": n,
        "m2": _arrondi(float(ventes["prix_m2"].median())),
        "prix": _arrondi(float(ventes["valeur_fonciere"].median())),
        "surface": _arrondi(float(ventes["surface_bati"].median())),
    }


def evolution(recent: pd.DataFrame, precedent: pd.DataFrame) -> float | None:
    """Evolution sur un an du prix median au m², en pourcentage.

    `None` si l'une des deux annees compte moins de `SEUIL_EVOLUTION` ventes.
    C'est l'evolution du prix des biens *vendus* : elle bouge aussi quand la
    taille ou l'emplacement des biens vendus change.
    """

    if len(recent) < SEUIL_EVOLUTION or len(precedent) < SEUIL_EVOLUTION:
        return None
    avant = float(precedent["prix_m2"].median())
    apres = float(recent["prix_m2"].median())
    if not avant > 0:
        return None
    return round((apres / avant - 1) * 100, 1)


def classes_pieces(ventes: pd.DataFrame) -> pd.Series:
    """Nombre de pieces de chaque vente, de 1 a `PIECES_MAX` (« et plus ») ;
    0 quand les DVF ne le disent pas."""

    if "nb_pieces" not in ventes.columns:
        return pd.Series(0, index=ventes.index, dtype="int64")
    nombre = pd.to_numeric(ventes["nb_pieces"], errors="coerce")
    nombre = pd.Series(nombre.to_numpy(dtype="float64", na_value=np.nan), index=ventes.index)
    return nombre.fillna(0).clip(lower=0, upper=PIECES_MAX).astype("int64")


def fiche(ventes: pd.DataFrame, fin: pd.Timestamp, voies: dict[str, str] | None = None) -> dict:
    """Tous les chiffres de la page d'une commune.

    `ventes` : les ventes nettoyees de la commune, sur toute la periode.
    `fin` : derniere date de vente du territoire (voir `fenetres`).
    `voies` : voies dominantes de chaque section (`charge.reperes_voies`).
    """

    debut_recent, debut_precedent = fenetres(fin)
    dates = pd.to_datetime(ventes["date_mutation"])
    recentes = ventes[dates >= debut_recent]
    precedentes = ventes[(dates >= debut_precedent) & (dates < debut_recent)]

    types = {}
    for nom in TYPES:
        du_type = ventes["type_bien"] == nom
        types[nom] = {
            "recent": resume(recentes[recentes["type_bien"] == nom]),
            "periode": resume(ventes[du_type]),
            "evolution": evolution(
                recentes[recentes["type_bien"] == nom],
                precedentes[precedentes["type_bien"] == nom],
            ),
        }

    annees = []
    for annee in sorted(int(a) for a in ventes["annee"].dropna().unique()):
        de_l_annee = ventes[ventes["annee"] == annee]
        annees.append({
            "annee": annee,
            **{nom: resume(de_l_annee[de_l_annee["type_bien"] == nom]) for nom in TYPES},
        })

    # Par nombre de pieces : les appartements seuls. Une maison de quatre
    # pieces et un appartement de quatre pieces ne sont pas le meme marche.
    appartements = ventes[ventes["type_bien"] == "Appartement"]
    classes = classes_pieces(appartements)
    pieces = [
        {"pieces": classe, **resume(appartements[classes == classe])}
        for classe in range(1, PIECES_MAX + 1)
    ]

    etats = etats_des_ventes(appartements)
    neuf = {
        "neuf": resume(appartements[etats == ETAT_NEUF]),
        "ancien": resume(appartements[etats == ETAT_ANCIEN]),
    }

    quartiers = []
    for code, de_la_section in ventes.groupby(CLE_SECTION, observed=True):
        chiffres = resume(de_la_section)
        if chiffres["m2"] is None:
            continue
        quartiers.append({
            "code": str(code),
            "section": str(de_la_section["section_courte"].iloc[0]),
            "voies": (voies or {}).get(str(code), ""),
            "n": chiffres["n"],
            "m2": chiffres["m2"],
        })
    # Du plus cher au moins cher, le code pour departager : l'ordre ne doit
    # pas changer d'une construction a l'autre.
    quartiers.sort(key=lambda q: (-q["m2"], q["code"]))

    return {
        "n": int(len(ventes)),
        "types": types,
        "annees": annees,
        "pieces": pieces,
        "neuf": neuf,
        "quartiers": quartiers,
        "sections": int(ventes[CLE_SECTION].nunique()),
    }


def fiches(transactions: pd.DataFrame, voies: dict[str, str] | None = None) -> dict[str, dict]:
    """La fiche de chaque commune du territoire (`code -> fiche`)."""

    fin = pd.to_datetime(transactions["date_mutation"]).max()
    return {
        str(code): fiche(ventes, fin, voies)
        for code, ventes in transactions.groupby(CLE_COMMUNE, observed=True)
    }


# --------------------------------------------------------------------------
# Voisinage
# --------------------------------------------------------------------------


def communes_proches(ancres: dict[str, tuple[float, float, float]], nombre: int = NB_VOISINES) -> dict[str, list[str]]:
    """Les communes les plus proches de chacune, de la plus proche a la moins.

    `ancres` : point representatif de chaque commune (`geo.points_representatifs`).
    La distance est prise a vol d'oiseau entre ces points : assez pour dire
    « autour d'ici », sans rien affirmer d'une frontiere commune.
    """

    codes = sorted(ancres)
    if len(codes) < 2:
        return {code: [] for code in codes}
    lon = np.array([ancres[c][0] for c in codes])
    lat = np.array([ancres[c][1] for c in codes])
    sortie = {}
    for i, code in enumerate(codes):
        x = (lon - lon[i]) * math.cos(math.radians(lat[i]))
        y = lat - lat[i]
        distance = x * x + y * y
        distance[i] = np.inf
        ordre = np.argsort(distance, kind="stable")[:nombre]
        sortie[code] = [codes[j] for j in ordre]
    return sortie
