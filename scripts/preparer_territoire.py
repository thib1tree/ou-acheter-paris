#!/usr/bin/env python3
"""Fabrique les donnees d'un territoire : mutations figees et contours allegés.

L'application ne telecharge rien au demarrage pour son territoire par defaut :
tout est prepare ici, une fois, puis versionne.

    # Le Grand Paris, de bout en bout (liste de communes, DVF, contours)
    python scripts/preparer_territoire.py --grand-paris --territoire grand-paris

    # Une region, sur les seules annees voulues
    python scripts/preparer_territoire.py --territoire region-53 --annees 2022-2025

    # Regenerer le referentiel des communes depuis npm (hors ligne ensuite)
    python scripts/preparer_territoire.py --referentiel

Ce que le script produit :

  data/territoires/<cle>/mutations.parquet   une ligne par vente, toutes natures
  data/territoires/<cle>/preparation.json    comptages amont et provenance
  data/geo/<dep>/<insee>-sections.geojson    contours de sections simplifies
  data/geo/communes/<cle>.geojson            contours des communes (vue large)

Le parquet ne fige que l'etape couteuse — reconstruction des ventes a partir
des lignes DVF. Tous les filtres de qualite restent appliques a chaque
interaction, donc ajustables depuis la barre laterale.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from src import geo, millesime, territoires
from src.ingestion import (
    OptionsNettoyage,
    RapportQualite,
    ecrire_territoire,
    reconstruire_mutations,
)
from src.telechargement import (
    TelechargementImpossible,
    annees_disponibles,
    lignes_departement,
)

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

#: Jeu data.gouv.fr « Liste des communes de France — 62 indicateurs », seule
#: source ouverte portant le rattachement commune -> unite urbaine.
DATASET_COMMUNES = "6745d9ae4524d845d2138193"
API_DATASET = "https://www.data.gouv.fr/api/1/datasets/{identifiant}/"

#: Unite urbaine de Paris au sens de l'INSEE : « Paris et son agglomeration »,
#: c'est-a-dire la continuite du bati, et non un perimetre institutionnel.
CODE_UU_PARIS = "00851"

DELAI = 120
ENTETES = {"User-Agent": "cartographie-dvf/1.0"}


def journal(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------
# Liste des communes du Grand Paris
# --------------------------------------------------------------------------


def _communes_plm() -> dict[str, list[str]]:
    """Arrondissements municipaux, indexes par leur commune mere.

    Les DVF ventilent Paris, Lyon et Marseille par arrondissement (`75101`)
    alors que les referentiels de zonage raisonnent en commune (`75056`) : sans
    cette correspondance, le territoire par defaut serait vide a Paris.
    """

    referentiel = territoires.referentiel_communes()
    meres = {"75": "75056", "69": "69123", "13": "13055"}
    resultat: dict[str, list[str]] = {}
    for departement, mere in meres.items():
        enfants = referentiel[
            referentiel["code_commune"].str.match(rf"^{departement}1\d\d$")
        ]["code_commune"].tolist()
        if enfants:
            resultat[mere] = sorted(enfants)
    return resultat


def communes_unite_urbaine(table: pd.DataFrame, code_uu: str = CODE_UU_PARIS) -> list[str]:
    """Codes INSEE d'une unite urbaine, arrondissements developpes."""

    colonnes = {c.lower(): c for c in table.columns}
    colonne_uu = colonnes.get("code_unite_urbaine") or colonnes.get("code_uu")
    colonne_insee = (
        colonnes.get("code_commune_insee")
        or colonnes.get("code_insee")
        or colonnes.get("code_commune")
        or colonnes.get("com")
    )
    if not colonne_uu or not colonne_insee:
        raise ValueError(
            "Le fichier ne porte pas les colonnes attendues (code unite urbaine et "
            f"code commune). Colonnes vues : {sorted(table.columns)[:12]}…"
        )

    retenues = table[table[colonne_uu].astype(str).str.strip().str.zfill(5) == code_uu]
    codes = {str(c).strip().zfill(5) for c in retenues[colonne_insee]}

    plm = _communes_plm()
    for mere, enfants in plm.items():
        if mere in codes:
            codes.discard(mere)
            codes.update(enfants)

    connues = set(territoires.referentiel_communes()["code_commune"])
    return sorted(codes & connues)


def _telecharger_table_communes() -> pd.DataFrame:
    """Recupere la table des communes via l'API data.gouv.fr.

    On passe par l'API plutot que par une URL de fichier en dur : les URL de
    ressources changent a chaque nouveau millesime, l'identifiant du jeu de
    donnees, non.
    """

    if requests is None:
        raise TelechargementImpossible("Module `requests` absent.")

    reponse = requests.get(
        API_DATASET.format(identifiant=DATASET_COMMUNES), timeout=DELAI, headers=ENTETES
    )
    reponse.raise_for_status()
    ressources = reponse.json().get("resources", [])

    candidates = [
        r for r in ressources
        if str(r.get("url", "")).endswith((".csv", ".csv.gz"))
        and "commune" in str(r.get("title", "")).lower()
    ] or [r for r in ressources if str(r.get("url", "")).endswith((".csv", ".csv.gz"))]
    if not candidates:
        raise TelechargementImpossible(
            "Aucune ressource CSV dans le jeu de donnees des communes. "
            "Telechargez-la a la main et relancez avec --depuis <fichier>."
        )

    url = candidates[0]["url"]
    journal(f"    ressource : {candidates[0].get('title') or url}")
    contenu = requests.get(url, timeout=DELAI, headers=ENTETES).content
    if contenu[:2] == b"\x1f\x8b":
        contenu = gzip.decompress(contenu)
    return _lire_table(io.BytesIO(contenu))


def _lire_table(source) -> pd.DataFrame:
    """Lit un CSV dont le separateur n'est pas connu d'avance."""

    donnees = source.read() if hasattr(source, "read") else open(source, "rb").read()
    if donnees[:2] == b"\x1f\x8b":
        donnees = gzip.decompress(donnees)
    if donnees[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(donnees)) as archive:
            nom = next(n for n in archive.namelist() if n.lower().endswith((".csv", ".txt")))
            donnees = archive.read(nom)
    entete = donnees.split(b"\n", 1)[0].decode("utf-8-sig", errors="replace")
    separateur = max(",;\t|", key=entete.count)
    return pd.read_csv(io.BytesIO(donnees), sep=separateur, dtype=str, encoding="utf-8-sig")


def ecrire_grand_paris(codes: list[str]) -> str:
    referentiel = territoires.referentiel_communes().set_index("code_commune")
    chemin = territoires.fichier_grand_paris()
    with open(chemin, "w", encoding="utf-8") as flux:
        flux.write("# Grand Paris — communes du territoire par defaut.\n")
        flux.write(
            f"# Perimetre : unite urbaine de Paris (INSEE UU2020-{CODE_UU_PARIS}), "
            "c'est-a-dire\n# Paris et la continuite du bati qui l'entoure. Paris, Lyon et "
            "Marseille y sont\n# ventiles par arrondissement, comme les DVF.\n"
        )
        flux.write("# Genere par `python scripts/preparer_territoire.py --grand-paris`.\n")
        for code in codes:
            nom = referentiel.loc[code, "nom_commune"] if code in referentiel.index else ""
            flux.write(f"{code}\t{nom}\n")
    return chemin


# --------------------------------------------------------------------------
# Referentiel des communes (npm)
# --------------------------------------------------------------------------


def regenerer_referentiel(source: str) -> str:
    """Reconstruit `data/territoires/communes.csv.gz` depuis decoupage-administratif.

    `source` est le repertoire `data/` du paquet `@etalab/decoupage-administratif`
    (recuperable par `npm pack @etalab/decoupage-administratif`).
    """

    communes = json.load(open(os.path.join(source, "communes.json"), encoding="utf-8"))
    regions = {
        r["code"]: r["nom"]
        for r in json.load(open(os.path.join(source, "regions.json"), encoding="utf-8"))
    }

    arrondissements = [c for c in communes if c["type"] == "arrondissement-municipal"]
    meres = {c["commune"] for c in arrondissements}
    # Paris, Lyon et Marseille sont remplacees par leurs arrondissements : c'est
    # la maille des DVF, et la commune mere n'y apparait jamais.
    retenues = sorted(
        (
            c for c in communes
            if c["type"] == "arrondissement-municipal"
            or (c["type"] == "commune-actuelle" and c["code"] not in meres)
        ),
        key=lambda c: c["code"],
    )

    chemin = territoires.fichier_communes()
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with gzip.open(chemin, "wt", encoding="utf-8", newline="", compresslevel=9) as flux:
        ecrivain = csv.writer(flux, delimiter=";")
        ecrivain.writerow(
            ["code_commune", "nom_commune", "code_departement", "code_region", "nom_region", "population"]
        )
        for commune in retenues:
            ecrivain.writerow([
                commune["code"], commune["nom"], commune["departement"], commune["region"],
                regions.get(commune["region"], ""), commune.get("population") or 0,
            ])
    journal(f"  {len(retenues)} communes ecrites dans {chemin}")
    return chemin


# --------------------------------------------------------------------------
# Mutations d'un territoire
# --------------------------------------------------------------------------


def preparer_mutations(cle: str, annees: list[int]) -> pd.DataFrame:
    perimetre = territoires.territoire(cle)
    journal(
        f"  {perimetre.nom} : {perimetre.nb_communes} communes, "
        f"{len(perimetre.departements)} departements"
    )
    if perimetre.departements_sans_dvf:
        journal(
            "  ⚠ departements hors champ DVF (livre foncier de droit local, Mayotte) : "
            + ", ".join(perimetre.departements_sans_dvf)
        )

    morceaux: list[pd.DataFrame] = []
    fichiers: list[str] = []
    for departement in perimetre.departements:
        if departement in territoires.DEPARTEMENTS_SANS_DVF:
            continue
        millesimes = annees_disponibles(departement, annees) or annees
        for annee in millesimes:
            try:
                lignes = lignes_departement(departement, annee, perimetre.codes_communes)
            except TelechargementImpossible as erreur:
                journal(f"    ⚠ {erreur}")
                continue
            if lignes.empty:
                continue
            morceaux.append(lignes)
            fichiers.append(f"{annee}-{departement}.csv.gz")
            journal(f"    {departement} / {annee} : {len(lignes):>7} lignes")

    if not morceaux:
        raise SystemExit(
            f"Aucune ligne DVF recuperee pour « {cle} ». Verifiez l'acces reseau a "
            "files.data.gouv.fr."
        )

    brut = pd.concat(morceaux, ignore_index=True)
    rapport = RapportQualite(fichiers=fichiers)
    rapport.lignes_brutes = len(brut)

    # Toutes natures conservees : le filtre « natures de mutation » doit rester
    # vivant dans la barre laterale, il ne peut donc pas etre applique ici.
    mutations = reconstruire_mutations(brut, OptionsNettoyage(natures_mutation=None), rapport)
    journal(f"  {len(brut)} lignes -> {len(mutations)} ventes reconstituees")

    chemin = ecrire_territoire(
        cle, mutations, rapport,
        {
            "annees": sorted({int(a) for a in mutations["annee"].dropna().unique()}),
            "communes": sorted(mutations["code_commune"].dropna().unique().tolist()),
            "sections": int(mutations["code_section"].nunique()),
            "source": "https://files.data.gouv.fr/geo-dvf/latest/csv/",
        },
    )
    journal(f"  parquet : {chemin} ({os.path.getsize(chemin) / 1_048_576:.1f} Mo)")
    return mutations


# --------------------------------------------------------------------------
# Contours
# --------------------------------------------------------------------------


def _telecharger_geojson(url: str) -> dict | None:
    if requests is None:
        return None
    try:
        reponse = requests.get(url, timeout=DELAI, headers=ENTETES)
        reponse.raise_for_status()
        contenu = reponse.content
        if contenu[:2] == b"\x1f\x8b":
            contenu = gzip.decompress(contenu)
        return json.loads(contenu.decode("utf-8"))
    except Exception:  # noqa: BLE001 - une commune manquante n'arrete pas le lot
        return None


def preparer_contours(cle: str, codes_communes: list[str], tolerance: float) -> None:
    """Sections simplifiees, commune par commune, plus le contour communal."""

    entites_communes: list[dict] = []
    sommets_avant = sommets_apres = 0
    manquantes: list[str] = []
    communes_manquantes: list[str] = []

    for rang, insee in enumerate(sorted(codes_communes), start=1):
        departement = geo.departement_de(insee)
        destination = geo.fichier_contours(insee)
        os.makedirs(os.path.dirname(destination), exist_ok=True)

        if not os.path.exists(destination):
            collection = _telecharger_geojson(
                geo.URL_SECTIONS_ETALAB.format(departement=departement, insee=insee)
            )
            if collection is None:
                manquantes.append(insee)
            else:
                entites = geo._normaliser_collection(collection, insee)
                sommets_avant += geo.compter_sommets(entites)
                entites = geo.simplifier_entites(entites, tolerance)
                sommets_apres += geo.compter_sommets(entites)
                with open(destination, "w", encoding="utf-8") as flux:
                    json.dump(
                        {"type": "FeatureCollection", "features": entites},
                        flux, separators=(",", ":"),
                    )

        contour = _telecharger_geojson(
            geo.URL_COMMUNES_ETALAB.format(departement=departement, insee=insee)
        )
        if not contour:
            communes_manquantes.append(insee)
        if contour:
            for entite in contour.get("features", []):
                proprietes = dict(entite.get("properties") or {})
                proprietes["code_commune"] = insee
                entites_communes.append({
                    "type": "Feature",
                    "properties": proprietes,
                    "geometry": geo.simplifier_geometrie(
                        entite.get("geometry") or {}, max(tolerance, 10.0)
                    ),
                })

        if rang % 25 == 0 or rang == len(codes_communes):
            journal(f"    contours : {rang}/{len(codes_communes)} communes")

    chemin = geo.fichier_communes_territoire(cle)
    if communes_manquantes and os.path.exists(chemin):
        # Un serveur du cadastre indisponible ne doit pas remplacer des
        # contours complets par une carte trouee : on garde les anciens.
        journal(f"  ⚠ contour communal indisponible pour {len(communes_manquantes)} commune(s) "
                f"({', '.join(communes_manquantes[:10])}) : {chemin} est conserve tel quel.")
    elif entites_communes:
        with open(chemin, "w", encoding="utf-8") as flux:
            json.dump(
                {"type": "FeatureCollection", "features": entites_communes},
                flux, separators=(",", ":"),
            )
        journal(f"  contours communaux : {chemin} ({os.path.getsize(chemin) / 1024:.0f} Ko)")

    if sommets_avant:
        journal(
            f"  simplification a {tolerance:g} m : {sommets_avant} -> {sommets_apres} sommets "
            f"({100 - 100 * sommets_apres / sommets_avant:.0f} % retires)"
        )
    if manquantes:
        journal(f"  ⚠ contours indisponibles pour {len(manquantes)} commune(s) : "
                + ", ".join(manquantes[:10]) + ("…" if len(manquantes) > 10 else ""))


# --------------------------------------------------------------------------
# Entree
# --------------------------------------------------------------------------


def _annees(expression: str) -> list[int]:
    if "-" in expression:
        debut, fin = expression.split("-", 1)
        return list(range(int(debut), int(fin) + 1))
    return [int(a) for a in expression.split(",") if a.strip()]


def main() -> int:
    analyseur = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    analyseur.add_argument("--territoire", help="cle du territoire (ex. grand-paris, region-53)")
    analyseur.add_argument(
        "--annees", default="auto",
        help="millesimes a couvrir, ex. 2021-2025 ou 2023,2024 ; par defaut, les cinq "
        "dernieres annees completes publiees par Etalab, plus l'annee en cours si elle "
        "l'est (voir src/millesime.py)",
    )
    analyseur.add_argument("--grand-paris", action="store_true",
                           help="regenere la liste des communes de l'unite urbaine de Paris")
    analyseur.add_argument("--depuis", help="fichier local a utiliser au lieu du telechargement")
    analyseur.add_argument("--referentiel", metavar="REPERTOIRE", nargs="?", const="",
                           help="regenere le referentiel depuis le paquet decoupage-administratif")
    analyseur.add_argument("--sans-contours", action="store_true",
                           help="ne prepare que les mutations")
    analyseur.add_argument("--tolerance", type=float, default=2.0,
                           help="tolerance de simplification des sections, en metres (2 par defaut)")
    arguments = analyseur.parse_args()

    if arguments.referentiel is not None:
        source = arguments.referentiel or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "node_modules", "@etalab", "decoupage-administratif", "data",
        )
        journal(f"→ Referentiel des communes depuis {source}")
        regenerer_referentiel(source)
        territoires.referentiel_communes.cache_clear()

    if arguments.grand_paris:
        journal("→ Communes de l'unite urbaine de Paris")
        table = _lire_table(arguments.depuis) if arguments.depuis else _telecharger_table_communes()
        codes = communes_unite_urbaine(table)
        chemin = ecrire_grand_paris(codes)
        journal(f"  {len(codes)} communes ecrites dans {chemin}")
        territoires._codes_grand_paris.cache_clear()
        territoires.territoires.cache_clear()

    if arguments.territoire:
        if arguments.annees == "auto":
            departements = [
                d for d in territoires.territoire(arguments.territoire).departements
                if d not in territoires.DEPARTEMENTS_SANS_DVF
            ]
            annees = millesime.interroger(departements).annees
        else:
            annees = _annees(arguments.annees)
        journal(f"→ Territoire « {arguments.territoire} », millesimes {annees[0]}-{annees[-1]}")
        mutations = preparer_mutations(arguments.territoire, annees)
        if not arguments.sans_contours:
            journal("→ Contours cadastraux")
            preparer_contours(
                arguments.territoire,
                sorted(mutations["code_commune"].dropna().unique().tolist()),
                arguments.tolerance,
            )

    if not (arguments.territoire or arguments.grand_paris or arguments.referentiel is not None):
        analyseur.print_help()
        return 1

    journal("Termine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
