"""Geometries : sections cadastrales (Etalab) et gares (OSM, SNCF, IDFM).

Trois regles gouvernent ce module :

1. **Le local prime.** Les contours deja presents dans `data/geo/` sont utilises
   tels quels ; rien n'est telecharge inutilement.
2. **Le reseau n'est jamais bloquant.** Toute requete HTTP est encapsulee dans
   un `try/except` : hors ligne, l'application demarre et fonctionne avec ce
   qu'elle a sous la main, en le signalant.
3. **Plusieurs sources valent mieux qu'une.** Les serveurs publics imposent des
   quotas (Overpass repond frequemment `429`) : chaque donnee a des sources de
   repli, essayees dans l'ordre jusqu'a la premiere qui repond **completement**.
   Une reponse tronquee compte pour un echec : memorisee, elle ne serait jamais
   reprise, et le depot livrerait un reseau a trous.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

try:  # `requests` reste optionnel : sans lui, on travaille en local uniquement.
    import requests
except ImportError:  # pragma: no cover - depend de l'environnement
    requests = None  # type: ignore[assignment]

DELAI_RESEAU = 20  # secondes

#: Sections cadastrales, millesime courant (fichiers statiques Etalab).
URL_SECTIONS_ETALAB = (
    "https://cadastre.data.gouv.fr/data/etalab-cadastre/latest/geojson/communes/"
    "{departement}/{insee}/cadastre-{insee}-sections.json.gz"
)
#: API cadastre (meme socle Etalab), utilisee en second recours.
URL_SECTIONS_API = "https://apicarto.ign.fr/api/cadastre/division?code_insee={insee}"

#: Miroirs Overpass (le premier qui repond gagne). Ces serveurs sont publics et
#: appliquent des quotas : un `429 Too Many Requests` est frequent aux heures
#: chargees, d'ou les sources de repli ci-dessous.
URLS_OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

#: Referentiel Île-de-France Mobilites (Opendatasoft, API v1). C'est le seul
#: qui soit a la fois exhaustif sur les huit departements franciliens — metro,
#: RER, Transilien, tramway, VAL, funiculaire, une ligne par enregistrement —
#: et le seul a porter les arrets **en projet**, donc le Grand Paris Express.
URL_IDFM = "https://data.iledefrance-mobilites.fr/api/records/1.0/search/"
JEU_IDFM_GARES = "emplacement-des-gares-idf"
JEU_IDFM_PROJETS = "projets_arrets_idf"

#: Emprise du referentiel IDFM. Hors d'Île-de-France il ne repond que le vide,
#: ou la poignee de gares d'un departement limitrophe : interroge pour une
#: autre region, il livrerait un reseau tronque en se faisant passer pour
#: complet. On ne l'interroge donc que pour un territoire francilien.
EMPRISE_IDF = (48.10, 1.40, 49.30, 3.60)

#: Repli national : le referentiel des gares SNCF (train seul, ni metro ni
#: tramway). L'API v1 est utilisee a dessein — elle expose la geometrie sous
#: une cle `geometry` normalisee, alors que le nom du champ geographique varie
#: d'un jeu de donnees a l'autre.
JEUX_OPENDATASOFT = (
    (
        "SNCF Open Data",
        "https://data.sncf.com/api/records/1.0/search/",
        "liste-des-gares",
    ),
)

#: Taille d'une page Opendatasoft. L'API v1 plafonne `rows` et, surtout, elle
#: ne signale pas la troncature autrement que par `nhits`, qu'il faut donc lire.
LOT_OPENDATASOFT = 1000
#: Plafond `start + rows` de l'API v1 : au-dela, la pagination s'arrete et le
#: resultat serait muet sur ce qui manque.
PLAFOND_OPENDATASOFT = 10_000

#: Les serveurs publics limitent fortement l'agent par defaut de `requests`.
ENTETES_HTTP = {
    "User-Agent": "prix-immobiliers-grand-paris/1.0 (cartographie DVF, usage non commercial)"
}

#: Statut d'une gare. `service` roule aujourd'hui ; les deux autres sont a
#: venir — `travaux` pour un chantier ouvert (le Grand Paris Express, le
#: prolongement d'Eole), `projet` pour une operation encore a l'etude ou a
#: l'enquete publique. Un acheteur ne lit pas les deux de la meme façon : le
#: premier a une date, le second a une probabilite.
STATUT_SERVICE = "service"
STATUT_TRAVAUX = "travaux"
STATUT_PROJET = "projet"
#: Du plus avance au moins avance. La fusion de deux enregistrements garde le
#: statut le plus avance : une gare desservie aujourd'hui et retenue par une
#: ligne a venir existe bel et bien, elle gagne juste une ligne de plus.
ORDRE_STATUTS = (STATUT_SERVICE, STATUT_TRAVAUX, STATUT_PROJET)

#: Statuts IDFM des arrets en projet, ramenes aux deux notres. « Travaux »
#: designe un chantier ouvert ; les autres valeurs sont des etapes d'etude
#: (avant-projet, declaration d'utilite publique, etudes prealables).
STATUTS_PROJET_IDFM = {"travaux": STATUT_TRAVAUX}

#: Cles de nom rencontrees selon les jeux de donnees.
CLES_NOM_GARE = (
    "nom",
    "name",
    "libelle",
    "nom_long",
    "nom_gare",
    "nom_zdc",
    "nom_arret",
    "nom_zda",
    "nom_zdl",
    "gare",
    "intitule_gare",
)


@dataclass
class ResultatGeo:
    """Contours recuperes + journal de provenance affiche dans l'interface."""

    geojson: dict[str, Any] = field(default_factory=lambda: {"type": "FeatureCollection", "features": []})
    sources: dict[str, str] = field(default_factory=dict)
    communes_absentes: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    #: Empreinte du contenu de `geojson` (voir `empreinte`).
    empreinte: str = ""


def empreinte(geojson: dict[str, Any]) -> str:
    """Empreinte courte du contenu d'une collection, geometries comprises.

    Deux collections de meme taille mais de traces differents n'ont pas la
    meme empreinte : c'est elle qui decide de republier les contours. Deux
    dixiemes de seconde pour le Grand Paris, payes une fois par chargement.
    """

    texte = json.dumps(geojson, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(texte.encode("utf-8")).hexdigest()[:16]


def racine_projet() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repertoire_geo() -> str:
    return os.environ.get("DVF_GEO_DIR") or os.path.join(racine_projet(), "data", "geo")


def repertoire_cache() -> str:
    chemin = os.environ.get("DVF_CACHE_DIR") or os.path.join(racine_projet(), "data", "cache")
    os.makedirs(chemin, exist_ok=True)
    return chemin


# --------------------------------------------------------------------------
# Normalisation des contours
# --------------------------------------------------------------------------


def _identifiant_section(proprietes: dict[str, Any], insee_attendu: str | None = None) -> str | None:
    """Ramene les differents schemas cadastraux a la cle `INSEE + prefixe + section`.

    Trois schemas coexistent dans `data/geo/` :

    * les fichiers Etalab bruts (`id`, `commune`, `prefixe`, `code`) ;
    * les reponses de l'API Carto IGN (`code_dep` + `code_com`, `com_abs`,
      `section`) ;
    * les fichiers **deja normalises par ce module** (`code_section`), ecrits
      par `telecharger_contours_manquants` puis committes. Ce dernier cas doit
      etre reconnu en premier : sans lui, relire nos propres fichiers renvoie
      zero section et la commune disparait silencieusement de la carte.
    """

    for cle in ("code_section", "id"):
        identifiant = proprietes.get(cle)
        if isinstance(identifiant, str) and len(identifiant.strip()) >= 10:
            return identifiant.strip()[:10]

    insee = str(
        proprietes.get("commune")
        or proprietes.get("code_commune")
        or proprietes.get("code_insee")
        or (str(proprietes.get("code_dep", "")) + str(proprietes.get("code_com", "")))
        or insee_attendu
        or ""
    ).strip()
    prefixe = str(
        proprietes.get("prefixe") or proprietes.get("com_abs") or "000"
    ).strip().zfill(3)
    section = str(
        proprietes.get("code") or proprietes.get("section") or proprietes.get("section_courte") or ""
    ).strip()
    if not insee or not section:
        return None
    return f"{insee.zfill(5)}{prefixe}{section.zfill(2)}"


def _normaliser_collection(collection: dict[str, Any], insee: str | None = None) -> list[dict[str, Any]]:
    entites = []
    for entite in collection.get("features", []):
        proprietes = entite.get("properties") or {}
        code_section = _identifiant_section(proprietes, insee)
        if not code_section or not entite.get("geometry"):
            continue
        entites.append(
            {
                "type": "Feature",
                "geometry": entite["geometry"],
                "properties": {
                    "code_section": code_section,
                    "code_commune": code_section[:5],
                    "section_courte": code_section[5:].lstrip("0"),
                },
            }
        )
    return entites


# --------------------------------------------------------------------------
# Sources locales
# --------------------------------------------------------------------------


#: Code INSEE dans un nom de fichier de contours : « 75101-sections.geojson »,
#: « cadastre-2A004-sections.json »…
_INSEE_DANS_NOM = re.compile(r"(?<![0-9A-Za-z])(\d[0-9AB]\d{3})(?![0-9])")

#: Index `code INSEE -> fichiers de contours`, par repertoire parcouru. Un seul
#: parcours de `data/geo/` au lieu d'une recherche recursive par commune, qui
#: en demanderait plus de quatre cents.
_index_contours: dict[str, dict[str, list[str]]] = {}


def departement_de(insee: str) -> str:
    """Departement d'une commune : deux caracteres, trois en outre-mer."""

    return insee[:3] if insee[:2] in {"97", "98"} else insee[:2]


def _indexer(racine: str) -> dict[str, list[str]]:
    index = _index_contours.get(racine)
    if index is not None:
        return index
    index = {}
    for dossier, _, fichiers in sorted(os.walk(racine)):
        for nom in sorted(fichiers):
            if "section" not in nom or not nom.endswith((".geojson", ".json")):
                continue
            trouve = _INSEE_DANS_NOM.search(nom)
            if trouve:
                index.setdefault(trouve.group(1), []).append(os.path.join(dossier, nom))
    _index_contours[racine] = index
    return index


def oublier_index_contours() -> None:
    """A appeler apres avoir ecrit des contours : l'index sera reconstruit."""

    _index_contours.clear()


def _fichiers_locaux(insee: str) -> list[str]:
    """Fichiers de contours d'une commune : le chemin canonique d'abord."""

    candidats = [fichier_contours(insee)]
    candidats += _indexer(repertoire_geo()).get(insee, [])
    candidats.append(os.path.join(repertoire_cache(), f"{insee}-sections.geojson"))
    return [chemin for chemin in dict.fromkeys(candidats) if os.path.exists(chemin)]


def _charger_local(insee: str) -> tuple[list[dict[str, Any]], str | None]:
    for chemin in _fichiers_locaux(insee):
        try:
            with open(chemin, "r", encoding="utf-8") as flux:
                collection = json.load(flux)
        except (OSError, json.JSONDecodeError):
            continue
        entites = _normaliser_collection(collection, insee)
        if entites:
            origine = "cache" if repertoire_cache() in chemin else "local"
            return entites, f"{origine} ({os.path.basename(chemin)})"
    return [], None


# --------------------------------------------------------------------------
# Telechargement (non bloquant)
# --------------------------------------------------------------------------


def _telecharger_sections(insee: str) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Tente le telechargement des contours ; ne leve jamais d'exception."""

    if requests is None:
        return [], None, "Module `requests` absent : telechargement des contours impossible."

    departement = departement_de(insee)
    tentatives = (
        ("API Cadastre Etalab", URL_SECTIONS_ETALAB.format(departement=departement, insee=insee)),
        ("API Carto IGN", URL_SECTIONS_API.format(insee=insee)),
    )

    derniere_erreur: str | None = None
    for libelle, url in tentatives:
        try:
            reponse = requests.get(url, timeout=DELAI_RESEAU, headers=ENTETES_HTTP)
            reponse.raise_for_status()
            # Les fichiers Etalab sont gzippes : `requests` les decompresse seul
            # quand l'entete le declare, sinon on s'en charge ici.
            contenu = reponse.content
            if contenu[:2] == b"\x1f\x8b":
                import gzip

                contenu = gzip.decompress(contenu)
            collection = json.loads(contenu.decode("utf-8"))
            entites = _normaliser_collection(collection, insee)
            if entites:
                _ecrire_cache(insee, entites)
                return entites, libelle, None
            derniere_erreur = f"{libelle} : reponse sans section exploitable."
        except Exception as erreur:  # noqa: BLE001 - contrainte : jamais bloquant
            derniere_erreur = f"{libelle} : {type(erreur).__name__} — {erreur}"
    return [], None, derniere_erreur


def _ecrire_cache(insee: str, entites: list[dict[str, Any]]) -> None:
    try:
        chemin = os.path.join(repertoire_cache(), f"{insee}-sections.geojson")
        with open(chemin, "w", encoding="utf-8") as flux:
            json.dump({"type": "FeatureCollection", "features": entites}, flux)
    except OSError:
        pass


# --------------------------------------------------------------------------
# API publique du module
# --------------------------------------------------------------------------


def charger_sections(
    codes_communes: Iterable[str],
    sections_attendues: Sequence[str] | None = None,
    autoriser_reseau: bool = True,
) -> ResultatGeo:
    """Contours des sections des communes demandees.

    L'ordre de recherche est : `data/geo/` -> cache local -> API Cadastre.
    `sections_attendues` restreint la sortie aux sections effectivement
    presentes dans les CSV (le cadastre en contient souvent bien davantage).
    """

    resultat = ResultatGeo()
    toutes: list[dict[str, Any]] = []

    for insee in sorted({str(code).zfill(5) for code in codes_communes if code}):
        entites, origine = _charger_local(insee)
        if not entites and autoriser_reseau:
            entites, origine, erreur = _telecharger_sections(insee)
            if erreur and not entites:
                resultat.messages.append(f"Commune {insee} — {erreur}")
        if entites:
            resultat.sources[insee] = origine or "inconnue"
            toutes.extend(entites)
        else:
            resultat.communes_absentes.append(insee)

    if sections_attendues is not None:
        attendues = set(sections_attendues)
        toutes = [e for e in toutes if e["properties"]["code_section"] in attendues]

    # Deduplication defensive : un meme contour peut venir de deux fichiers.
    vues: set[str] = set()
    uniques = []
    for entite in toutes:
        code = entite["properties"]["code_section"]
        if code in vues:
            continue
        vues.add(code)
        uniques.append(entite)

    resultat.geojson = {"type": "FeatureCollection", "features": uniques}
    resultat.empreinte = empreinte(resultat.geojson)
    return resultat


def fichier_contours(insee: str) -> str:
    """Chemin canonique des contours d'une commune : `data/geo/<dep>/<insee>-sections.geojson`."""

    insee = str(insee).zfill(5)
    return os.path.join(repertoire_geo(), departement_de(insee), f"{insee}-sections.geojson")


def sections_locales(insee: str) -> set[str]:
    """Codes de section que le fichier local de cette commune couvre deja."""

    entites, _ = _charger_local(str(insee).zfill(5))
    return {entite["properties"]["code_section"] for entite in entites}


def telecharger_contours_manquants(
    codes_communes: Iterable[str],
    sections_attendues: Iterable[str] | None = None,
) -> tuple[dict[str, int], dict[str, str]]:
    """Ecrit dans `data/geo/` les contours qui manquent ou sont incomplets.

    Sans ces fichiers, les sections d'une commune sont absentes de la carte
    alors que ses ventes comptent dans les statistiques : l'ecart passe
    inapercu. Les contours telecharges ici sont versionnables, donc
    disponibles en production sans le moindre appel reseau.

    Un fichier deja present mais qui ne couvre pas toutes les sections
    attendues est **retelecharge** : c'est le cas d'un millesime cadastral
    anterieur, ou d'un fichier tronque. Passer `sections_attendues` active ce
    controle ; sans lui, seule l'absence de fichier declenche un telechargement.

    Renvoie `(communes ecrites -> nombre de sections, communes en echec -> raison)`.
    """

    attendues_par_commune: dict[str, set[str]] = {}
    for code_section in sections_attendues or []:
        code_section = str(code_section)
        attendues_par_commune.setdefault(code_section[:5], set()).add(code_section)

    ecrites: dict[str, int] = {}
    echecs: dict[str, str] = {}

    for insee in sorted({str(code).zfill(5) for code in codes_communes if code}):
        deja_la = sections_locales(insee)
        attendues = attendues_par_commune.get(insee, set())
        if deja_la and not (attendues - deja_la):
            continue

        entites, _, erreur = _telecharger_sections(insee)
        if not entites:
            echecs[insee] = erreur or "aucune section renvoyee"
            continue

        obtenues = {entite["properties"]["code_section"] for entite in entites}
        introuvables = attendues - obtenues
        if introuvables:
            # Le telechargement a reussi mais ne couvre toujours pas tout : on
            # ecrit quand meme le fichier, et on explique precisement l'ecart,
            # seul moyen de diagnostiquer une divergence de codification entre
            # les donnees DVF et le cadastre.
            echecs[insee] = (
                f"{len(introuvables)} section(s) absente(s) du cadastre telecharge — "
                f"attendu p. ex. {sorted(introuvables)[:3]}, "
                f"cadastre p. ex. {sorted(obtenues)[:3]}"
            )

        try:
            os.makedirs(os.path.dirname(fichier_contours(insee)), exist_ok=True)
            with open(fichier_contours(insee), "w", encoding="utf-8") as flux:
                json.dump({"type": "FeatureCollection", "features": entites}, flux)
        except OSError as erreur_ecriture:
            echecs[insee] = f"ecriture impossible : {erreur_ecriture}"
            continue
        oublier_index_contours()
        ecrites[insee] = len(entites)

    return ecrites, echecs


def etendue(geojson: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Boite englobante (lat_min, lon_min, lat_max, lon_max) d'une collection."""

    lats: list[float] = []
    lons: list[float] = []

    def parcourir(coordonnees: Any) -> None:
        if (
            isinstance(coordonnees, (list, tuple))
            and len(coordonnees) == 2
            and all(isinstance(c, (int, float)) for c in coordonnees)
        ):
            lons.append(float(coordonnees[0]))
            lats.append(float(coordonnees[1]))
            return
        if isinstance(coordonnees, (list, tuple)):
            for element in coordonnees:
                parcourir(element)

    for entite in geojson.get("features", []):
        parcourir((entite.get("geometry") or {}).get("coordinates"))

    if not lats:
        return None
    return min(lats), min(lons), max(lats), max(lons)


def _centre_anneau(anneau: list) -> tuple[float, float, float] | None:
    """Centroide et aire signee d'un anneau ferme, par la formule du lacet."""

    if not anneau or len(anneau) < 4:
        return None
    aire = 0.0
    cx = 0.0
    cy = 0.0
    for (x1, y1), (x2, y2) in zip(anneau, anneau[1:]):
        produit = x1 * y2 - x2 * y1
        aire += produit
        cx += (x1 + x2) * produit
        cy += (y1 + y2) * produit
    if abs(aire) < 1e-12:
        return None
    return cx / (3 * aire), cy / (3 * aire), abs(aire) / 2


def points_representatifs(
    geojson: dict[str, Any], cle: str = "code_commune"
) -> dict[str, tuple[float, float, float]]:
    """Un point d'ancrage par entite : `code -> (lon, lat, aire)`.

    Sert a poser les noms de communes sur la carte. Le point retenu est le
    centroide de l'anneau exterieur le plus vaste : une commune faite de
    plusieurs morceaux est etiquetee sur le principal, et non entre les deux.
    L'aire accompagne le point pour departager les etiquettes qui se
    chevauchent — a place egale, la plus grande commune l'emporte.
    """

    points: dict[str, tuple[float, float, float]] = {}
    for entite in geojson.get("features", []):
        code = (entite.get("properties") or {}).get(cle)
        geometrie = entite.get("geometry") or {}
        coordonnees = geometrie.get("coordinates")
        if not code or not coordonnees:
            continue
        if geometrie.get("type") == "Polygon":
            polygones = [coordonnees]
        elif geometrie.get("type") == "MultiPolygon":
            polygones = coordonnees
        else:
            continue
        meilleur = None
        for polygone in polygones:
            centre_aire = _centre_anneau(polygone[0] if polygone else [])
            if centre_aire and (meilleur is None or centre_aire[2] > meilleur[2]):
                meilleur = centre_aire
        if meilleur:
            points[str(code)] = (
                round(meilleur[0], 5), round(meilleur[1], 5), round(meilleur[2], 8)
            )
    return points


def _point_dans_anneau(lon: float, lat: float, anneau: list) -> bool:
    """Lancer de rayon : le point est-il a l'interieur de l'anneau ferme ?

    On compte les intersections entre une demi-droite horizontale partant du
    point et les segments de l'anneau : un nombre impair signifie dedans.
    """

    dedans = False
    nombre = len(anneau)
    precedent = nombre - 1
    for courant in range(nombre):
        try:
            lon_c, lat_c = float(anneau[courant][0]), float(anneau[courant][1])
            lon_p, lat_p = float(anneau[precedent][0]), float(anneau[precedent][1])
        except (TypeError, ValueError, IndexError):
            precedent = courant
            continue
        # Le segment traverse-t-il la latitude du point ?
        if (lat_c > lat) != (lat_p > lat):
            croisement = (lon_p - lon_c) * (lat - lat_c) / (lat_p - lat_c) + lon_c
            if lon < croisement:
                dedans = not dedans
        precedent = courant
    return dedans


def _point_dans_polygone(lon: float, lat: float, anneaux: list) -> bool:
    """Premier anneau = contour exterieur, suivants = trous."""

    if not anneaux or not _point_dans_anneau(lon, lat, anneaux[0]):
        return False
    return not any(_point_dans_anneau(lon, lat, trou) for trou in anneaux[1:])


def _point_dans_entite(lon: float, lat: float, entite: dict[str, Any]) -> bool:
    geometrie = entite.get("geometry") or {}
    coordonnees = geometrie.get("coordinates")
    if not coordonnees:
        return False
    if geometrie.get("type") == "Polygon":
        return _point_dans_polygone(lon, lat, coordonnees)
    if geometrie.get("type") == "MultiPolygon":
        return any(_point_dans_polygone(lon, lat, polygone) for polygone in coordonnees)
    return False


def gares_dans_sections(
    gares: list[dict[str, Any]], geojson: dict[str, Any]
) -> list[dict[str, Any]]:
    """Ne garde que les gares situees a l'interieur des sections chargees.

    L'emprise rectangulaire utilisee pour le telechargement deborde largement
    des sections reellement analysees — elle englobe les communes voisines, et
    meme les sections d'une commune analysee pour lesquelles aucun CSV n'a ete
    fourni. Ce filtre ramene l'affichage au perimetre effectif de l'etude.
    """

    entites = geojson.get("features") or []
    if not entites:
        return []

    # Pre-filtre par boite englobante : le test exact ne tourne que sur les
    # candidats plausibles.
    boites = []
    for entite in entites:
        boite = etendue({"features": [entite]})
        boites.append((boite, entite))

    retenues = []
    for gare in gares:
        try:
            lon, lat = float(gare["longitude"]), float(gare["latitude"])
        except (KeyError, TypeError, ValueError):
            continue
        for boite, entite in boites:
            if not boite:
                continue
            if not (boite[0] <= lat <= boite[2] and boite[1] <= lon <= boite[3]):
                continue
            if _point_dans_entite(lon, lat, entite):
                retenues.append(gare)
                break
    return retenues


# --------------------------------------------------------------------------
# Gares — strictement non bloquant
# --------------------------------------------------------------------------


def fichier_gares() -> str:
    """Gares memorisees sur disque, telechargees une seule fois.

    Le fichier vit dans `data/geo/` et non dans le cache jetable : il est
    versionnable, il survit aux redemarrages, et il enregistre les emprises
    deja interrogees. Ajouter des communes elargit l'emprise ; seul ce qui
    manque est alors telecharge, puis fusionne dans le meme fichier.
    """

    return os.path.join(repertoire_geo(), "gares.json")


def _lire_fichier_gares() -> tuple[list[dict[str, Any]], list[list[float]]]:
    """Renvoie `(gares, emprises deja couvertes)`."""

    chemin = fichier_gares()
    if not os.path.exists(chemin):
        return [], []
    try:
        with open(chemin, "r", encoding="utf-8") as flux:
            contenu = json.load(flux)
    except (OSError, json.JSONDecodeError):
        return [], []
    if not isinstance(contenu, dict):
        return [], []
    emprises = [
        [float(v) for v in emprise]
        for emprise in contenu.get("emprises", [])
        if isinstance(emprise, (list, tuple)) and len(emprise) == 4
    ]
    # Dedoublonnage et reconciliation a la lecture aussi : c'est ce qui
    # applique les regles du code, quel que soit le fichier.
    return _dedoublonner_gares(contenu.get("gares", [])), emprises


def _ecrire_fichier_gares(gares: list[dict[str, Any]], emprises: list[list[float]]) -> None:
    try:
        chemin = fichier_gares()
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "w", encoding="utf-8") as flux:
            json.dump(
                {
                    "emprises": emprises,
                    "gares": sorted(gares, key=lambda g: g["nom"]),
                },
                flux,
                ensure_ascii=False,
                indent=1,
            )
    except OSError:
        pass


def _lignes_valides(brutes: Any) -> list[str]:
    """Libelles de lignes, sans doublon et dans l'ordre de lecture."""

    vus: dict[str, str] = {}
    morceaux = brutes if isinstance(brutes, (list, tuple)) else str(brutes or "").split(";")
    for morceau in morceaux:
        libelle = str(morceau).strip()
        if libelle:
            vus.setdefault(_cle_nom_gare(libelle), libelle)
    return sorted(vus.values(), key=_ordre_ligne)


def _gares_valides(brutes: Any) -> list[dict[str, Any]]:
    """Normalise une liste de gares en ecartant les entrees inexploitables."""

    retenues = []
    for gare in brutes if isinstance(brutes, list) else []:
        try:
            latitude = float(gare["latitude"])
            longitude = float(gare["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        statut = str(gare.get("statut") or STATUT_SERVICE)
        statut = statut if statut in ORDRE_STATUTS else STATUT_SERVICE
        lignes = _lignes_valides(gare.get("lignes"))
        retenues.append(
            {
                "nom": str(gare.get("nom") or "Gare"),
                "latitude": latitude,
                "longitude": longitude,
                "reseau": str(gare.get("reseau") or ""),
                "type": str(gare.get("type") or "station"),
                # Sans statut, une gare est en service ; sans lignes, on
                # ne les connait pas (OpenStreetMap, SNCF).
                "statut": statut,
                "lignes": lignes,
                # Lignes annoncees sur un pole deja desservi. Une gare a venir
                # n'en a pas : toutes ses `lignes` le sont deja.
                "lignes_a_venir": [
                    ligne
                    for ligne in _lignes_valides(gare.get("lignes_a_venir"))
                    if statut == STATUT_SERVICE and not _ligne_parmi(ligne, lignes)
                ],
            }
        )
    return retenues


def _ligne_parmi(ligne: str, lignes: Iterable[str]) -> bool:
    """Vrai si `ligne` figure dans `lignes`, a l'ecriture pres."""

    cle = _cle_nom_gare(ligne)
    return any(_cle_nom_gare(autre) == cle for autre in lignes)


def _dans_emprise(gare: dict[str, Any], boite, marge: float = 0.02) -> bool:
    lat_min, lon_min, lat_max, lon_max = boite
    return (
        lat_min - marge <= gare["latitude"] <= lat_max + marge
        and lon_min - marge <= gare["longitude"] <= lon_max + marge
    )


def _emprise_couverte(boite, emprises: list[list[float]], marge: float = 0.01) -> bool:
    """Vrai si l'emprise demandee tient dans une emprise deja telechargee."""

    lat_min, lon_min, lat_max, lon_max = boite
    for couverte in emprises:
        if (
            couverte[0] - marge <= lat_min
            and couverte[1] - marge <= lon_min
            and lat_max <= couverte[2] + marge
            and lon_max <= couverte[3] + marge
        ):
            return True
    return False


def _fusionner_emprises(emprises: list[list[float]], nouvelle) -> list[list[float]]:
    """Ajoute une emprise en retirant celles qu'elle englobe desormais."""

    conservees = [e for e in emprises if not _emprise_couverte(e, [list(nouvelle)], marge=0.0)]
    conservees.append([float(v) for v in nouvelle])
    return conservees


def _resumer_erreur(erreur: Exception) -> str:
    """Message court et lisible, sans l'URL complete."""

    reponse = getattr(erreur, "response", None)
    code = getattr(reponse, "status_code", None)
    if code == 429:
        return "quota atteint (429 Too Many Requests)"
    if code:
        return f"erreur HTTP {code}"
    # Nos propres verifications (reponse partielle, pagination impossible)
    # levent un `RuntimeError` dont le message est deja redige pour etre lu ;
    # les exceptions reseau, elles, trainent l'URL complete derriere elles.
    if isinstance(erreur, RuntimeError) and str(erreur):
        return str(erreur)
    return type(erreur).__name__


#: Distance en deca de laquelle deux gares de meme nom sont la meme gare.
#: Mesure faite sur le fichier du depot : les doublons observes (noeud OSM et
#: centre du contour, quai SNCF et acces metro d'un meme pole) sont a moins de
#: 250 m les uns des autres, et deux gares reellement distinctes portant le
#: meme nom sont toujours dans des communes differentes, donc bien au-dela.
DISTANCE_FUSION_GARES = 400.0

#: Reseaux « lourds » : une gare qu'on veut reperer de loin.
_RESEAUX_LOURDS = (
    "rer", "transilien", "ter", "tgv", "intercit", "eurostar", "sncf", "train", "grandes lignes",
)
#: Reseaux « legers » : metro, tramway, funiculaire. Ce sont eux qui saturaient
#: la carte — a Paris il y en a un tous les trois cents metres.
_RESEAUX_LEGERS = ("metro", "tram", "funicul")
#: Valeurs de `type` (tag OSM `station` ou `railway`) designant un reseau leger.
_TYPES_LEGERS = frozenset(
    {"subway", "light_rail", "tram", "tram_stop", "funicular", "monorail"}
)

#: Rangs exposes a la carte : ils pilotent la taille du point.
RANG_LEGER = 1
RANG_LOURD = 2

#: Genres exposes a la carte : ils pilotent le **dessin** du point, la ou le
#: rang pilote sa taille : un train, un « M » et un « T » disent ce qu'on y
#: prend sans legende.
GENRE_TRAIN = "train"
GENRE_METRO = "metro"
GENRE_TRAM = "tram"

#: Ce qui designe un tramway, et ce qui designe un metro, dans le reseau
#: declare comme dans le `type` d'origine (tags OSM `station` / `railway`).
_MOTS_TRAM = ("tram", "funicul")
_TYPES_TRAM = frozenset({"tram", "tram_stop", "funicular"})
_MOTS_METRO = ("metro", "subway")
_TYPES_METRO = frozenset({"subway", "light_rail", "monorail"})


def _sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFKD", str(texte))
    return "".join(c for c in decompose if not unicodedata.combining(c))


def _cle_nom_gare(nom: str) -> str:
    """Nom reduit a ses lettres et ses chiffres, sans accents ni casse."""

    return "".join(c for c in _sans_accents(nom).casefold() if c.isalnum())


def rang_gare(gare: dict[str, Any]) -> int:
    """`RANG_LOURD` pour une gare RER / Transilien / TER / TGV, sinon `RANG_LEGER`.

    Le rang decide de la taille du point sur la carte. Un pole desservi a la
    fois par le metro et par le RER compte comme lourd : c'est le RER qui
    justifie qu'on le voie en vue d'ensemble.
    """

    reseau = _sans_accents(gare.get("reseau") or "").casefold()
    if any(mot in reseau for mot in _RESEAUX_LOURDS):
        return RANG_LOURD
    if str(gare.get("type") or "").casefold() in _TYPES_LEGERS:
        return RANG_LEGER
    if any(mot in reseau for mot in _RESEAUX_LEGERS):
        return RANG_LEGER
    # `railway=station` sans reseau renseigne : le cas le plus frequent, et
    # c'est une gare ferroviaire — le metro sort d'OSM avec `station=subway`.
    return RANG_LOURD


def genre_gare(gare: dict[str, Any]) -> str:
    """Dessin a poser sur la carte : `train`, `metro` ou `tram`.

    Le rang dit de combien de loin on veut voir l'arret ; le genre dit ce
    qu'on y prend. Les deux ne se recouvrent pas tout a fait : un pole
    desservi par le RER **et** par le metro est lourd — on veut le reperer de
    loin — et c'est bien un train qu'on y prend en premier.

    L'ordre des tests suit celui de la portee : ce qui va le plus loin
    l'emporte, comme sur un panneau de correspondances.
    """

    reseau = _sans_accents(gare.get("reseau") or "").casefold()
    type_origine = str(gare.get("type") or "").casefold()
    if any(mot in reseau for mot in _RESEAUX_LOURDS):
        return GENRE_TRAIN
    if any(mot in reseau for mot in _MOTS_METRO):
        return GENRE_METRO
    if any(mot in reseau for mot in _MOTS_TRAM):
        return GENRE_TRAM
    if type_origine in _TYPES_METRO:
        return GENRE_METRO
    if type_origine in _TYPES_TRAM:
        return GENRE_TRAM
    # Meme repli que `rang_gare` : un `railway=station` sans reseau renseigne
    # est une gare ferroviaire.
    return GENRE_TRAIN


def _distance_metres(un: dict[str, Any], autre: dict[str, Any]) -> float:
    """Distance plane approchee, largement suffisante a l'echelle d'un pole."""

    latitude_moyenne = math.radians((un["latitude"] + autre["latitude"]) / 2)
    dy = (un["latitude"] - autre["latitude"]) * 111_320
    dx = (un["longitude"] - autre["longitude"]) * 111_320 * math.cos(latitude_moyenne)
    return math.hypot(dx, dy)


def _fusionner_reseaux(un: str, autre: str) -> str:
    """Union des reseaux de deux enregistrements, sans repetition."""

    vus: dict[str, str] = {}
    for morceau in str(un).split(";") + str(autre).split(";"):
        morceau = morceau.strip()
        if morceau:
            vus.setdefault(_cle_nom_gare(morceau), morceau)
    return ";".join(vus.values())


#: Ordre d'affichage des lignes d'un pole : le reseau qui porte le plus loin
#: d'abord. C'est l'ordre dans lequel on lit un panneau de correspondances.
_ORDRE_RESEAUX_LIGNE = ("grandes lignes", "rer", "train", "metro", "tram")


def _ordre_ligne(libelle: str) -> tuple[int, int, str]:
    """Cle de tri d'un libelle de ligne : reseau, puis numero, puis alphabet.

    Le numero est extrait pour que « Métro 11 » suive « Métro 4 » au lieu de le
    preceder, comme le ferait un tri purement alphabetique.
    """

    reduit = _sans_accents(libelle).casefold()
    reseau = next(
        (i for i, mot in enumerate(_ORDRE_RESEAUX_LIGNE) if mot and mot in reduit),
        len(_ORDRE_RESEAUX_LIGNE),
    )
    chiffres = "".join(c for c in reduit if c.isdigit())
    return (reseau, int(chiffres) if chiffres else 0, reduit)


def _dedoublonner_gares(gares: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Une meme gare revient en plusieurs objets ; on n'en garde qu'un.

    Les sources se recoupent — noeud OSM et centre du contour, quai SNCF et
    acces metro, portail Opendatasoft et Overpass — et livrent des positions
    voisines mais jamais identiques : arrondir les coordonnees ne suffit pas.

    Deux enregistrements de meme nom distants de moins de
    `DISTANCE_FUSION_GARES` sont donc fusionnes : on garde la position du plus
    « lourd », l'**union** de leurs reseaux et de leurs lignes, et le statut le
    plus avance — un pole ou s'arretera la ligne 15 n'en reste pas moins
    desservi aujourd'hui, il gagne simplement une ligne a venir.
    """

    # Normalisation d'abord : chaque source renseigne les champs qu'elle
    # connait, et la suite du programme compte sur les cinq.
    groupes: dict[str, list[dict[str, Any]]] = {}
    for gare in _gares_valides(list(gares)):
        groupes.setdefault(_cle_nom_gare(gare["nom"]), []).append(gare)

    retenues: list[dict[str, Any]] = []
    for homonymes in groupes.values():
        # Les gares lourdes d'abord : c'est leur position et leur type que la
        # fusion conserve.
        for gare in sorted(homonymes, key=lambda g: -rang_gare(g)):
            voisine = next(
                (
                    autre
                    for autre in retenues
                    if _cle_nom_gare(autre["nom"]) == _cle_nom_gare(gare["nom"])
                    and _distance_metres(autre, gare) <= DISTANCE_FUSION_GARES
                ),
                None,
            )
            if voisine is None:
                retenues.append(dict(gare))
            else:
                # Le reseau decide du dessin : un arret de tramway ou le metro
                # n'arrivera que dans deux ans reste un « T ». Seuls comptent
                # les reseaux de ce qui roule, s'il roule quelque chose.
                service_1 = voisine["statut"] == STATUT_SERVICE
                service_2 = gare["statut"] == STATUT_SERVICE
                if service_2 and not service_1:
                    voisine["reseau"], voisine["type"] = gare.get("reseau", ""), gare["type"]
                elif service_1 == service_2:
                    voisine["reseau"] = _fusionner_reseaux(voisine["reseau"], gare.get("reseau", ""))
                _fusionner_dessertes(voisine, gare)
    return _reconcilier_mises_en_service(retenues)


def _dessertes(gare: dict[str, Any]) -> tuple[list[str], list[str]]:
    """`(lignes en service, lignes a venir)` d'un enregistrement normalise."""

    if gare["statut"] == STATUT_SERVICE:
        return list(gare["lignes"]), list(gare.get("lignes_a_venir") or [])
    return [], list(gare["lignes"])


def _poser_dessertes(
    gare: dict[str, Any], en_service: list[str], a_venir: list[str], statut_a_venir: str
) -> None:
    """Ecrit dans `gare` ses lignes, son statut et ses lignes a venir.

    Une gare ou roule deja une ligne est **en service**, meme si d'autres y
    sont annoncees : elle porte alors celles-ci dans `lignes_a_venir`. Une
    gare ou rien ne roule encore porte le statut de son projet le plus avance,
    et toutes ses lignes sont a venir.
    """

    en_service = _lignes_valides(en_service)
    a_venir = [l for l in _lignes_valides(a_venir) if not _ligne_parmi(l, en_service)]
    if gare["statut"] == STATUT_SERVICE or en_service:
        gare["statut"] = STATUT_SERVICE
        gare["lignes"], gare["lignes_a_venir"] = en_service, a_venir
    else:
        gare["statut"] = statut_a_venir
        gare["lignes"], gare["lignes_a_venir"] = a_venir, []


def _fusionner_dessertes(voisine: dict[str, Any], gare: dict[str, Any]) -> None:
    """Fusionne dans `voisine` les lignes et le statut de `gare`.

    Le statut le plus avance l'emporte : un pole ou s'arretera la ligne 15
    n'en reste pas moins desservi aujourd'hui. Mais la ligne 15 n'y devient
    pas pour autant une ligne en service : elle reste **a venir**, et le
    redeviendra seulement le jour ou la source des gares en service la cite.
    """

    service_1, venir_1 = _dessertes(voisine)
    service_2, venir_2 = _dessertes(gare)
    statuts = [g["statut"] for g in (voisine, gare) if g["statut"] != STATUT_SERVICE]
    statut_a_venir = min(statuts or [STATUT_TRAVAUX], key=ORDRE_STATUTS.index)
    if STATUT_SERVICE in (voisine["statut"], gare["statut"]):
        voisine["statut"] = STATUT_SERVICE
    _poser_dessertes(voisine, service_1 + service_2, venir_1 + venir_2, statut_a_venir)


#: Distance en deca de laquelle une ligne a venir est reputee ouverte, des
#: qu'une gare **en service** la cite. Mesure faite sur les deux jeux
#: d'Île-de-France Mobilites en septembre 2026 : une gare ouverte depuis des
#: mois reste listee « en travaux » dans le jeu des projets (Nanterre - La
#: Folie, RER E, ouverte en 2024), a quelques metres de sa place dans le jeu
#: des gares en service (0 a 170 m selon le point de reference retenu). Deux
#: arrets distincts d'une meme ligne, eux, ne sont jamais a moins de 200 m
#: (Daniel Perdrigé et Arboretum, sur le tramway 4).
DISTANCE_MEME_ARRET = 150.0


def _meme_arret(un: dict[str, Any], autre: dict[str, Any]) -> bool:
    """Deux enregistrements qui designent sans doute le meme arret.

    Tout pres l'un de l'autre, ou un peu plus loin mais sous des noms dont
    l'un contient l'autre (« Noisy-le-Sec RER » et « Noisy-le-Sec »).
    """

    distance = _distance_metres(un, autre)
    if distance <= DISTANCE_MEME_ARRET:
        return True
    if distance > DISTANCE_FUSION_GARES:
        return False
    nom_1, nom_2 = _cle_nom_gare(un["nom"]), _cle_nom_gare(autre["nom"])
    return bool(nom_1 and nom_2) and (nom_1 in nom_2 or nom_2 in nom_1)


def _reconcilier_mises_en_service(gares: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retire des lignes « a venir » celles qui roulent deja.

    C'est ce qui rend la carte robuste a l'ouverture d'une ligne. Le jeu des
    projets d'IDFM ne retire pas une gare le jour ou elle ouvre : il la garde
    des mois « en travaux », alors que le jeu des gares en service la cite
    deja, parfois sous un autre nom. La regle est donc tiree de la seule
    source qui fasse foi, celle des gares **en service** : une ligne a venir
    est ouverte des qu'une gare en service de la meme ligne se tient au meme
    endroit (`_meme_arret`).

    - Une gare a venir dont toutes les lignes roulent disparait : la gare en
      service qui la double la represente deja, et porte ces lignes.
    - Une gare a venir dont une partie seulement roule reste a venir, pour
      les autres lignes.
    - Un pole deja desservi perd de ses `lignes_a_venir` celles qu'on y prend
      aujourd'hui.

    Idempotente : l'appliquer deux fois ne change rien.
    """

    en_service = [g for g in gares if g["statut"] == STATUT_SERVICE and g["lignes"]]
    retenues = []
    for gare in gares:
        _, a_venir = _dessertes(gare)
        ouvertes = [
            ligne
            for ligne in a_venir
            if any(
                autre is not gare and _ligne_parmi(ligne, autre["lignes"]) and _meme_arret(gare, autre)
                for autre in en_service
            )
        ]
        if not ouvertes:
            retenues.append(gare)
            continue
        restantes = [ligne for ligne in a_venir if ligne not in ouvertes]
        if gare["statut"] != STATUT_SERVICE and not restantes:
            continue
        gare = dict(gare)
        if gare["statut"] == STATUT_SERVICE:
            gare["lignes_a_venir"] = restantes
        else:
            gare["lignes"] = restantes
        retenues.append(gare)
    return retenues


#: Valeurs de `railway` (et de `construction:railway`, `proposed:railway`)
#: retenues comme gare ou station. `tram_stop` designe l'arret de tramway.
_RAILWAY_GARES = "^(station|halt|tram_stop)$"

#: Cote d'une tuile Overpass, en degres. Une seule requete sur l'Île-de-France
#: entiere depasse le temps imparti au serveur : il repond alors `200`, avec un
#: `remark` d'avertissement et **les seuls objets qu'il a eu le temps de
#: parcourir**. Un decoupage en tuiles maintient chaque requete loin de la
#: limite, et le `remark` est traite comme un echec.
PAS_TUILE_OVERPASS = 0.25


def _tuiles(boite: tuple[float, float, float, float], pas: float) -> list[tuple[float, ...]]:
    """Decoupe une emprise en tuiles d'au plus `pas` degres de cote."""

    lat_min, lon_min, lat_max, lon_max = boite
    decoupees = []
    latitude = lat_min
    while latitude < lat_max:
        longitude = lon_min
        while longitude < lon_max:
            decoupees.append(
                (latitude, longitude, min(latitude + pas, lat_max), min(longitude + pas, lon_max))
            )
            longitude += pas
        latitude += pas
    return decoupees or [tuple(boite)]


def _statut_overpass(etiquettes: dict[str, Any]) -> str:
    """Statut d'un objet OSM : en service, en travaux ou en projet."""

    if etiquettes.get("railway") == "proposed" or etiquettes.get("proposed:railway"):
        return STATUT_PROJET
    if etiquettes.get("railway") == "construction" or etiquettes.get("construction:railway"):
        return STATUT_TRAVAUX
    return STATUT_SERVICE


def _interroger_overpass(requete: str) -> dict[str, Any]:
    """Une requete Overpass, sur le premier miroir qui repond **completement**.

    Un `remark` dans la reponse signale une requete interrompue en cours de
    route. La reponse porte alors le code `200` et une liste d'objets partielle
    qu'il serait desastreux de prendre pour le reseau complet : elle serait
    ecrite sur disque et l'emprise marquee comme couverte, donc jamais reprise.
    """

    derniere: Exception | None = None
    for url in URLS_OVERPASS:
        try:
            reponse = requests.post(
                url, data={"data": requete}, timeout=DELAI_RESEAU, headers=ENTETES_HTTP
            )
            reponse.raise_for_status()
            charge = reponse.json()
            remarque = charge.get("remark")
            if remarque:
                raise RuntimeError(f"réponse partielle ({str(remarque)[:60]})")
            return charge
        except Exception as erreur:  # noqa: BLE001 - contrainte : jamais bloquant
            derniere = erreur
    raise derniere if derniere else RuntimeError("aucun miroir Overpass interroge")


def _gares_overpass(boite: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Gares, stations et arrets de tramway depuis OpenStreetMap.

    Source nationale, interrogee tuile par tuile. Une tuile en echec interrompt
    tout : mieux vaut aucune gare qu'un reseau a trous memorise pour de bon.
    """

    gares = []
    for tuile in _tuiles(boite, PAS_TUILE_OVERPASS):
        marge = 0.01
        cadre = (
            f"{tuile[0] - marge},{tuile[1] - marge},{tuile[2] + marge},{tuile[3] + marge}"
        )
        requete = f"""
        [out:json][timeout:{DELAI_RESEAU * 4}];
        (
          nwr["railway"~"{_RAILWAY_GARES}"]({cadre});
          nwr["construction:railway"~"{_RAILWAY_GARES}"]({cadre});
          nwr["proposed:railway"~"{_RAILWAY_GARES}"]({cadre});
        );
        out center tags;
        """
        for element in _interroger_overpass(requete).get("elements", []):
            latitude = element.get("lat") or (element.get("center") or {}).get("lat")
            longitude = element.get("lon") or (element.get("center") or {}).get("lon")
            etiquettes = element.get("tags") or {}
            if latitude is None or longitude is None:
                continue
            if etiquettes.get("usage") == "industrial":
                continue
            gares.append(
                {
                    "nom": etiquettes.get("name") or "Gare",
                    "latitude": float(latitude),
                    "longitude": float(longitude),
                    "reseau": etiquettes.get("network") or etiquettes.get("operator") or "",
                    "type": (
                        etiquettes.get("station")
                        or etiquettes.get("construction:railway")
                        or etiquettes.get("proposed:railway")
                        or etiquettes.get("railway")
                        or "station"
                    ),
                    "statut": _statut_overpass(etiquettes),
                }
            )
    return _dedoublonner_gares(gares)


def _nom_depuis_champs(champs: dict[str, Any]) -> str:
    """Nom de la gare, quel que soit le nommage des colonnes du jeu de donnees."""

    for cle in CLES_NOM_GARE:
        valeur = champs.get(cle)
        if isinstance(valeur, str) and valeur.strip():
            return valeur.strip()
    for cle, valeur in champs.items():
        if isinstance(valeur, str) and valeur.strip() and ("nom" in cle or "libelle" in cle):
            return valeur.strip()
    return "Gare"


def _enregistrements_opendatasoft(
    url: str, jeu: str, boite: tuple[float, float, float, float] | None = None
) -> list[dict[str, Any]]:
    """Tous les enregistrements d'un jeu Opendatasoft, page apres page.

    L'API v1 ne renvoie qu'une page a la fois et ne dit ce qui manque que par
    son `nhits` : le reseau francilien compte plus de mille enregistrements.
    On pagine donc jusqu'a `nhits`, et on refuse de rendre un resultat qu'on
    sait tronque.
    """

    parametres: dict[str, Any] = {"dataset": jeu, "rows": LOT_OPENDATASOFT}
    if boite:
        lat_min, lon_min, lat_max, lon_max = boite
        parametres["geofilter.bbox"] = f"{lat_min},{lon_min},{lat_max},{lon_max}"

    enregistrements: list[dict[str, Any]] = []
    attendus: int | None = None
    while True:
        reponse = requests.get(
            url,
            params={**parametres, "start": len(enregistrements)},
            timeout=DELAI_RESEAU,
            headers=ENTETES_HTTP,
        )
        reponse.raise_for_status()
        charge = reponse.json()
        page = charge.get("records") or []
        if attendus is None:
            attendus = charge.get("nhits")
        enregistrements.extend(page)
        if not page or attendus is None or len(enregistrements) >= attendus:
            break
        if len(enregistrements) + LOT_OPENDATASOFT > PLAFOND_OPENDATASOFT:
            raise RuntimeError(
                f"{attendus} enregistrements, au-delà du plafond de pagination "
                f"({PLAFOND_OPENDATASOFT})"
            )
    return enregistrements


def _point_opendatasoft(enregistrement: dict[str, Any]) -> tuple[float, float] | None:
    """Coordonnees `(latitude, longitude)` d'un enregistrement, ou rien."""

    coordonnees = (enregistrement.get("geometry") or {}).get("coordinates")
    if not (isinstance(coordonnees, (list, tuple)) and len(coordonnees) >= 2):
        return None
    return float(coordonnees[1]), float(coordonnees[0])


def _gares_opendatasoft(
    boite: tuple[float, float, float, float], url: str, jeu: str
) -> list[dict[str, Any]]:
    """Gares depuis un portail Opendatasoft (SNCF Open Data).

    L'API v1 est utilisee a dessein : elle expose la geometrie sous une cle
    `geometry` normalisee, alors que le nom du champ geographique varie d'un
    jeu de donnees a l'autre.
    """

    gares = []
    for enregistrement in _enregistrements_opendatasoft(url, jeu, boite):
        point = _point_opendatasoft(enregistrement)
        if point is None:
            continue
        champs = enregistrement.get("fields") or {}
        gares.append(
            {
                "nom": _nom_depuis_champs(champs),
                "latitude": point[0],
                "longitude": point[1],
                "reseau": str(champs.get("reseau") or champs.get("exploitant") or ""),
                "type": "station",
            }
        )
    return _dedoublonner_gares(gares)


#: Modes IDFM, ramenes au couple `(reseau, type)` dont se sert `rang_gare`.
#: C'est le mode qui decide de la taille du point, pas l'exploitant : « RATP »
#: ne dit pas si l'on a devant soi la ligne 4 ou le RER A.
MODES_IDFM = {
    "rer": ("RER", "station"),
    "train": ("Transilien", "station"),
    "metro": ("Métro", "subway"),
    "tramway": ("Tramway", "tram"),
    "tram": ("Tramway", "tram"),
    "val": ("VAL", "light_rail"),
    "cable": ("Câble", "funicular"),
    "funiculaire": ("Funiculaire", "funicular"),
    "bus": ("Bus", "bus"),
    "tzen": ("Bus", "bus"),
}

#: Libelles de ligne IDFM, ecrits en capitales, ramenes a la graphie d'un plan.
#: Les entrees a un seul mot s'appliquent au premier mot du libelle (« METRO 9 »
#: → « Métro 9 »), les autres au libelle entier.
_LIBELLES_LIGNE_IDFM = {
    "METRO": "Métro",
    "TRAM": "Tram",
    "TRAIN": "Train",
    "CABLE": "Câble",
    "BUS": "Bus",
    "TZEN": "TZen",
    "GL": "Grandes lignes",
    "FUNICULAIRE MONTMARTRE": "Funiculaire de Montmartre",
}


def _mode_idfm(champs: dict[str, Any]) -> tuple[str, str]:
    """`(reseau, type)` d'un enregistrement IDFM, d'apres son mode."""

    mode = _sans_accents(champs.get("mode") or champs.get("sous_mode") or "").casefold()
    for cle, valeur in MODES_IDFM.items():
        if cle in mode:
            return valeur
    return ("", "station")


def _libelle_ligne_idfm(res_com: Any) -> str:
    """« METRO 9 » → « Métro 9 » : le libelle tel qu'on le lit sur un plan."""

    entier = str(res_com or "").strip()
    if entier in _LIBELLES_LIGNE_IDFM:
        return _LIBELLES_LIGNE_IDFM[entier]
    mots = entier.split()
    if not mots:
        return ""
    return " ".join([_LIBELLES_LIGNE_IDFM.get(mots[0], mots[0])] + mots[1:])


def _gares_idfm(boite: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Gares et stations franciliennes en service, par ligne desservie.

    Le jeu porte un enregistrement par couple (gare, ligne) : la fusion les
    ramene a un point unique qui connait toutes ses lignes.
    """

    gares = []
    for enregistrement in _enregistrements_opendatasoft(URL_IDFM, JEU_IDFM_GARES, boite):
        point = _point_opendatasoft(enregistrement)
        if point is None:
            continue
        champs = enregistrement.get("fields") or {}
        reseau, type_gare = _mode_idfm(champs)
        gares.append(
            {
                # `nom_zdc` est le nom du pole d'echange, celui des plans ;
                # `nom_zda` descend au quai, et dedoublerait la gare.
                "nom": _nom_depuis_champs(champs),
                "latitude": point[0],
                "longitude": point[1],
                "reseau": reseau,
                "type": type_gare,
                "statut": STATUT_SERVICE,
                "lignes": [_libelle_ligne_idfm(champs.get("res_com"))],
            }
        )
    return _dedoublonner_gares(gares)


def _gares_projets_idfm(boite: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Gares et stations franciliennes a venir : Grand Paris Express, Eole, tram.

    Le jeu melange le fer et la route ; les projets de bus et de TZen sont
    ecartes — une station de metro et un arret d'autobus ne pesent pas la meme
    chose sur un prix au metre carre.
    """

    gares = []
    for enregistrement in _enregistrements_opendatasoft(URL_IDFM, JEU_IDFM_PROJETS, boite):
        point = _point_opendatasoft(enregistrement)
        if point is None:
            continue
        champs = enregistrement.get("fields") or {}
        reseau, type_gare = _mode_idfm(champs)
        if type_gare == "bus" or not champs.get("nom_arret"):
            # Sans nom, l'arret ne se legende pas et ne se fusionne pas : c'est
            # un point de trace, pas une gare.
            continue
        statut = _sans_accents(champs.get("statut") or "").casefold()
        gares.append(
            {
                "nom": _nom_depuis_champs(champs),
                "latitude": point[0],
                "longitude": point[1],
                "reseau": reseau,
                "type": type_gare,
                "statut": STATUTS_PROJET_IDFM.get(statut, STATUT_PROJET),
                "lignes": [_libelle_ligne_idfm(champs.get("res_com"))],
            }
        )
    return _dedoublonner_gares(gares)


def _fournisseurs_gares(boite: tuple[float, float, float, float]):
    """Sources des gares en service, dans l'ordre ; la premiere complete gagne.

    Elles ne sont pas cumulees a dessein : chacune nomme les gares a sa façon
    (« Gare de Houilles » ici, « Houilles Carrières-sur-Seine » la), et les
    additionner reposerait deux points cote a cote sur le meme pole. Une source
    donne donc le reseau entier, ou elle cede la place.

    En Île-de-France, le referentiel regional passe devant : il est exhaustif et
    connait les lignes desservies. Ailleurs, il n'a rien a dire, et on ne
    l'interroge pas — un reseau tronque serait pire que pas de reseau.
    """

    fournisseurs = []
    if _emprise_couverte(boite, [list(EMPRISE_IDF)], marge=0.0):
        fournisseurs.append(("Île-de-France Mobilités", _gares_idfm))
    fournisseurs.append(("OpenStreetMap (Overpass)", _gares_overpass))
    for libelle, url, jeu in JEUX_OPENDATASOFT:
        fournisseurs.append(
            (libelle, lambda boite, url=url, jeu=jeu: _gares_opendatasoft(boite, url, jeu))
        )
    return fournisseurs


def _fournisseurs_projets(boite: tuple[float, float, float, float]):
    """Sources des gares a venir. Elles s'ajoutent aux gares en service.

    Ici le cumul ne pose pas le probleme des homonymes : ce sont des gares qui
    n'existent pas encore, et celles qui doublent un pole deja desservi sont
    fusionnees avec lui — il gagne une ligne a venir sans se dedoubler.
    """

    if _emprise_couverte(boite, [list(EMPRISE_IDF)], marge=0.0):
        return [("Île-de-France Mobilités (projets)", _gares_projets_idfm)]
    return []


def charger_gares(
    boite: tuple[float, float, float, float] | None,
    autoriser_reseau: bool = True,
) -> tuple[list[dict[str, Any]], str | None]:
    """Gares de la zone etudiee : train, RER, metro, tramway, en service ou a venir.

    Telechargees une seule fois, puis memorisees dans `data/geo/gares.json`
    avec la liste des emprises deja couvertes. Tant que l'emprise demandee
    tient dans ce qui a ete telecharge, aucune requete ne part ; elargir le
    territoire ne retelecharge que la nouvelle emprise, fusionnee au fichier.

    Chaque gare porte son `statut` — en service, en travaux ou en projet — et
    les `lignes` qui la desservent quand la source les connait. Les gares a
    venir s'ajoutent a celles en service, et c'est l'appelant qui decide de les
    montrer ou non.

    Renvoie `(gares, message)`. Plusieurs sources sont tentees : le referentiel
    francilien, puis OpenStreetMap — dont les miroirs publics appliquent des
    quotas et repondent souvent `429` — puis SNCF Open Data. Une source qui ne
    repond pas **completement** est comptee en echec : une liste tronquee serait
    ecrite sur disque et l'emprise marquee couverte, donc jamais reprise. Si
    toutes echouent, la liste est vide et le message explique pourquoi :
    l'application continue de s'afficher.
    """

    if not boite:
        return [], None

    connues, emprises = _lire_fichier_gares()
    dans_emprise = [gare for gare in connues if _dans_emprise(gare, boite)]

    if _emprise_couverte(boite, emprises):
        return dans_emprise, (
            f"{len(dans_emprise)} gare(s) en mémoire (`data/geo/gares.json`), "
            "aucun appel réseau."
        )

    if not autoriser_reseau:
        message = "Récupération des gares désactivée."
        if dans_emprise:
            message = (
                f"{len(dans_emprise)} gare(s) déjà en mémoire ; le reste de l'emprise "
                "nécessiterait un appel réseau, désactivé."
            )
        return dans_emprise, message
    if requests is None:
        return dans_emprise, "Module `requests` absent : gares non récupérées."

    echecs: list[str] = []
    for libelle, fournisseur in _fournisseurs_gares(boite):
        try:
            recuperees = fournisseur(boite)
        except Exception as erreur:  # noqa: BLE001 - contrainte : jamais bloquant
            echecs.append(f"{libelle} : {_resumer_erreur(erreur)}")
            continue
        if not recuperees:
            echecs.append(f"{libelle} : aucune gare dans l'emprise")
            continue

        # Les gares a venir sont un supplement : leur source peut manquer sans
        # priver la carte du reseau d'aujourd'hui.
        projets: list[dict[str, Any]] = []
        for libelle_projets, fournisseur_projets in _fournisseurs_projets(boite):
            try:
                projets = fournisseur_projets(boite)
            except Exception as erreur:  # noqa: BLE001 - contrainte : jamais bloquant
                echecs.append(f"{libelle_projets} : {_resumer_erreur(erreur)}")

        fusionnees = _dedoublonner_gares(connues + recuperees + projets)
        _ecrire_fichier_gares(fusionnees, _fusionner_emprises(emprises, boite))
        retenues = [gare for gare in fusionnees if _dans_emprise(gare, boite)]
        a_venir = sum(1 for gare in retenues if gare["statut"] != STATUT_SERVICE)

        message = (
            f"{len(retenues)} gare(s) récupérée(s) via {libelle}, "
            "mémorisées dans `data/geo/gares.json` (téléchargement unique)."
        )
        if a_venir:
            message += f" Dont {a_venir} à venir (travaux ou projet)."
        if echecs:
            message += " Sources écartées — " + " ; ".join(echecs) + "."
        return retenues, message

    message = (
        "Gares indisponibles (" + " ; ".join(echecs) + "). "
        "La carte s'affiche sans les gares ; réessayez plus tard, les serveurs "
        "publics limitent le nombre de requêtes."
    )
    if dans_emprise:
        message = f"{len(dans_emprise)} gare(s) en mémoire ; " + message
    return dans_emprise, message


#: Part du reseau en service connu qu'une actualisation doit retrouver pour
#: remplacer le fichier. En deca, la reponse est tenue pour tronquee (un
#: incident de la source, une pagination coupee) et le fichier est garde tel
#: quel : un reseau a trous serait pire qu'un reseau vieux de quelques mois.
SEUIL_ACTUALISATION_GARES = 0.9


@dataclass
class ActualisationGares:
    """Ce qu'une actualisation du reseau a change, pour la pull request."""

    ecrit: bool = False
    avant: int = 0
    apres: int = 0
    #: « Nom — ligne » des lignes qui etaient a venir et ne le sont plus.
    ouvertes: list[str] = field(default_factory=list)
    #: « Nom — ligne » des lignes nouvellement annoncees.
    annoncees: list[str] = field(default_factory=list)
    #: Noms des gares en service apparues ou disparues.
    apparues: list[str] = field(default_factory=list)
    disparues: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def change(self) -> bool:
        return bool(self.ouvertes or self.annoncees or self.apparues or self.disparues)


def _projets_de(gares: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Les lignes a venir d'un reseau connu, en enregistrements de projet.

    Sert de repli quand la source des projets ne repond pas : les annonces
    deja connues sont gardees, et la reconciliation retire celles que le
    nouveau reseau en service dessert deja.
    """

    projets = []
    for gare in gares:
        _, a_venir = _dessertes(gare)
        if a_venir:
            statut = gare["statut"] if gare["statut"] != STATUT_SERVICE else STATUT_TRAVAUX
            projets.append({**gare, "statut": statut, "lignes": a_venir, "lignes_a_venir": []})
    return projets


def _paires(gares: list[dict[str, Any]], a_venir: bool) -> set[str]:
    paires = set()
    for gare in gares:
        en_service, futures = _dessertes(gare)
        for ligne in futures if a_venir else en_service or [""]:
            paires.add(f"{gare['nom']} — {ligne}" if ligne else gare["nom"])
    return paires


def actualiser_gares() -> ActualisationGares:
    """Retelecharge le reseau de chaque emprise memorisee, et remplace le fichier.

    `charger_gares` ne telecharge qu'une fois : c'est ce qui rend la
    construction du site independante du reseau. Mais une gare ouverte depuis
    reste alors « a venir » pour toujours. Cette fonction, lancee chaque mois
    par le workflow `reseau.yml`, remplace les gares de chaque emprise par
    celles que publient **aujourd'hui** les sources, reconciliees
    (`_reconcilier_mises_en_service`).

    Deux precautions :

    - seule la **premiere** source des gares en service est interrogee (en
      Île-de-France, le referentiel d'IDFM). Si elle echoue, rien n'est
      remplace : se rabattre sur OpenStreetMap renommerait la moitie des
      gares sans qu'aucune n'ait change ;
    - une reponse qui retrouve moins de `SEUIL_ACTUALISATION_GARES` du reseau
      en service connu est tenue pour tronquee, et ignoree.

    Les projets, eux, peuvent manquer : les annonces deja connues sont alors
    gardees, et seules celles qui roulent desormais disparaissent.
    """

    rapport = ActualisationGares()
    connues, emprises = _lire_fichier_gares()
    rapport.avant = len(connues)
    if not emprises:
        rapport.messages.append(
            "`data/geo/gares.json` ne memorise aucune emprise : rien a actualiser."
        )
        return rapport
    if requests is None:
        rapport.messages.append("Module `requests` absent : gares non actualisees.")
        return rapport

    reseau = list(connues)
    for emprise in emprises:
        boite = tuple(emprise)
        dedans = [g for g in reseau if _dans_emprise(g, boite, marge=0.0)]
        dehors = [g for g in reseau if not _dans_emprise(g, boite, marge=0.0)]
        libelle, fournisseur = _fournisseurs_gares(boite)[0]
        try:
            en_service = _dedoublonner_gares(fournisseur(boite))
        except Exception as erreur:  # noqa: BLE001 - contrainte : jamais bloquant
            rapport.messages.append(f"{libelle} : {_resumer_erreur(erreur)} ; emprise gardee telle quelle.")
            continue
        avant = sum(1 for g in dedans if g["statut"] == STATUT_SERVICE)
        if len(en_service) < SEUIL_ACTUALISATION_GARES * avant:
            rapport.messages.append(
                f"{libelle} : {len(en_service)} gares en service contre {avant} connues ; "
                "reponse tenue pour tronquee, emprise gardee telle quelle."
            )
            continue

        projets = _projets_de(dedans)
        for libelle_projets, fournisseur_projets in _fournisseurs_projets(boite):
            try:
                projets = fournisseur_projets(boite)
            except Exception as erreur:  # noqa: BLE001 - contrainte : jamais bloquant
                rapport.messages.append(
                    f"{libelle_projets} : {_resumer_erreur(erreur)} ; annonces connues gardees."
                )
        reseau = dehors + _dedoublonner_gares(en_service + projets)
        rapport.messages.append(
            f"{libelle} : {len(en_service)} gares en service, {len(projets)} arrets a venir."
        )

    reseau = _dedoublonner_gares(reseau)
    rapport.apres = len(reseau)
    rapport.ouvertes = sorted(_paires(connues, True) - _paires(reseau, True))
    rapport.annoncees = sorted(_paires(reseau, True) - _paires(connues, True))
    service_avant = {g["nom"] for g in connues if g["statut"] == STATUT_SERVICE}
    service_apres = {g["nom"] for g in reseau if g["statut"] == STATUT_SERVICE}
    rapport.apparues = sorted(service_apres - service_avant)
    rapport.disparues = sorted(service_avant - service_apres)
    par_nom = lambda gares: sorted(gares, key=lambda g: (g["nom"], g["latitude"], g["longitude"]))  # noqa: E731
    if par_nom(reseau) != par_nom(connues):
        _ecrire_fichier_gares(reseau, emprises)
        rapport.ecrit = True
    return rapport


# --------------------------------------------------------------------------
# Simplification des contours
# --------------------------------------------------------------------------
#
# Une section cadastrale brute pese en moyenne 1,7 Ko de GeoJSON. Sur les dix
# communes historiques du depot (417 sections) c'est indolore ; sur l'unite
# urbaine de Paris (de l'ordre de 16 000 sections) cela represente une
# trentaine de Mo a versionner, a charger et a dessiner. Or le cadastre decrit
# les limites au centimetre, alors que la carte les affiche au mieux au metre.
#
# Douglas-Peucker retire les sommets qui ne changent pas le trace au-dela d'une
# tolerance donnee. Il est implemente ici plutot qu'importe : `src/geo.py`
# n'a deliberement aucune dependance geospatiale (point-dans-polygone compris),
# et une dependance de plus serait payee par tous les deploiements pour une
# trentaine de lignes.

#: Degres de latitude par metre. La longitude est corrigee par le cosinus de la
#: latitude : a Paris (48,8 N) un degre de longitude vaut ~73 km contre 111 km
#: pour un degre de latitude, et simplifier sans cette correction ecraserait
#: les contours dans le sens est-ouest.
_DEGRES_PAR_METRE = 1.0 / 111_320.0


def _distance_au_segment(point, debut, fin, facteur_lon: float) -> float:
    """Distance perpendiculaire d'un point a un segment, en degres corriges."""

    (px, py), (ax, ay), (bx, by) = point, debut, fin
    px, ax, bx = px * facteur_lon, ax * facteur_lon, bx * facteur_lon

    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    position = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    position = max(0.0, min(1.0, position))
    return math.hypot(px - (ax + position * dx), py - (ay + position * dy))


def _douglas_peucker(points: list, tolerance: float, facteur_lon: float) -> list:
    if len(points) < 3:
        return list(points)

    pire, indice = 0.0, 0
    for i in range(1, len(points) - 1):
        ecart = _distance_au_segment(points[i], points[0], points[-1], facteur_lon)
        if ecart > pire:
            pire, indice = ecart, i

    if pire <= tolerance:
        return [points[0], points[-1]]
    gauche = _douglas_peucker(points[: indice + 1], tolerance, facteur_lon)
    droite = _douglas_peucker(points[indice:], tolerance, facteur_lon)
    return gauche[:-1] + droite


def _simplifier_anneau(anneau: list, tolerance: float, facteur_lon: float) -> list:
    """Simplifie un anneau ferme en preservant sa fermeture.

    Un anneau reduit a moins de quatre sommets ne decrit plus une surface : on
    rend alors l'anneau non simplifie plutot qu'un polygone degenere, qui se
    dessinerait comme un trait.
    """

    if len(anneau) < 4:
        return anneau
    simplifie = _douglas_peucker([tuple(p[:2]) for p in anneau], tolerance, facteur_lon)
    if len(simplifie) < 4:
        return anneau
    if simplifie[0] != simplifie[-1]:
        simplifie.append(simplifie[0])
    return [list(p) for p in simplifie]


def simplifier_geometrie(geometrie: dict[str, Any], tolerance_metres: float = 2.0) -> dict[str, Any]:
    """Allege une geometrie GeoJSON sans deplacer son trace au-dela de la tolerance."""

    if not geometrie or tolerance_metres <= 0:
        return geometrie
    type_geo = geometrie.get("type")
    coordonnees = geometrie.get("coordinates")
    if type_geo not in {"Polygon", "MultiPolygon"} or not coordonnees:
        return geometrie

    latitudes = [
        point[1]
        for polygone in (coordonnees if type_geo == "MultiPolygon" else [coordonnees])
        for anneau in polygone
        for point in anneau
        if len(point) >= 2
    ]
    if not latitudes:
        return geometrie
    latitude_moyenne = sum(latitudes) / len(latitudes)
    facteur_lon = max(math.cos(math.radians(latitude_moyenne)), 0.05)
    tolerance = tolerance_metres * _DEGRES_PAR_METRE

    def simplifier_polygone(polygone: list) -> list:
        return [_simplifier_anneau(anneau, tolerance, facteur_lon) for anneau in polygone]

    if type_geo == "Polygon":
        nouvelles = simplifier_polygone(coordonnees)
    else:
        nouvelles = [simplifier_polygone(polygone) for polygone in coordonnees]
    return {**geometrie, "coordinates": nouvelles}


def simplifier_entites(
    entites: list[dict[str, Any]], tolerance_metres: float = 2.0
) -> list[dict[str, Any]]:
    """Simplifie une liste d'entites GeoJSON, proprietes inchangees."""

    return [
        {**entite, "geometry": simplifier_geometrie(entite.get("geometry") or {}, tolerance_metres)}
        for entite in entites
    ]


def compter_sommets(entites: Iterable[dict[str, Any]]) -> int:
    """Nombre total de sommets : la mesure qui compte pour le poids et le rendu."""

    total = 0

    def parcourir(coordonnees: Any) -> None:
        nonlocal total
        if isinstance(coordonnees, (list, tuple)):
            if coordonnees and isinstance(coordonnees[0], (int, float)):
                total += 1
            else:
                for element in coordonnees:
                    parcourir(element)

    for entite in entites:
        parcourir((entite.get("geometry") or {}).get("coordinates"))
    return total


# --------------------------------------------------------------------------
# Contours de communes : le niveau large de la carte
# --------------------------------------------------------------------------
#
# Dessiner 16 000 sections d'un seul tenant fige le navigateur, et n'aurait
# aucun sens visuel : a l'echelle d'une agglomeration, une section fait
# quelques pixels. La carte a donc deux niveaux — communes quand on est
# dezoome, sections des qu'on entre dans le detail — et ce bloc fournit le
# niveau large.

#: Contour communal du meme socle Etalab que les sections.
URL_COMMUNES_ETALAB = (
    "https://cadastre.data.gouv.fr/data/etalab-cadastre/latest/geojson/communes/"
    "{departement}/{insee}/cadastre-{insee}-communes.json.gz"
)


def repertoire_communes() -> str:
    chemin = os.path.join(repertoire_geo(), "communes")
    os.makedirs(chemin, exist_ok=True)
    return chemin


def fichier_communes_territoire(cle: str) -> str:
    """Contours des communes d'un territoire, en un seul fichier versionne."""

    return os.path.join(repertoire_communes(), f"{cle}.geojson")


#: Tolerance de simplification des contours servis a la carte, en metres. Une
#: section cadastrale fait quelques centaines de metres : a 8 m, le trace
#: reste juste a l'ecran tout en divisant le poids du fichier par trois ou
#: quatre.
TOLERANCE_CARTE = 8.0


def contours_carte(
    entites: list[dict[str, Any]], cle: str, tolerance_metres: float = TOLERANCE_CARTE
) -> dict[str, Any]:
    """Contours tels que la carte les recoit : simplifies, et leur code seul.

    `c` et rien d'autre : les proprietes utiles (couleur, chiffres) sont
    posees cote navigateur, dans l'etat de chaque entite.
    """

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"c": entite["properties"][cle]},
                "geometry": simplifier_geometrie(entite["geometry"], tolerance_metres),
            }
            for entite in entites
            if entite.get("properties", {}).get(cle) and entite.get("geometry")
        ],
    }


def charger_communes_territoire(cle: str) -> dict[str, Any]:
    """Contours communaux d'un territoire, lus localement — jamais de reseau.

    Ce niveau est petit (quelques centaines d'entites simplifiees) et il est
    affiche des le premier ecran : il doit etre dans le depot, pas au bout
    d'une API.
    """

    chemin = fichier_communes_territoire(cle)
    try:
        with open(chemin, "r", encoding="utf-8") as flux:
            collection = json.load(flux)
    except (OSError, json.JSONDecodeError):
        return {"type": "FeatureCollection", "features": []}

    entites = []
    for entite in collection.get("features", []):
        proprietes = dict(entite.get("properties") or {})
        code = str(
            proprietes.get("code_commune") or proprietes.get("commune") or proprietes.get("id") or ""
        ).zfill(5)
        if not code.strip("0"):
            continue
        proprietes["code_commune"] = code
        entites.append({**entite, "properties": proprietes})
    return {"type": "FeatureCollection", "features": entites}
