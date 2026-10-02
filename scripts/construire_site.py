#!/usr/bin/env python3
"""Construit le site statique dans `dist/` : tout ce que Cloudflare Pages sert.

    python scripts/construire_site.py [--territoire grand-paris] [--sortie dist]

Le site n'a pas de serveur : Python ne sert qu'ici, a preparer des fichiers.
Tout part du territoire precalcule (`data/territoires/<cle>/mutations.parquet`)
et des contours et gares versionnes dans `data/geo/`. Aucun appel reseau.

Ce que le script ecrit :

    index.html, site.css, app.js, calcul.js,   la page et la carte MapLibre,
    calcul-worker.js, carte/                   copiees telles quelles de `site/`
    mentions-legales.html, textes.css          mentions legales et confidentialite
    manifeste.json                             ce que la page lit en premier
    donnees/                                   les donnees, nommees par leur empreinte
      sections-*.json, communes-*.json         contours simplifies
      gares-*.json, etiquettes-*.json          reperes
      voies-*.json                             voies dominantes de chaque zone
      ventes-*.bin, ventes-*.json              ventes en colonnes binaires (gzip)
      defaut-*.json                            statistiques des filtres d'ouverture
      dvf-pts-*.json                           ventes individuelles, en tuiles
    _headers                                   en-tetes HTTP de Cloudflare Pages
    robots.txt, sitemap.xml                    referencement (le plan, si l'adresse
                                               publique est connue : --adresse)

Chaque fichier de donnees porte l'empreinte de son contenu : il se met en
cache pour toujours, et une nouvelle version porte un autre nom. Seuls la page
et le manifeste sont revalides a chaque visite — c'est le manifeste qui
designe les donnees du jour.

Le script verifie enfin les limites de Cloudflare Pages et echoue s'il les
depasse (`verifier_limites`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

from src import binaire, charge, geo, points, territoires  # noqa: E402
from src.ingestion import (  # noqa: E402
    OptionsNettoyage,
    construire_transactions_territoire,
    repertoire_territoire,
    territoire_prepare,
)
from src.stats import CLE_COMMUNE, CLE_SECTION  # noqa: E402

#: Limites de Cloudflare Pages, offre gratuite
#: (https://developers.cloudflare.com/pages/platform/limits/) : 20 000
#: fichiers par site, 25 Mio par fichier.
PLAFOND_FICHIERS = 20_000
PLAFOND_TAILLE = 25 * 1024 * 1024

#: Regles de nettoyage des ventes (voir `OptionsNettoyage`).
OPTIONS_NETTOYAGE = OptionsNettoyage()

SITE = RACINE / "site"

#: Nom du site : titre de l'onglet et de l'en-tete, apercus de partage,
#: application ajoutee a l'ecran d'accueil. Il n'est ecrit qu'ici.
#:
#: Il dit a quoi sert la carte — choisir ou acheter — plutot que ce qu'elle
#: montre ; le sous-titre, dans la page, dit d'ou viennent les chiffres. Sur
#: telephone, il tient en entier jusqu'a une trentaine de caracteres.
NOM_SITE = "Où acheter autour de Paris ?"
#: Nom court, sous l'icone d'un telephone (une douzaine de caracteres).
NOM_COURT = "Où acheter ?"

#: Depot public du code, cite dans le pied de page et dans « À propos ».
URL_DEPOT = "https://github.com/thib1tree/ou-acheter-paris"

#: Contact de l'editeur, cite dans les mentions legales : c'est aussi l'adresse
#: des demandes relatives aux donnees personnelles (retrait d'une vente).
CONTACT = "contact@ou-acheter-paris.fr"

#: Pages du site dont les `{{cle}}` sont remplis a la construction.
PAGES = ("index.html", "mentions-legales.html")

#: Adresse de production, pour l'adresse canonique, les apercus de partage et
#: le plan du site. La variable `ADRESSE_SITE` la remplace si besoin.
ADRESSE_PRODUCTION = "https://www.ou-acheter-paris.fr/"

#: Politique de securite du contenu. Le site ne sert que ses propres fichiers,
#: et ne contacte que les serveurs de tuiles des fonds de carte :
#:
#: - aucun script en ligne ni venu d'ailleurs (`script-src 'self'`) : une
#:   injection de HTML dans une infobulle ne pourrait rien executer ;
#: - `worker-src blob:` : MapLibre cree son worker a partir d'un blob ;
#: - `img-src` / `connect-src` : les tuiles, que MapLibre telecharge par
#:   `fetch` puis decode en images ;
#: - ni cadre, ni formulaire, ni plugin, ni balise `<base>`.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "worker-src 'self' blob:",
    "child-src 'self' blob:",
    "img-src 'self' data: blob: " + " ".join(charge.ORIGINES_FONDS),
    "connect-src 'self' " + " ".join(charge.ORIGINES_FONDS),
    "font-src 'self'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
])

#: En-tetes HTTP. Les donnees portent leur empreinte : elles ne changent
#: jamais sous un meme nom, et le navigateur les garde un an sans redemander.
#: Tout le reste (page, scripts, manifeste) garde le comportement par defaut
#: de Cloudflare Pages : revalide a chaque visite, donc a jour des le
#: deploiement suivant.
#:
#: `X-Robots-Tag` sur `donnees/` : les conditions de reutilisation des DVF
#: interdisent de laisser les moteurs de recherche indexer les donnees (article
#: R112 A-3 du livre des procedures fiscales). La page, elle, ne contient
#: aucune vente, et peut etre indexee. `Referrer-Policy` reste
#: `strict-origin-when-cross-origin` : les serveurs de tuiles d'OpenStreetMap
#: exigent un `Referer`.
EN_TETES = f"""\
/donnees/*
  Cache-Control: public, max-age=31536000, immutable
  X-Robots-Tag: noindex, nofollow, noarchive

/manifeste.json
  X-Robots-Tag: noindex, nofollow, noarchive

/*
  Content-Security-Policy: {CSP}
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  X-Frame-Options: DENY
  Strict-Transport-Security: max-age=31536000
  Cross-Origin-Opener-Policy: same-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()
"""

#: Les robots peuvent lire la page, pas les donnees (voir `EN_TETES`).
ROBOTS = """\
User-agent: *
Disallow: /donnees/
Disallow: /manifeste.json
Allow: /
"""


class LimiteDepassee(RuntimeError):
    """Le site ne tiendrait pas dans Cloudflare Pages."""


def journal(message: str) -> None:
    print(message, flush=True)


def _empreinte(contenu: bytes) -> str:
    return hashlib.sha256(contenu).hexdigest()[:16]


def ecrire_donnee(dossier: Path, prefixe: str, contenu: bytes | str, extension: str = "json") -> str:
    """Ecrit un fichier de donnees nomme par son empreinte ; rend son chemin relatif."""

    if isinstance(contenu, str):
        contenu = contenu.encode("utf-8")
    nom = f"{prefixe}-{_empreinte(contenu)}.{extension}"
    (dossier / nom).write_bytes(contenu)
    return f"donnees/{nom}"


def _json(objet) -> str:
    return json.dumps(objet, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def verifier_limites(dossier: Path) -> tuple[int, int]:
    """Nombre de fichiers et taille du plus gros ; `LimiteDepassee` sinon."""

    fichiers = [f for f in dossier.rglob("*") if f.is_file()]
    if len(fichiers) > PLAFOND_FICHIERS:
        raise LimiteDepassee(
            f"{len(fichiers)} fichiers : Cloudflare Pages en accepte {PLAFOND_FICHIERS} au plus."
        )
    trop_gros = [f for f in fichiers if f.stat().st_size > PLAFOND_TAILLE]
    if trop_gros:
        raise LimiteDepassee(
            "Fichiers de plus de 25 Mio : "
            + ", ".join(str(f.relative_to(dossier)) for f in trop_gros)
        )
    return len(fichiers), max((f.stat().st_size for f in fichiers), default=0)


def copier_page(sortie: Path) -> None:
    """La page et la carte : des fichiers sources, copies tels quels."""

    shutil.copytree(SITE, sortie, dirs_exist_ok=True)


def _milliers(n: float) -> str:
    """12345 → « 12 345 », avec l'espace insecable fine du français."""

    return f"{n:,.0f}".replace(",", "\u202f")


def _date_fr(jour) -> str:
    return f"{jour:%d/%m/%Y}"


def ecrire_page(sortie: Path, valeurs: dict[str, str], adresse: str | None) -> None:
    """Remplit les `{{cle}}` des pages (`PAGES`) ; retire le bloc `adresse` si besoin.

    Le texte de « À propos » cite les regles de nettoyage et les chiffres du
    territoire : ils sont ecrits ici, depuis le code qui les applique, pour ne
    jamais diverger. Une cle oubliee fait echouer la construction.
    """

    for nom in PAGES:
        chemin = sortie / nom
        page = chemin.read_text(encoding="utf-8")
        debut, fin = "<!--adresse-->", "<!--/adresse-->"
        if debut in page:
            if adresse:
                page = page.replace(debut, "").replace(fin, "")
            else:
                page = page[: page.index(debut)] + page[page.index(fin) + len(fin):]
        for cle, valeur in {**valeurs, "adresse": adresse or ""}.items():
            page = page.replace("{{" + cle + "}}", valeur)
        if "{{" in page:
            restant = page[page.index("{{"):][:40]
            raise SystemExit(f"{nom} : valeur manquante pour {restant!r}")
        chemin.write_text(page, encoding="utf-8")

    manifeste_appli = sortie / "site.webmanifest"
    texte = manifeste_appli.read_text(encoding="utf-8")
    for cle in ("nom_site", "nom_court"):
        texte = texte.replace("{{" + cle + "}}", valeurs[cle])
    manifeste_appli.write_text(texte, encoding="utf-8")


def valeurs_page(cle: str, transactions, bornes) -> dict[str, str]:
    """Ce que `index.html` cite des donnees et des regles de nettoyage."""

    options = OPTIONS_NETTOYAGE
    nb_communes = int(transactions[CLE_COMMUNE].nunique())
    description = (
        "Pour choisir où acheter autour de Paris : les prix de vente réels des logements, "
        "par commune, quartier et vente, et les gares actuelles et à venir, d'après les "
        "demandes de valeurs foncières (DVF)."
    )
    json_ld = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": NOM_SITE,
        "description": description,
        "applicationCategory": "ReferenceApplication",
        "operatingSystem": "Tous (navigateur web)",
        "inLanguage": "fr",
        "isAccessibleForFree": True,
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "EUR"},
        "codeRepository": URL_DEPOT,
        "isBasedOn": {
            "@type": "Dataset",
            "name": "Demandes de valeurs foncières géolocalisées",
            "url": charge.URL_JEU_DE_DONNEES,
            "license": charge.URL_LICENCE,
            "creator": {"@type": "Organization", "name": "DGFiP / Etalab"},
        },
        "spatialCoverage": f"Unité urbaine de Paris ({nb_communes} communes)",
    }
    # `</` ne doit jamais fermer le bloc de script qui porte ce JSON.
    texte_ld = json.dumps(json_ld, ensure_ascii=False).replace("</", "<\\/")
    return {
        "nom_site": NOM_SITE,
        "nom_court": NOM_COURT,
        "url_jeu": charge.URL_JEU_DE_DONNEES,
        "url_licence": charge.URL_LICENCE,
        "url_depot": URL_DEPOT,
        "contact": CONTACT,
        "debut": _date_fr(bornes[0]) if bornes else "—",
        "annee_debut": f"{bornes[0]:%Y}" if bornes else "",
        "annee_fin": f"{bornes[1]:%Y}" if bornes else "",
        "fin": _date_fr(bornes[1]) if bornes else "—",
        "nb_communes": str(nb_communes),
        "nb_ventes": _milliers(len(transactions)),
        "seuil_bloc": str(options.seuil_vente_en_bloc),
        "valeur_min": _milliers(options.valeur_fonciere_min),
        "prix_min": _milliers(options.prix_m2_min),
        "prix_max": _milliers(options.prix_m2_max),
        "surface_min": f"{options.surface_min:g}",
        "ventes_min": str(charge.SEUIL_GRISAGE + 1),
        "json_ld": texte_ld,
    }


def ecrire_referencement(sortie: Path, adresse: str | None, jour: str) -> None:
    """`robots.txt`, et le plan du site quand son adresse publique est connue."""

    robots = ROBOTS
    if adresse:
        robots += f"\nSitemap: {adresse}sitemap.xml\n"
        (sortie / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url><loc>{adresse}</loc><lastmod>{jour}</lastmod></url>\n"
            "</urlset>\n",
            encoding="utf-8",
        )
    (sortie / "robots.txt").write_text(robots, encoding="utf-8")


def adresse_publique(brute: str | None) -> str | None:
    """`https://exemple.fr/` : l'adresse de production, ou rien.

    Elle sert a l'adresse canonique, aux apercus de partage et au plan du
    site. Refusee si ce n'est pas une adresse `https` simple : elle est
    ecrite telle quelle dans la page.
    """

    if not brute:
        return None
    brute = brute.strip()
    if not brute.endswith("/"):
        brute += "/"
    if not re.fullmatch(r"https://[a-z0-9.-]+(:[0-9]+)?/([A-Za-z0-9._~-]+/)*", brute):
        raise SystemExit(f"Adresse du site invalide : {brute!r} (attendu : https://domaine/)")
    return brute


def millesime_source(cle: str) -> dict | None:
    """Le millesime Etalab d'ou viennent les donnees, s'il est connu."""

    chemin = Path(repertoire_territoire(cle)) / "source.json"
    if not chemin.exists():
        return None
    source = json.loads(chemin.read_text(encoding="utf-8"))
    return {"millesime": source.get("millesime"), "verifie_le": source.get("verifie_le")}


def construire(cle: str, sortie: Path, adresse: str | None = None) -> dict:
    """Ecrit tout le site dans `sortie` (vide au prealable) ; rend le manifeste.

    `adresse` est l'adresse publique de production (`https://…/`), si elle est
    connue : elle ne sert qu'au referencement.
    """

    if sortie.exists():
        shutil.rmtree(sortie)
    donnees = sortie / "donnees"
    donnees.mkdir(parents=True)
    copier_page(sortie)
    (sortie / "_headers").write_text(EN_TETES, encoding="utf-8")

    journal(f"→ Ventes du territoire « {cle} »")
    transactions, _ = construire_transactions_territoire(cle, OPTIONS_NETTOYAGE)
    if transactions.empty:
        raise SystemExit(f"Territoire « {cle} » : aucune vente exploitable.")
    fichiers: dict[str, str] = {}

    # ---------------- Contours ----------------
    journal("→ Contours")
    codes_communes = tuple(sorted(transactions[CLE_COMMUNE].dropna().unique()))
    sections_attendues = tuple(sorted(transactions[CLE_SECTION].dropna().unique()))
    resultat = geo.charger_sections(codes_communes, sections_attendues, autoriser_reseau=False)
    if not resultat.geojson.get("features"):
        raise SystemExit("Aucun contour cadastral dans data/geo/.")
    contours_communes = geo.charger_communes_territoire(cle)
    fichiers["sections"] = ecrire_donnee(
        donnees, "sections", _json(geo.contours_carte(resultat.geojson["features"], CLE_SECTION))
    )
    if contours_communes.get("features"):
        fichiers["communes"] = ecrire_donnee(
            donnees, "communes", _json(geo.contours_carte(contours_communes["features"], CLE_COMMUNE))
        )
    cartographiees = {
        e["properties"][CLE_SECTION] for e in resultat.geojson["features"]
        if CLE_SECTION in e.get("properties", {})
    }
    orphelines = sorted(set(sections_attendues) - cartographiees)
    if orphelines:
        journal(f"  ! {len(orphelines)} section(s) sans contour : leurs ventes comptent, "
                "mais elles ne sont pas dessinees.")

    # ---------------- Reperes ----------------
    journal("→ Gares, noms de communes, voies")
    boite = geo.etendue(resultat.geojson)
    gares, message = geo.charger_gares(boite, autoriser_reseau=False)
    if not gares:
        journal(f"  ! aucune gare : {message}")
    fichiers["gares"] = ecrire_donnee(donnees, "gares", _json(charge.points_gares(gares)))
    noms = transactions.drop_duplicates(CLE_COMMUNE).set_index(CLE_COMMUNE)["nom_commune"].to_dict()
    fichiers["etiquettes"] = ecrire_donnee(
        donnees, "etiquettes", _json(charge.etiquettes_communes(contours_communes, noms))
    )
    reperes = charge.reperes_voies(transactions)
    fichiers["voies"] = ecrire_donnee(donnees, "voies", _json(reperes))

    # ---------------- Ventes, pour le calcul dans le navigateur ----------------
    journal("→ Ventes en colonnes binaires")
    libelles = charge.libelles_zones(transactions)
    contenu, meta = binaire.encoder_ventes(transactions, libelles)
    fichiers["ventes"] = ecrire_donnee(
        donnees, "ventes", gzip.compress(contenu, compresslevel=9, mtime=0), "bin"
    )
    fichiers["ventes_meta"] = ecrire_donnee(donnees, "ventes", _json(meta))

    # ---------------- Premier affichage ----------------
    # Les statistiques des filtres d'ouverture sont calculees ici : la carte
    # s'affiche coloree sans attendre les 3 Mo de ventes, que la plupart des
    # visiteurs ne telechargeront que s'ils touchent a un filtre.
    journal("→ Statistiques des filtres d'ouverture")
    defaut = charge.filtres_par_defaut(transactions)
    couches, echelle_points, nb = charge.calculer_couches(
        transactions, defaut, charge.METRIQUE_PAR_DEFAUT, (CLE_SECTION, CLE_COMMUNE), reperes, libelles
    )
    fichiers["defaut"] = ecrire_donnee(
        donnees, "defaut",
        _json({"metrique": charge.METRIQUE_PAR_DEFAUT, "couches": couches,
               "echelle_points": echelle_points, "nb": nb}),
    )

    # ---------------- Ventes individuelles, en tuiles ----------------
    journal("→ Tuiles de ventes")
    jeton = points.jeton(_empreinte(contenu), boite)
    fichiers["points"] = "donnees/" + points.publier(transactions, jeton, str(donnees), boite)

    # ---------------- Manifeste ----------------
    bornes = charge.periode(transactions)
    manifeste = {
        "version": 1,
        "genere_le": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "territoire": cle,
        "source": {
            "url": charge.URL_JEU_DE_DONNEES,
            "licence": charge.URL_LICENCE,
            **(millesime_source(cle) or {}),
        },
        "periode": {"debut": f"{bornes[0]:%Y-%m-%d}", "fin": f"{bornes[1]:%Y-%m-%d}"} if bornes else None,
        "nb_ventes": int(len(transactions)),
        "sections_sans_contour": len(orphelines),
        "fichiers": fichiers,
        "filtres": {
            "annees": charge.annees_disponibles(transactions),
            "surface_plafond": charge.plafond_surface(transactions),
            "types": charge.types_disponibles(transactions),
            "etats": charge.etats_disponibles(transactions),
        },
        "regles": charge.regles_calcul(),
        "fonds": charge.FONDS_DE_CARTE,
        "fond_initial": charge.FOND_PAR_DEFAUT,
        "cadrage": charge.cadrage_ventes(transactions, boite),
        "limites": charge.limites_de_navigation(boite),
    }
    (sortie / "manifeste.json").write_text(_json(manifeste), encoding="utf-8")
    ecrire_page(sortie, valeurs_page(cle, transactions, bornes), adresse)
    ecrire_referencement(sortie, adresse, manifeste["genere_le"][:10])

    nombre, plus_gros = verifier_limites(sortie)
    journal(f"✓ {sortie} : {nombre} fichiers (plafond {PLAFOND_FICHIERS}), "
            f"le plus gros {plus_gros / 1e6:.1f} Mo (plafond {PLAFOND_TAILLE / 2**20:.0f} Mio)")
    return manifeste


def main(arguments: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parseur.add_argument("--territoire", default=territoires.CLE_GRAND_PARIS)
    parseur.add_argument("--sortie", default=str(RACINE / "dist"))
    parseur.add_argument(
        "--adresse", default=os.environ.get("ADRESSE_SITE") or ADRESSE_PRODUCTION,
        help="adresse publique du site (https://…/), pour le referencement ; "
        "par defaut la variable ADRESSE_SITE, sinon l'adresse de production",
    )
    options = parseur.parse_args(arguments)
    adresse = adresse_publique(options.adresse)
    if not territoire_prepare(options.territoire):
        print(f"Territoire « {options.territoire} » non prepare.", file=sys.stderr)
        return 1
    os.environ.setdefault("DVF_RESEAU", "0")
    try:
        construire(options.territoire, Path(options.sortie), adresse)
    except LimiteDepassee as erreur:
        print(f"✗ {erreur}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
