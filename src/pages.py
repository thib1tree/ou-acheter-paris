"""Mise en page des pages par commune : du HTML statique, sans script.

Les chiffres viennent de `src/fiches.py` ; ce module ne fait que les ecrire.
Chaque page est un fichier `prix-immobilier/<commune>/index.html`, lisible
sans JavaScript — c'est ce qu'un moteur de recherche lit le mieux — et tenue
par la meme politique de securite que le reste du site : ni style ni script
dans le HTML, tout est dans `site/textes.css` et `site/fiches.css`.

Trois regles de redaction :

* **des prix, pas des jugements.** Une page decrit sa commune ; elle ne la
  classe pas, ne la note pas, et ne dit d'aucune qu'elle est « chere » ou
  « a eviter » ;
* **un chiffre dit toujours sur quoi il porte** : la periode, le type de
  bien, le nombre de ventes ;
* **un tiret plutot qu'un chiffre fragile** : sous le seuil de publication
  (`fiches.SEUIL_PUBLICATION`), la case reste vide et la legende dit pourquoi.
"""

from __future__ import annotations

import html
import json
import math

from src import fiches

#: Dossier des pages, dans le site : il fait partie de leur adresse.
DOSSIER = "prix-immobilier"

#: Espace fine insecable (milliers) et espace insecable (unites).
FINE = " "
INSEC = " "
ABSENT = "—"

#: Ordre des departements dans la liste : Paris, la petite couronne, la grande.
ORDRE_DEPARTEMENTS = ("75", "92", "93", "94", "77", "78", "91", "95")

LIBELLES_TYPES = {"Appartement": "Appartements", "Maison": "Maisons"}
#: Classe CSS de la serie de chaque type : la couleur suit le type, jamais
#: son rang — une commune sans maisons garde ses appartements en bleu.
SERIES = {"Appartement": "s1", "Maison": "s2"}

LIBELLES_STATUT = {"travaux": "en travaux", "projet": "en projet"}

#: Zoom d'arrivee sur la carte : celui de « Aller à » (`ZOOM_COMMUNE`, carte.js).
ZOOM_CARTE = 13


# --------------------------------------------------------------------------
# Mise en forme
# --------------------------------------------------------------------------


def e(texte) -> str:
    return html.escape(str(texte), quote=True)


def entier(n) -> str:
    return f"{n:,.0f}".replace(",", FINE)


def euros(valeur) -> str:
    return ABSENT if valeur is None else entier(valeur) + INSEC + "€"


def euros_m2(valeur) -> str:
    return ABSENT if valeur is None else entier(valeur) + INSEC + "€/m²"


def surface(valeur) -> str:
    return ABSENT if valeur is None else entier(valeur) + INSEC + "m²"


def pourcentage(valeur: float) -> str:
    """« +2,1 % », « −0,4 % » (un vrai signe moins), « stable » a zero."""

    if abs(valeur) < 0.05:
        return "stable"
    signe = "+" if valeur > 0 else "−"
    return signe + f"{abs(valeur):.1f}".replace(".", ",") + INSEC + "%"


def ventes(n: int) -> str:
    return entier(n) + INSEC + ("vente" if n < 2 else "ventes")


def libelle_pieces(nombre: int) -> str:
    if nombre == 1:
        return "Studio"
    if nombre == fiches.PIECES_MAX:
        return f"{nombre} pièces et +"
    return f"{nombre} pièces"


def periode_recente(fin) -> str:
    """« juillet 2025 à juin 2026 » : les douze derniers mois publies."""

    debut, _ = fiches.fenetres(fin)
    return f"{fiches.mois_fr(debut)} à {fiches.mois_fr(fin)}"


# --------------------------------------------------------------------------
# Graphique : prix median au m² par annee
# --------------------------------------------------------------------------


def _pas_rond(etendue: float, divisions: int = 4) -> float:
    """Un pas « rond » (1, 2, 2,5 ou 5 fois une puissance de dix)."""

    brut = max(etendue, 1.0) / divisions
    puissance = 10 ** math.floor(math.log10(brut))
    for facteur in (1, 2, 2.5, 5, 10):
        if brut <= facteur * puissance:
            return facteur * puissance
    return 10 * puissance


def graphique(annees: list[dict], titre: str) -> str:
    """Le prix median au m² par annee, une ligne par type de bien, en SVG.

    Une annee sous le seuil de publication n'a pas de point, et la ligne ne
    l'enjambe pas : relier 2022 a 2024 laisserait croire a un 2023. Rend une
    chaine vide s'il n'y a pas au moins deux points a relier.
    """

    series = {
        nom: [(ligne["annee"], ligne[nom]["m2"], ligne[nom]["n"]) for ligne in annees
              if ligne[nom]["m2"] is not None]
        for nom in fiches.TYPES
    }
    series = {nom: points for nom, points in series.items() if len(points) >= 2}
    if not series:
        return ""

    toutes = [a["annee"] for a in annees]
    valeurs = [v for points in series.values() for _, v, _ in points]
    pas = _pas_rond(max(valeurs) - min(valeurs))
    bas = math.floor(min(valeurs) / pas) * pas
    haut = math.ceil(max(valeurs) / pas) * pas
    if haut == bas:
        bas, haut = bas - pas, haut + pas

    # Un cadre etroit : sur un telephone, il n'est reduit que d'un tiers, et
    # ses textes restent lisibles ; sur grand ecran, la feuille de style le
    # borne (`.graphique`) pour qu'ils ne grossissent pas.
    largeur, hauteur = 520, 280
    gauche, droite, sommet, pied = 70, 84, 14, 36
    trace_l, trace_h = largeur - gauche - droite, hauteur - sommet - pied

    def x(annee: int) -> float:
        if len(toutes) == 1:
            return gauche + trace_l / 2
        return gauche + (toutes.index(annee) / (len(toutes) - 1)) * trace_l

    def y(valeur: float) -> float:
        return sommet + (1 - (valeur - bas) / (haut - bas)) * trace_h

    morceaux = [
        f'<svg class="graphique" viewBox="0 0 {largeur} {hauteur}" role="img" '
        f'aria-label="{e(titre)}">'
    ]
    graduation = bas
    while graduation <= haut + 1e-9:
        gy = y(graduation)
        morceaux.append(f'<line class="grille" x1="{gauche}" x2="{largeur - droite}" y1="{gy:.1f}" y2="{gy:.1f}"/>')
        morceaux.append(
            f'<text class="axe" x="{gauche - 8}" y="{gy + 4:.1f}" text-anchor="end">{entier(graduation)}{INSEC}€</text>'
        )
        graduation += pas
    for annee in toutes:
        morceaux.append(
            f'<text class="axe" x="{x(annee):.1f}" y="{hauteur - 12}" text-anchor="middle">{annee}</text>'
        )

    # Etiquettes de fin de ligne : la valeur, a droite du dernier point. Deux
    # lignes qui finissent au meme niveau s'ecartent juste assez pour se lire.
    fins = {nom: y(points[-1][1]) for nom, points in series.items()}
    if len(fins) == 2:
        (haute, y_haute), (basse, y_basse) = sorted(fins.items(), key=lambda paire: paire[1])
        manque = 17 - (y_basse - y_haute)
        if manque > 0:
            fins[haute], fins[basse] = y_haute - manque / 2, y_basse + manque / 2

    for nom, points in series.items():
        classe = SERIES[nom]
        suite: list[tuple[int, int, int]] = []
        troncons = [suite]
        for point in points:
            if suite and toutes.index(point[0]) != toutes.index(suite[-1][0]) + 1:
                suite = []
                troncons.append(suite)
            suite.append(point)
        for troncon in troncons:
            if len(troncon) > 1:
                trace = " ".join(f"{x(a):.1f},{y(v):.1f}" for a, v, _ in troncon)
                morceaux.append(f'<polyline class="ligne {classe}" points="{trace}"/>')
        for annee, valeur, nombre in points:
            morceaux.append(
                f'<circle class="point {classe}" cx="{x(annee):.1f}" cy="{y(valeur):.1f}" r="4.5">'
                f"<title>{e(LIBELLES_TYPES[nom])}, {annee} : {e(euros_m2(valeur))} ({e(ventes(nombre))})</title>"
                "</circle>"
            )
        dernier = points[-1]
        morceaux.append(
            f'<text class="valeur" x="{x(dernier[0]) + 11:.1f}" y="{fins[nom] + 5:.1f}">'
            f"{e(euros(dernier[1]))}</text>"
        )
    morceaux.append("</svg>")

    legende = ""
    if len(series) > 1:
        legende = '<div class="legende" aria-hidden="true">' + "".join(
            f'<span><i class="cle {SERIES[nom]}"></i>{e(LIBELLES_TYPES[nom])}</span>' for nom in series
        ) + "</div>"
    return legende + "".join(morceaux)


# --------------------------------------------------------------------------
# Morceaux de page
# --------------------------------------------------------------------------


def _entete(titre: str, description: str, racine: str, canonique: str | None,
            nom_site: str, json_ld: list | None) -> str:
    lignes = [
        "<!doctype html>",
        '<html lang="fr">',
        "<head>",
        '<meta charset="utf-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />',
        f"<title>{e(titre)}</title>",
        f'<meta name="description" content="{e(description)}" />',
        '<meta name="theme-color" content="#bd0026" />',
    ]
    if canonique:
        site = canonique.split(DOSSIER + "/")[0]
        lignes += [
            f'<link rel="canonical" href="{e(canonique)}" />',
            f'<meta property="og:url" content="{e(canonique)}" />',
            f'<meta property="og:image" content="{e(site)}partage.png" />',
            '<meta name="twitter:card" content="summary_large_image" />',
        ]
    lignes += [
        '<meta property="og:type" content="article" />',
        '<meta property="og:locale" content="fr_FR" />',
        f'<meta property="og:site_name" content="{e(nom_site)}" />',
        f'<meta property="og:title" content="{e(titre)}" />',
        f'<meta property="og:description" content="{e(description)}" />',
        f'<link rel="icon" href="{racine}favicon.svg" type="image/svg+xml" />',
        f'<link rel="stylesheet" href="{racine}textes.css" />',
        f'<link rel="stylesheet" href="{racine}fiches.css" />',
    ]
    if json_ld:
        # `</` ne doit jamais fermer le bloc de script qui porte ce JSON.
        texte = json.dumps(json_ld, ensure_ascii=False).replace("</", "<\\/")
        lignes.append(f'<script type="application/ld+json">{texte}</script>')
    lignes += ["</head>", '<body class="fiche">']
    return "\n".join(lignes)


def _fil_d_ariane(adresse: str | None, etapes: list[tuple[str, str]]) -> list | None:
    """Le chemin de la page, pour les moteurs (`BreadcrumbList`). Il lui faut
    des adresses completes : sans adresse publique, rien."""

    if not adresse:
        return None
    return [{
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": rang, "name": nom, "item": adresse + chemin}
            for rang, (nom, chemin) in enumerate(etapes, start=1)
        ],
    }]


def _tuile(nom: str, chiffres: dict) -> str:
    """Le prix du moment d'un type de bien, et ce qu'il faut pour le lire."""

    recent, periode = chiffres["recent"], chiffres["periode"]
    libelle = LIBELLES_TYPES[nom]
    if recent["m2"] is None:
        if periode["m2"] is None:
            detail = f"{ventes(periode['n'])} en cinq ans : trop peu pour un prix médian."
            return (f'<div class="chiffre"><span class="etiquette">{libelle}</span>'
                    f'<span class="valeur absent">{ABSENT}</span>'
                    f'<span class="detail">{detail}</span></div>')
        return (f'<div class="chiffre"><span class="etiquette">{libelle}</span>'
                f'<span class="valeur">{euros_m2(periode["m2"])}</span>'
                f'<span class="detail">Prix médian sur cinq ans ({ventes(periode["n"])}) : '
                f"trop peu de ventes sur les douze derniers mois ({recent['n']}).</span></div>")
    details = [f"Prix médian d'une vente : {euros(recent['prix'])}, pour {surface(recent['surface'])}."]
    suite = ventes(recent["n"]) + " en douze mois"
    if chiffres["evolution"] is not None:
        suite += " ; prix au m² " + pourcentage(chiffres["evolution"]) + " sur un an"
    details.append(suite[0].upper() + suite[1:] + ".")
    return (f'<div class="chiffre"><span class="etiquette">{libelle}</span>'
            f'<span class="valeur">{euros_m2(recent["m2"])}</span>'
            + "".join(f'<span class="detail">{d}</span>' for d in details) + "</div>")


def _tableau(entetes: list[str], lignes: list[list[str]], legende: str = "") -> str:
    """Un tableau : premiere colonne en texte, les autres en chiffres."""

    tete = "".join(
        ('<th scope="col">' if i == 0 else '<th scope="col" class="n">') + cellule + "</th>"
        for i, cellule in enumerate(entetes)
    )
    corps = "".join(
        "<tr>" + "".join(
            (f'<th scope="row">{cellule}</th>' if i == 0 else f'<td class="n">{cellule}</td>')
            for i, cellule in enumerate(ligne)
        ) + "</tr>"
        for ligne in lignes
    )
    titre = f"<caption>{legende}</caption>" if legende else ""
    return (f'<div class="tableau"><table>{titre}<thead><tr>{tete}</tr></thead>'
            f"<tbody>{corps}</tbody></table></div>")


def _gare(gare: dict) -> str:
    lignes = ", ".join(gare.get("lignes") or []) or (gare.get("reseau") or "")
    statut = LIBELLES_STATUT.get(gare.get("statut") or "")
    a_venir = ", ".join(gare.get("lignes_a_venir") or [])
    texte = f"<b>{e(gare['nom'])}</b>"
    if lignes:
        texte += f" — {e(lignes)}"
    if statut:
        texte += f" ({statut})"
    if a_venir:
        texte += f" ; à venir : {e(a_venir)}"
    return f"<li>{texte}</li>"


def _prix_des_types(fiche: dict) -> str:
    """« appartements 6 850 €/m², maisons 7 400 €/m² » : ce qui est publiable."""

    morceaux = [
        f"{LIBELLES_TYPES[nom].lower()} {euros_m2(fiche['types'][nom]['recent']['m2'])}"
        for nom in fiches.TYPES if fiche["types"][nom]["recent"]["m2"] is not None
    ]
    return ", ".join(morceaux)


# --------------------------------------------------------------------------
# Page d'une commune
# --------------------------------------------------------------------------


def page_commune(commune: dict, contexte: dict) -> str:
    """La page d'une commune.

    `commune` : `code`, `nom`, `identifiant`, `fiche`, `gares`, `proches`
    (liste de communes, memes cles), `ancre` (lon, lat) ou `None`.
    `contexte` : `nom_site`, `adresse` (publique, ou `None`), `debut`, `fin`
    (bornes des ventes), `url_jeu`, `url_licence`, `jour` (date de construction).
    """

    fiche = commune["fiche"]
    nom = fiches.nom_affiche(commune["nom"])
    lieu = fiches.a_commune(commune["nom"])
    numero, nom_departement = fiches.departement(commune["code"])
    debut, fin = contexte["debut"], contexte["fin"]
    racine = "../../"
    recente = periode_recente(fin)

    # Le titre et la description n'annoncent que ce que la page contient :
    # une petite commune n'a ni quartiers publiables, ni prix sur douze mois.
    detaillee = len(fiche["quartiers"]) > 1
    titre = f"Prix immobilier {lieu} ({numero}) : " + (
        "prix au m², évolution, quartiers" if detaillee else "prix au m² et ventes"
    )
    prix = _prix_des_types(fiche)
    description = (
        (f"{lieu[0].upper()}{lieu[1:]} ({numero}), prix médians de {recente} : {prix}. " if prix
         else f"Les prix de vente des logements {lieu} ({numero}). ")
        + ("Évolution, prix par taille de logement et par quartier" if detaillee
           else "Prix et nombre de ventes par année")
        + f", d'après {ventes(fiche['n'])} réelles (DVF)."
    )
    canonique = (
        f"{contexte['adresse']}{DOSSIER}/{commune['identifiant']}/" if contexte["adresse"] else None
    )
    ariane = _fil_d_ariane(contexte["adresse"], [
        (contexte["nom_site"], ""),
        ("Prix par commune", f"{DOSSIER}/"),
        (nom, f"{DOSSIER}/{commune['identifiant']}/"),
    ])

    corps = [_entete(titre, description, racine, canonique, contexte["nom_site"], ariane)]
    corps.append(
        "<header>"
        f'<a class="retour" href="{racine}">← La carte</a>'
        '<a class="retour suite" href="../">Toutes les communes</a>'
        "</header>\n<main>"
    )
    corps.append(f"<h1>Prix de l'immobilier {e(lieu)}</h1>")
    corps.append(
        f'<p class="chapeau">{e(nom_departement)} ({numero}) · {ventes(fiche["n"])} de logements, '
        f"du {debut:%d/%m/%Y} au {fin:%d/%m/%Y}, d'après les demandes de valeurs foncières (DVF).</p>"
    )

    # Un type de bien sans rien de publiable (les maisons d'un arrondissement
    # de Paris) ne prend pas la place de l'autre.
    presents = [t for t in fiches.TYPES if fiche["types"][t]["periode"]["m2"] is not None]
    montres = presents or list(fiches.TYPES)
    corps.append('<div class="chiffres">' + "".join(
        _tuile(type_bien, fiche["types"][type_bien]) for type_bien in montres
    ) + "</div>")
    if prix:
        corps.append(
            f'<p class="note">Prix médians des ventes de {recente} : la moitié des logements se '
            "sont vendus moins cher, l'autre moitié plus cher. L'évolution compare ces douze mois "
            "aux douze précédents ; elle suit les biens vendus, pas un même bien.</p>"
        )
    else:
        corps.append(
            '<p class="note">Un prix médian sépare les ventes en deux : la moitié des logements se '
            "sont vendus moins cher, l'autre moitié plus cher.</p>"
        )
    if commune.get("ancre"):
        lon, lat = commune["ancre"]
        corps.append(
            f'<p><a class="bouton" href="{racine}#{ZOOM_CARTE:.2f}/{lat:.5f}/{lon:.5f}">'
            f"Voir {e(nom)} sur la carte, quartier par quartier et vente par vente</a></p>"
        )

    # ---------------- Evolution ----------------
    annees = fiche["annees"]
    if annees:
        corps.append(f'<h2 id="evolution">Évolution des prix {e(lieu)}</h2>')
        titre_graphique = f"Prix médian au m² par année {lieu}"
        dessin = graphique(annees, titre_graphique)
        partielles = []
        if debut.month > 1:
            partielles.append(f"{debut.year} ne compte que les ventes à partir de {fiches.MOIS[debut.month - 1]}")
        if fin.month < 12:
            partielles.append(f"{fin.year} s'arrête en {fiches.MOIS[fin.month - 1]}")
        note_annees = " ; ".join(partielles) + ". " if partielles else ""
        traces = [t for t in fiches.TYPES if sum(l[t]["m2"] is not None for l in annees) >= 2]
        if dessin:
            objet = (
                f"des {LIBELLES_TYPES[traces[0]].lower()} vendus chaque année" if len(traces) == 1
                else "des ventes de chaque année"
            )
            corps.append(
                f"<figure>{dessin}<figcaption>Prix médian au m² {objet}. "
                f"{note_annees}</figcaption></figure>"
            )
        lignes = [
            [str(ligne["annee"])] + [
                cellule for t in montres
                for cellule in (euros_m2(ligne[t]["m2"]), entier(ligne[t]["n"]))
            ]
            for ligne in annees
        ]
        corps.append(_tableau(
            ["Année"] + [cellule for t in montres for cellule in (LIBELLES_TYPES[t], "Ventes")], lignes,
            f"Prix médian au m² et nombre de ventes par année. {ABSENT} : moins de "
            f"{fiches.SEUIL_PUBLICATION} ventes." + ("" if dessin else f" {note_annees}"),
        ))

    # ---------------- Pieces ----------------
    pieces = [ligne for ligne in fiche["pieces"] if ligne["m2"] is not None]
    if pieces:
        corps.append(f'<h2 id="pieces">Prix d\'un appartement {e(lieu)}, selon sa taille</h2>')
        corps.append(_tableau(
            ["Taille", "Prix", "Surface", "Prix au m²", "Ventes"],
            # L'unite du prix au m² est dans l'en-tete : cinq colonnes doivent
            # tenir sur un telephone.
            [[libelle_pieces(l["pieces"]), euros(l["prix"]), surface(l["surface"]),
              euros(l["m2"]), entier(l["n"])] for l in fiche["pieces"] if l["n"]],
            f"Prix, surface et prix au m² médians des appartements vendus du {debut:%d/%m/%Y} au "
            f"{fin:%d/%m/%Y}. {ABSENT} : moins de {fiches.SEUIL_PUBLICATION} ventes.",
        ))
        maisons = fiche["types"]["Maison"]["periode"]
        if maisons["m2"] is not None:
            corps.append(
                f"<p>Sur la même période, une maison s'est vendue {euros(maisons['prix'])} en prix "
                f"médian, pour {surface(maisons['surface'])} habitables ({ventes(maisons['n'])}). "
                "Le prix d'une maison comprend son terrain.</p>"
            )

    # ---------------- Neuf et ancien ----------------
    neuf, ancien = fiche["neuf"]["neuf"], fiche["neuf"]["ancien"]
    if neuf["m2"] is not None and ancien["m2"] is not None:
        corps.append('<h2 id="neuf">Neuf et ancien</h2>')
        corps.append(
            f"<p>Les appartements neufs vendus sur plan (VEFA) se sont vendus {euros_m2(neuf['m2'])} "
            f"en prix médian ({ventes(neuf['n'])}), contre {euros_m2(ancien['m2'])} dans l'ancien "
            f"({ventes(ancien['n'])}), sur toute la période. Un logement neuf vendu une fois achevé "
            "compte comme ancien.</p>"
        )

    # ---------------- Quartiers ----------------
    quartiers = fiche["quartiers"]
    if len(quartiers) > 1:
        corps.append(f'<h2 id="quartiers">Prix par quartier {e(lieu)}</h2>')
        corps.append(
            f"<p>De {euros_m2(quartiers[-1]['m2'])} à {euros_m2(quartiers[0]['m2'])} selon le quartier. "
            "Le découpage est celui du cadastre, en sections de quelques pâtés de maisons ; il ne "
            "les nomme pas, et chacune est désignée ici par les rues où se concentrent ses ventes.</p>"
        )
        # Replie : une grande commune compte une centaine de sections, et la
        # page doit rester lisible d'un trait. Le tableau reste dans la page.
        corps.append(f"<details><summary>Le prix des {len(quartiers)} quartiers, un par un</summary>")
        corps.append(_tableau(
            ["Quartier", "Prix au m²", "Ventes"],
            [[(e(q["voies"]) + f' <span class="section">section {e(q["section"])}</span>') if q["voies"]
              else f"Section {e(q['section'])}", euros_m2(q["m2"]), entier(q["n"])] for q in quartiers],
            f"Prix médian au m², appartements et maisons confondus, du {debut:%d/%m/%Y} au "
            f"{fin:%d/%m/%Y}. Sections d'au moins {fiches.SEUIL_PUBLICATION} ventes.",
        ))
        corps.append("</details>")

    # ---------------- Gares ----------------
    gares = commune.get("gares") or []
    en_service = [g for g in gares if (g.get("statut") or "service") == "service"]
    a_venir = [g for g in gares if (g.get("statut") or "service") != "service"]
    if gares:
        corps.append(f'<h2 id="gares">Gares et stations {e(lieu)}</h2>')
        if en_service:
            corps.append('<ul class="gares">' + "".join(_gare(g) for g in en_service) + "</ul>")
        if a_venir:
            corps.append("<p>À venir :</p>")
            corps.append('<ul class="gares">' + "".join(_gare(g) for g in a_venir) + "</ul>")

    # ---------------- Communes proches ----------------
    proches = commune.get("proches") or []
    if proches:
        corps.append('<h2 id="proches">Communes proches</h2>')
        liens = []
        for proche in proches:
            prix_proche = _prix_des_types(proche["fiche"])
            liens.append(
                f'<li><a href="../{e(proche["identifiant"])}/">{e(fiches.nom_affiche(proche["nom"]))}</a>'
                + (f'<span class="detail">{prix_proche}</span>' if prix_proche else "") + "</li>"
            )
        corps.append('<ul class="communes">' + "".join(liens) + "</ul>")

    corps.append(_sources(contexte, racine))
    corps.append("</main>\n</body>\n</html>\n")
    return "\n".join(corps)


def _sources(contexte: dict, racine: str) -> str:
    return (
        '<h2 id="sources">D\'où viennent ces chiffres</h2>'
        "<p>Des <a href=\"" + e(contexte["url_jeu"]) + '" rel="noopener">demandes de valeurs '
        "foncières géolocalisées</a> (DGFiP, Etalab, <a href=\"" + e(contexte["url_licence"])
        + '" rel="noopener">Licence Ouverte 2.0</a>) : chaque vente immobilière enregistrée par '
        "l'administration fiscale. Seules les ventes d'appartements et de maisons sont retenues, "
        "sans les ventes en bloc, les lots mêlés à un local commercial ni les prix invraisemblables. "
        "Une vente n'y paraît que plusieurs mois après sa signature : les derniers mois sont "
        "incomplets.</p>"
        f"<p>Aucune statistique n'est publiée sur moins de {fiches.SEUIL_PUBLICATION} ventes, et "
        "aucune vente n'est citée ici. Ces chiffres décrivent des ventes passées : ils ne sont ni "
        "l'estimation d'un bien, ni un conseil. Voir les "
        f'<a href="{racine}mentions-legales.html">mentions légales</a>.</p>'
        f'<p class="maj">Page mise à jour le {contexte["jour"]}.</p>'
    )


# --------------------------------------------------------------------------
# Liste des communes
# --------------------------------------------------------------------------


def page_liste(communes: list[dict], contexte: dict) -> str:
    """La liste des communes, par departement et par ordre alphabetique."""

    racine = "../"
    fin = contexte["fin"]
    recente = periode_recente(fin)
    titre = "Prix immobilier autour de Paris, commune par commune"
    description = (
        f"Le prix médian au m² des appartements et des maisons dans {len(communes)} communes de "
        f"l'agglomération parisienne, d'après les ventes réelles (DVF) de {recente}."
    )
    canonique = f"{contexte['adresse']}{DOSSIER}/" if contexte["adresse"] else None
    ariane = _fil_d_ariane(contexte["adresse"], [
        (contexte["nom_site"], ""), ("Prix par commune", f"{DOSSIER}/"),
    ])

    corps = [_entete(titre, description, racine, canonique, contexte["nom_site"], ariane)]
    corps.append(f'<header><a class="retour" href="{racine}">← La carte</a></header>\n<main>')
    corps.append("<h1>Prix de l'immobilier, commune par commune</h1>")
    corps.append(
        f'<p class="chapeau">Les {len(communes)} communes de l\'agglomération parisienne, et pour '
        f"chacune le prix médian au m² des ventes de {recente}. Chaque page détaille l'évolution, "
        "les prix par taille de logement et par quartier.</p>"
    )

    par_departement: dict[str, list[dict]] = {}
    for commune in communes:
        par_departement.setdefault(commune["code"][:2], []).append(commune)
    numeros = [n for n in ORDRE_DEPARTEMENTS if n in par_departement] + sorted(
        set(par_departement) - set(ORDRE_DEPARTEMENTS)
    )
    corps.append('<nav class="sommaire" aria-label="Départements">' + "".join(
        f'<a href="#d{n}">{e(fiches.DEPARTEMENTS.get(n, n))}</a>' for n in numeros
    ) + "</nav>")

    for numero in numeros:
        # Paris se lit dans l'ordre de ses arrondissements, le reste par nom.
        cle = (lambda c: c["code"]) if numero == "75" else (lambda c: c["identifiant"])
        lignes = []
        for commune in sorted(par_departement[numero], key=cle):
            types = commune["fiche"]["types"]
            lignes.append([
                f'<a href="{e(commune["identifiant"])}/">{e(fiches.nom_affiche(commune["nom"]))}</a>',
                euros_m2(types["Appartement"]["recent"]["m2"]),
                euros_m2(types["Maison"]["recent"]["m2"]),
            ])
        corps.append(f'<h2 id="d{numero}">{e(fiches.DEPARTEMENTS.get(numero, numero))} ({numero})</h2>')
        corps.append(_tableau(["Commune", "Appartements", "Maisons"], lignes))

    corps.append(
        f'<p class="note">Prix médians au m² des ventes de {recente}. {ABSENT} : moins de '
        f"{fiches.SEUIL_PUBLICATION} ventes sur la période.</p>"
    )
    corps.append(_sources(contexte, racine))
    corps.append("</main>\n</body>\n</html>\n")
    return "\n".join(corps)
