"""Ce que la carte recoit : couleurs, chiffres, legendes, gares, cadrage.

Tout ce module est fait de fonctions pures : c'est la **reference** des
chiffres de la carte. Le site s'en sert a sa construction (statistiques des
filtres par defaut, regles transmises au navigateur), et le test de parite y
compare le calcul que le navigateur refait en JavaScript (`site/calcul.js`).

La carte recoit des *nombres* et un descriptif des champs, pas du HTML deja
mis en forme : c'est ce qui permet d'envoyer les dix mille sections d'une
agglomeration en quelques centaines de kilo-octets au lieu de plusieurs
megaoctets de libelles repetes. La mise en forme se fait dans le navigateur,
a partir du format annonce ici pour chaque champ.
"""

from __future__ import annotations

import math
import re

import pandas as pd

from src import geo, points
from src.ingestion import ETAT_ANCIEN, ETAT_NEUF
from src.stats import (
    ANNEES_MIN_TENDANCE,
    CLE_COMMUNE,
    CLE_SECTION,
    GRIS_DONNEES_INSUFFISANTES,
    METRIQUES,
    PALETTE_DIVERGENTE,
    PALETTE_SEQUENTIELLE,
    VOLUME_MIN_ANNUEL,
    EchelleCouleur,
    Filtres,
    amplitude_rendement,
    couleurs_rendement,
    couleurs_sections,
    echelle_et_grisees,
    etats_des_ventes,
    filtrer,
    formater_euros,
    formater_taux_annuel,
    rendement_sections,
    statistiques_sections,
    voies_dominantes,
)

#: Le jeu de donnees d'ou sort tout ce que montre la carte. C'est la seule
#: chose qui subsiste sous elle : qui veut verifier un chiffre doit pouvoir
#: remonter a la source en un clic, le reste se lit sur la carte elle-meme.
URL_JEU_DE_DONNEES = (
    "https://www.data.gouv.fr/fr/datasets/demandes-de-valeurs-foncieres-geolocalisees/"
)
URL_LICENCE = "https://www.etalab.gouv.fr/licence-ouverte-open-licence/"

#: Seuil de grisage : une zone comptant ce nombre de ventes ou moins est
#: grisee et ecartee du calcul de l'echelle de couleurs. Il faut donc au
#: moins `SEUIL_GRISAGE + 1` = 5 ventes pour qu'une zone soit coloree. Sur
#: trois ou quatre ventes, une mediane change du tout au tout selon le bien
#: vendu : elle ne dit rien du quartier. C'est une regle statistique, pas un
#: reglage de lecture.
SEUIL_GRISAGE = 4

# --------------------------------------------------------------------------
# Fonds de carte
# --------------------------------------------------------------------------
# Carto (Positron / Dark Matter) impose une cle API : ces fonds sont
# volontairement absents. Tous ceux listes ci-dessous sont utilisables sans
# cle, et leur gabarit d'URL est celui qu'attend MapLibre (`{z}/{x}/{y}`).
#
# `zoom_max` est le dernier niveau reellement servi par la source. Au-dela,
# MapLibre agrandit les tuiles de ce niveau plutot que d'en demander qui
# n'existent pas — sans quoi le fond vire au gris uni (« Map data not yet
# available ») exactement quand on zoome pour voir les rues sous les ventes.
#
# `peinture` regle le rendu des tuiles dans MapLibre (proprietes `raster-*`).
#
# Le premier fond est celui de l'ouverture : le Plan IGN, service public
# francais, ouvert et sans cle, plutot qu'un fond americain tolere sans
# compte. Il est en couleurs ; desature et eclairci, il devient un gris
# discret sous les couleurs des prix, comme le fond clair d'Esri qu'il
# remplace — et l'adresse IP du visiteur ne quitte plus la France par defaut.
FONDS_DE_CARTE: dict[str, dict] = {
    "Plan IGN (gris)": {
        "url": "https://data.geopf.fr/wmts?SERVICE=WMTS&VERSION=1.0.0&REQUEST=GetTile"
        "&LAYER=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2&STYLE=normal&TILEMATRIXSET=PM"
        "&FORMAT=image/png&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}",
        "attr": "&copy; IGN — Géoplateforme (Plan IGN)",
        "zoom_max": 19,
        "peinture": {
            "raster-saturation": -1,
            "raster-contrast": -0.2,
            "raster-brightness-min": 0.25,
        },
    },
    "Clair (Esri)": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
        "World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        # Esri exige « Powered by Esri » en plus de la source des donnees.
        "attr": "Powered by Esri · Esri, HERE, Garmin, &copy; OpenStreetMap",
        "zoom_max": 16,
    },
    "OpenStreetMap": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        # La fondation OpenStreetMap demande un lien vers sa page de droits.
        "attr": '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" '
        'rel="noopener">OpenStreetMap</a> contributors',
    },
    "Satellite (IGN)": {
        "url": "https://data.geopf.fr/wmts?SERVICE=WMTS&VERSION=1.0.0&REQUEST=GetTile"
        "&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&TILEMATRIXSET=PM"
        "&FORMAT=image/jpeg&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}",
        "attr": "&copy; IGN — Géoplateforme (BD ORTHO)",
    },
    "Satellite (Esri)": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
        "MapServer/tile/{z}/{y}/{x}",
        "attr": "Powered by Esri · Esri, Maxar, Earthstar Geographics",
    },
}

#: Serveurs de tuiles des fonds, pour la politique de securite du site
#: (`Content-Security-Policy`) : le navigateur n'en contactera aucun autre.
ORIGINES_FONDS: tuple[str, ...] = tuple(
    sorted({"/".join(fond["url"].split("/")[:3]) for fond in FONDS_DE_CARTE.values()})
)

#: Fond pose a l'ouverture : le Plan IGN en gris, discret sous les couleurs
#: des prix. Les autres fonds se choisissent **sur la carte** : changer
#: de fond ne change que des tuiles.
FOND_PAR_DEFAUT = next(iter(FONDS_DE_CARTE))

# --------------------------------------------------------------------------
# Metriques
# --------------------------------------------------------------------------
#: L'evolution annuelle du prix est une metrique comme les autres : on la choisit
#: dans la meme liste, et c'est le seul endroit ou elle se decide. Elle n'est
#: pourtant pas une colonne de `statistiques_sections` mais une *lecture* de
#: l'une d'elles dans le temps — d'ou une cle a part, et la metrique de base
#: sur laquelle la tendance est ajustee.
CLE_RENDEMENT = "rendement"
METRIQUE_BASE_RENDEMENT = "prix_m2_median"

#: Liste proposee sur la carte : les niveaux de prix, puis leur evolution.
METRIQUES_PROPOSEES: tuple[tuple[str, str], ...] = tuple(
    [(cle, definition[0]) for cle, definition in METRIQUES.items()]
    + [(CLE_RENDEMENT, "Évolution annuelle du prix médian au m²")]
)
LIBELLES_METRIQUES: dict[str, str] = dict(METRIQUES_PROPOSEES)
METRIQUE_PAR_DEFAUT = next(iter(METRIQUES))

#: Ce qu'il faut savoir de l'evolution annuelle avant de la lire. Il
#: apparait dans la legende, seulement lorsque la metrique est choisie.
NOTE_RENDEMENT = (
    f"Tendance ajustée sur les médianes annuelles : il faut au moins "
    f"{ANNEES_MIN_TENDANCE} années comptant chacune {VOLUME_MIN_ANNUEL} ventes ou plus. "
    "Le taux mesure l'évolution du prix des biens vendus, pas celle d'un bien donné : "
    "une année où la section vend surtout des grands logements se lit comme une hausse. "
    "À lire comme un ordre de grandeur, pas comme un indice de marché."
)

#: Champs de l'infobulle d'une zone : (libelle, format lu par `carte.js`).
CHAMPS_ZONE: tuple[tuple[str, str], ...] = (
    ("Transactions", "entier"),
    ("Prix médian au m²", "euros_m2"),
    ("Prix moyen au m²", "euros_m2"),
    ("Prix total médian", "euros"),
    ("Prix total moyen", "euros"),
    ("Surface médiane", "surface"),
    ("Vente la plus chère", "euros"),
    ("Vente la moins chère", "euros"),
)

#: Colonnes de `statistiques_sections` lues pour `CHAMPS_ZONE`, dans le meme ordre.
COLONNES_ZONE: tuple[str, ...] = (
    "nb_transactions",
    "prix_m2_median",
    "prix_m2_moyen",
    "prix_total_median",
    "prix_total_moyen",
    "surface_mediane",
    "prix_max",
    "prix_min",
)

#: Champs ajoutes en mode evolution annuelle du prix : la tendance du prix
#: median au m² des biens vendus.
#:
#: La regularite (R2) est le garde-fou du mode : un taux de -8 %/an tire d'une
#: courbe en dents de scie se lirait sinon comme une baisse installee. Elle
#: reste donc a cote du taux, dans la meme infobulle.
CHAMPS_RENDEMENT: tuple[tuple[str, str], ...] = (
    ("Évolution annuelle", "taux"),
    ("Variation sur la période", "pourcent"),
    ("Régularité (R²)", "reel"),
    ("Médianes annuelles retenues", "entier"),
)

#: Colonnes de `rendement_sections` lues pour `CHAMPS_RENDEMENT`, meme ordre.
COLONNES_RENDEMENT: list[str] = [
    "taux_annuel", "variation_cumulee", "regularite", "nb_annees"
]

assert len(CHAMPS_ZONE) == len(COLONNES_ZONE)
assert len(CHAMPS_RENDEMENT) == len(COLONNES_RENDEMENT)


# --------------------------------------------------------------------------
# Filtres
# --------------------------------------------------------------------------


def annees_disponibles(transactions: pd.DataFrame) -> list[int]:
    return sorted(int(annee) for annee in transactions["annee"].dropna().unique())


def plafond_surface(transactions: pd.DataFrame) -> int:
    """Borne haute proposee pour la surface : le 99e centile, arrondi a 50 m².

    Laissee a ce maximum, la borne est **ouverte** : elle signifie « et plus ».
    """

    return int(transactions["surface_bati"].quantile(0.99) // 50 * 50 + 50)


def types_disponibles(transactions: pd.DataFrame) -> list[str]:
    return sorted(transactions["type_bien"].dropna().unique())


def etats_disponibles(transactions: pd.DataFrame) -> list[str]:
    """« Ancien » puis « Neuf (VEFA) », ceux qui ont des ventes."""

    presents = set(etats_des_ventes(transactions).unique())
    return [etat for etat in (ETAT_ANCIEN, ETAT_NEUF) if etat in presents]


def filtres_par_defaut(transactions: pd.DataFrame) -> Filtres:
    """Les filtres a l'ouverture : toutes les annees, toutes surfaces, tous
    types, le neuf comme l'ancien."""

    return Filtres(
        annees=tuple(annees_disponibles(transactions)),
        surface=(0.0, float(plafond_surface(transactions))),
        surface_max_ouvert=True,
        types_bien=types_disponibles(transactions),
        etats=etats_disponibles(transactions),
    )


def filtres_des_ventes(filtres: Filtres) -> dict:
    """Les filtres, tels que le navigateur les rejouera sur les ventes.

    Ce sont les memes regles que `stats.filtrer`, transmises en quelques
    dizaines d'octets plutot qu'appliquees ici.
    """

    surface = filtres.surface or (0.0, float("inf"))
    # `None` et non `[]` quand le filtre est absent : une liste vide dit « rien
    # ne passe », alors qu'un filtre non pose dit « tout passe ». Les deux se
    # ressemblent en Python, ou `filtrer` ignore un `None` ; dans le
    # navigateur, la difference est une carte sans une seule vente.
    return {
        "an": list(filtres.annees) if filtres.annees else None,
        "su": [float(surface[0]), float(surface[1]), bool(filtres.surface_max_ouvert)],
        "ty": list(filtres.types_bien) if filtres.types_bien else None,
        "et": list(filtres.etats) if filtres.etats else None,
    }


# --------------------------------------------------------------------------
# Couches : couleurs et chiffres d'une maille
# --------------------------------------------------------------------------


def _colonne(valeurs: pd.Series) -> list[float | None]:
    """Une colonne de nombres prete pour JSON — `None` la ou pandas met `NaN`."""

    reels = pd.to_numeric(valeurs, errors="coerce").to_numpy(dtype=float)
    return [None if math.isnan(valeur) else float(valeur) for valeur in reels]


def libelles_zones(transactions: pd.DataFrame) -> dict[str, str]:
    """Titre de l'infobulle de chaque zone, propriete du territoire.

    « Boulogne-Billancourt — section AB » pour une section, le nom seul pour
    une commune. Une poignee de sections portent des ventes rattachees a deux
    communes ou a deux libelles (mutations a cheval sur plusieurs parcelles) :
    le titre retenu est le plus frequent sur tout le territoire, et non celui
    de la premiere vente filtree — il ne change donc pas avec les filtres, et
    le navigateur n'a pas besoin de l'ordre des lignes pour le retrouver.
    """

    if transactions.empty:
        return {}
    libelles: dict[str, str] = {}
    for niveau, colonnes in (
        (CLE_SECTION, ["nom_commune", "section_courte"]),
        (CLE_COMMUNE, ["nom_commune"]),
    ):
        compte = (
            transactions.groupby([niveau, *colonnes], observed=True).size()
            .rename("n").reset_index()
            .sort_values([niveau, "n", *colonnes], ascending=[True, False, *[True] * len(colonnes)])
            .drop_duplicates(niveau)
        )
        if niveau == CLE_COMMUNE:
            textes = compte["nom_commune"].astype(str)
        else:
            textes = compte["nom_commune"].astype(str) + " — section " + compte["section_courte"].astype(str)
        libelles.update(zip(compte[niveau].astype(str), textes))
    return libelles


def couche(
    stats: pd.DataFrame,
    couleurs: dict[str, str],
    niveau: str,
    grisees: set[str] | None = None,
    rendements: pd.DataFrame | None = None,
    reperes: dict[str, str] | None = None,
    libelles: dict[str, str] | None = None,
) -> dict:
    """Tableau compact `code -> couleur + chiffres` pour une maille donnee.

    `libelles` (voir `libelles_zones`) fixe le titre de chaque zone ; sans
    lui, le titre est tire des ventes retenues.
    """

    mot = "commune" if niveau == CLE_COMMUNE else "section"
    champs = list(CHAMPS_ZONE) + (list(CHAMPS_RENDEMENT) if rendements is not None else [])

    # Colonne par colonne plutot que ligne par ligne : a dix mille sections,
    # un `iterrows()` coute a lui seul le tiers du temps de reexecution.
    codes = [str(code) for code in stats[niveau].to_numpy()]
    entetes = (
        [libelles.get(code, code) for code in codes]
        if libelles is not None
        else [str(nom) for nom in stats["nom_commune"].to_numpy()]
        if niveau == CLE_COMMUNE
        else [
            f"{commune} — section {section}"
            for commune, section in zip(
                stats["nom_commune"].to_numpy(), stats["section_courte"].to_numpy()
            )
        ]
    )
    colonnes = [_colonne(stats[nom]) for nom in COLONNES_ZONE]
    notes: list[str | None] = [None] * len(codes)
    # Les sections cadastrales n'ont pas de nom : le sous-titre de l'infobulle
    # donne les voies ou se concentrent leurs ventes.
    voies = [reperes.get(code) for code in codes] if reperes else None

    if grisees:
        note_volume = f"Trop peu de ventes pour être représentatif — {mot} grisée"
        notes = [note_volume if code in grisees else None for code in codes]

    if rendements is not None:
        aligne = (
            rendements.set_index(niveau).reindex(codes)
            if not rendements.empty
            else pd.DataFrame(index=pd.Index(codes), columns=COLONNES_RENDEMENT + ["fiable"])
        )
        fiable = aligne["fiable"].fillna(False).to_numpy(dtype=bool)
        colonnes += [
            [valeur if sur else None for valeur, sur in zip(_colonne(aligne[nom]), fiable)]
            for nom in COLONNES_RENDEMENT[:3]
        ]
        colonnes.append(_colonne(aligne[COLONNES_RENDEMENT[3]]))
        note_tendance = (
            f"Évolution non calculable — {ANNEES_MIN_TENDANCE} années à "
            f"{VOLUME_MIN_ANNUEL} ventes minimum"
        )
        notes = [
            note if sur else note_tendance for note, sur in zip(notes, fiable)
        ]

    return {
        "codes": codes,
        "couleurs": [couleurs.get(code, GRIS_DONNEES_INSUFFISANTES) for code in codes],
        "entetes": entetes,
        "reperes": voies if voies and any(voies) else None,
        "notes": notes if any(notes) else None,
        "champs": [list(champ) for champ in champs],
        "valeurs": [list(rangee) for rangee in zip(*colonnes)] if colonnes else [],
    }


def legende_prix(echelle: EchelleCouleur, libelle: str, note_gris: str) -> dict:
    return {
        "titre": libelle,
        "palette": list(PALETTE_SEQUENTIELLE),
        "graduations": [formater_euros(valeur, "") for valeur in echelle.graduations(5)],
        "note_gris": note_gris,
    }


def titre_rendement(libelle: str) -> str:
    """« Prix médian au m² » → « Évolution annuelle du prix médian au m² »."""

    return f"Évolution annuelle du {libelle[:1].lower()}{libelle[1:]}"


def legende_rendement(amplitude: float, libelle: str) -> dict:
    return {
        "titre": titre_rendement(libelle),
        "palette": list(PALETTE_DIVERGENTE),
        "graduations": [
            formater_taux_annuel(-amplitude),
            formater_taux_annuel(0),
            formater_taux_annuel(amplitude),
        ],
        "note_gris": f"Gris : moins de {ANNEES_MIN_TENDANCE} années exploitables",
        # Mise en garde affichee dans la legende, seulement quand cette
        # metrique est choisie.
        "note": NOTE_RENDEMENT,
    }


def echelle_des_ventes(echelle: EchelleCouleur | None, colonne: str) -> dict:
    """L'echelle de couleurs des ventes, transmise plutot qu'appliquee.

    Une couleur par vente, ce serait deux cent mille chaines a renvoyer a
    chaque changement de filtre. Le navigateur recoit les deux bornes et la
    palette, et c'est le GPU qui en tire la teinte de chaque pastille — sur la
    **meme echelle que les sections**, pour qu'un point plus rouge que sa
    section s'y lise comme une vente plus chere que sa médiane.
    """

    if echelle is None:
        return {"vide": True, "palette": list(PALETTE_SEQUENTIELLE), "colonne": "m2"}
    return {
        "bas": float(echelle.bas),
        "haut": float(echelle.haut),
        "vide": bool(echelle.vide),
        "palette": list(PALETTE_SEQUENTIELLE),
        # Nom de la colonne dans les tuiles : le prix au m² ou le prix total.
        "colonne": "m2" if colonne == "prix_m2" else "va",
    }


def couche_complete(
    filtrees: pd.DataFrame,
    niveau: str,
    metrique: str,
    seuil_volume: int,
    mode_rendement: bool,
    libelle_metrique: str,
    reperes: dict[str, str] | None = None,
    libelles: dict[str, str] | None = None,
) -> tuple[dict, pd.DataFrame, EchelleCouleur]:
    """Couleurs, chiffres et legende d'une maille.

    Rend aussi ses statistiques et l'echelle des prix. Cette derniere sert a
    colorer les ventes individuelles : points et zones partagent ainsi une
    seule et meme grille de lecture.
    """

    stats = statistiques_sections(filtrees, niveau)
    mot = "commune" if niveau == CLE_COMMUNE else "section"

    if mode_rendement:
        rendements = rendement_sections(filtrees, metrique, cle=niveau)
        fiables = (
            rendements.loc[rendements["fiable"], "taux_annuel"]
            if not rendements.empty
            else pd.Series(dtype=float)
        )
        amplitude = amplitude_rendement(fiables)
        # Colonne par colonne, comme les niveaux de prix : une boucle
        # `iterrows()` sur dix mille sections pese plus d'une seconde.
        teintes = couleurs_rendement(rendements["taux_annuel"], amplitude)
        couleurs = {
            code: (teinte if fiable else GRIS_DONNEES_INSUFFISANTES)
            for code, teinte, fiable in zip(
                rendements[niveau].to_numpy(), teintes, rendements["fiable"].to_numpy()
            )
        }
        charge = couche(
            stats, couleurs, niveau, rendements=rendements, reperes=reperes, libelles=libelles
        )
        charge["legende"] = legende_rendement(amplitude, libelle_metrique)
        charge["resume"] = (
            f"{int(rendements['fiable'].sum()) if not rendements.empty else 0} "
            f"{mot}(s) sur {int(filtrees[niveau].nunique())} atteignent "
            f"{ANNEES_MIN_TENDANCE} années à {VOLUME_MIN_ANNUEL} ventes ou plus ; "
            f"médiane du territoire {formater_taux_annuel(float(fiables.median()) if len(fiables) else float('nan'))}."
        )
        # Meme hors mode prix, les points de vente restent colores par niveau
        # de prix : l'evolution d'une vente isolee n'existe pas.
        echelle, _ = echelle_et_grisees(stats, metrique, seuil_volume, cle=niveau)
        return charge, stats, echelle

    couleurs, echelle, grisees = couleurs_sections(stats, metrique, seuil_volume, cle=niveau)
    note_gris = (
        f"Gris : moins de {seuil_volume + 1} ventes"
        if seuil_volume
        else "Gris : aucune vente avec les filtres actifs"
    )
    charge = couche(
        stats, couleurs, niveau, grisees=grisees, reperes=reperes, libelles=libelles
    )
    charge["legende"] = legende_prix(echelle, libelle_metrique, note_gris)
    charge["resume"] = (
        f"{len(filtrees):,}".replace(",", " ")
        + f" ventes sur {int(filtrees[niveau].nunique())} {mot}s"
        + (f", dont {len(grisees)} grisée(s) faute de volume." if grisees else ".")
    )
    return charge, stats, echelle


def calculer_couches(
    transactions: pd.DataFrame,
    filtres: Filtres,
    choix_metrique: str,
    niveaux: tuple[str, ...] = (CLE_SECTION, CLE_COMMUNE),
    reperes: dict[str, str] | None = None,
    libelles: dict[str, str] | None = None,
) -> tuple[dict[str, dict], dict, int]:
    """Couleurs, chiffres et legendes de chaque maille, pour un jeu de reglages.

    Rend les couches, l'echelle des ventes (sur celle des sections) et le
    nombre de ventes retenues par les filtres. `libelles` se calcule sur le
    territoire entier (`libelles_zones`) : a defaut, il l'est ici.
    """

    if libelles is None:
        libelles = libelles_zones(transactions)

    mode_rendement = choix_metrique == CLE_RENDEMENT
    metrique = METRIQUE_BASE_RENDEMENT if mode_rendement else choix_metrique
    libelle_metrique, colonne_metrique, _, _ = METRIQUES[metrique]
    filtrees = filtrer(transactions, filtres)

    couches: dict[str, dict] = {}
    echelle_points = None
    for niveau in niveaux:
        couches[niveau], _stats, echelle = couche_complete(
            filtrees, niveau, metrique, SEUIL_GRISAGE, mode_rendement, libelle_metrique, reperes,
            libelles,
        )
        if niveau == CLE_SECTION:
            echelle_points = echelle
    return couches, echelle_des_ventes(echelle_points, colonne_metrique), len(filtrees)


# --------------------------------------------------------------------------
# Reperes : voies, gares, noms de communes
# --------------------------------------------------------------------------


def reperes_voies(transactions: pd.DataFrame) -> dict[str, str]:
    """Voies dominantes de chaque section et de chaque commune.

    Le cadastre ne nomme pas ses sections ; ce repere est reconstitue a partir
    des adresses des ventes. Il ne depend d'aucun filtre : c'est une propriete
    du territoire, calculee une seule fois.
    """

    reperes: dict[str, str] = {}
    for niveau in (CLE_SECTION, CLE_COMMUNE):
        reperes.update(voies_dominantes(transactions, niveau).to_dict())
    return reperes


def points_gares(gares: list[dict]) -> dict:
    """Gares en GeoJSON, chacune avec son rang, son genre, son statut et ses lignes.

    Le rang distingue les gares lourdes (RER, Transilien, TER, TGV) des
    stations de metro et de tramway. C'est la carte qui en tire deux tailles :
    ce sont les stations de metro, une tous les trois cents metres dans Paris,
    qui saturaient la vue d'ensemble — pas les gares.

    Le genre, lui, decide du **dessin** : un train, un « M » ou un « T ». Deux
    proprietes plutot qu'une parce qu'elles ne disent pas la meme chose — un
    pole ou passent le RER et le metro se voit de loin (rang lourd) et s'y
    prend un train (genre train), mais une gare de tramway se dessine « T »
    tout en restant discrete.

    Le statut, lui, separe deux couches : ce qui roule aujourd'hui et ce qui
    est annonce. Un pole en service ou une ligne est annoncee reste en
    service ; la ligne annoncee part a part (`a_venir`). La carte filtre sur cette propriete plutot que de recevoir
    deux jeux de points, pour que la bascule ne recharge rien.
    """

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(gare["longitude"]), float(gare["latitude"])],
                },
                "properties": {
                    "nom": gare["nom"],
                    "reseau": gare.get("reseau") or "",
                    "lignes": ", ".join(gare.get("lignes") or []),
                    # Lignes annoncees sur un pole deja desservi : l'infobulle
                    # les separe de celles qu'on y prend aujourd'hui.
                    "a_venir": ", ".join(gare.get("lignes_a_venir") or []),
                    "statut": gare.get("statut") or geo.STATUT_SERVICE,
                    "rang": geo.rang_gare(gare),
                    "genre": geo.genre_gare(gare),
                },
            }
            for gare in gares
        ],
    }


def nom_sur_carte(nom: str) -> str:
    """Raccourcit un nom de commune pour qu'il tienne sur la carte.

    « Paris 11e Arrondissement » devient « Paris 11e » : le mot complet triple
    la largeur de l'etiquette et evince ses voisines pour ne rien apprendre.
    """

    return re.sub(r"\s+Arrondissement$", "", str(nom))


def etiquettes_communes(contours: dict, noms: dict[str, str]) -> list[dict]:
    """Noms de communes a poser sur la carte, avec leur point d'ancrage.

    Le poids est l'aire du contour : quand deux etiquettes se chevauchent, la
    carte garde la plus grande commune, celle qu'on s'attend a lire en premier.
    """

    ancres = geo.points_representatifs(contours, CLE_COMMUNE)
    etiquettes = [
        {"n": nom_sur_carte(noms[code]), "x": lon, "y": lat, "p": aire}
        for code, (lon, lat, aire) in ancres.items()
        if code in noms
    ]
    return sorted(etiquettes, key=lambda e: -e["p"])


# --------------------------------------------------------------------------
# Cadrage
# --------------------------------------------------------------------------

#: Part des ventes laissee hors du cadrage d'ouverture, de chaque cote. Le
#: territoire s'etire en longs doigts le long de la Seine et des vallees :
#: cadrer sur ses contours extremes reduirait Paris et sa premiere couronne
#: a un timbre-poste au milieu de l'ecran. Cadrer sur ou se vendent les
#: logements — 98 % d'entre eux — donne a l'agglomeration tout l'ecran.
QUANTILE_CADRAGE = 0.01


def cadrage_ventes(
    transactions: pd.DataFrame,
    boite: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    """Vue d'ouverture `(ouest, sud, est, nord)` : la ou se vendent les logements.

    Se replie sur l'emprise des contours si les coordonnees manquent.
    """

    repli = (boite[1], boite[0], boite[3], boite[2]) if boite else None
    if not {"longitude", "latitude"} <= set(transactions.columns):
        return repli
    garde = points.dans_emprise(transactions["longitude"], transactions["latitude"], boite)
    lon = transactions["longitude"].to_numpy(dtype=float)[garde]
    lat = transactions["latitude"].to_numpy(dtype=float)[garde]
    if len(lon) < 50:
        return repli
    bas, haut = QUANTILE_CADRAGE, 1 - QUANTILE_CADRAGE
    ouest, est = (float(v) for v in pd.Series(lon).quantile([bas, haut]))
    sud, nord = (float(v) for v in pd.Series(lat).quantile([bas, haut]))
    if not (est > ouest and nord > sud):
        return repli
    return ouest, sud, est, nord


def limites_de_navigation(
    boite: tuple[float, float, float, float] | None, marge: float = 0.6
) -> tuple[float, float, float, float] | None:
    """Rectangle `(ouest, sud, est, nord)` dont la carte ne laisse pas sortir.

    L'emprise du territoire, elargie d'une quarantaine de kilometres : assez
    pour regarder autour, pas assez pour se perdre en pleine campagne.
    """

    if not boite:
        return None
    lat_min, lon_min, lat_max, lon_max = boite
    return lon_min - marge, lat_min - marge / 2, lon_max + marge, lat_max + marge / 2


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def periode(transactions: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Premiere et derniere date de vente, lues dans les donnees."""

    if "date_mutation" not in transactions.columns:
        return None
    dates = pd.to_datetime(transactions["date_mutation"], errors="coerce").dropna()
    if dates.empty:
        return None
    return dates.min(), dates.max()


def pied_de_page(transactions: pd.DataFrame) -> str:
    """Provenance et periode couverte, en une ligne (Markdown).

    La Licence Ouverte demande de citer la source **et sa date** : la periode
    des ventes en tient lieu, lue dans les donnees elles-memes plutot
    qu'ecrite a la main — elle ne peut donc pas retarder sur le parquet.
    """

    bornes = periode(transactions)
    texte = f" · ventes du {bornes[0]:%d/%m/%Y} au {bornes[1]:%d/%m/%Y}" if bornes else ""
    # Une ligne, pas deux : la carte descend jusqu'au bas de l'ecran et ne
    # laisse voir que la premiere.
    return (
        f"Données : [DVF géolocalisées]({URL_JEU_DE_DONNEES}), DGFiP / Etalab, "
        f"[Licence Ouverte 2.0]({URL_LICENCE}){texte}."
    )


# --------------------------------------------------------------------------
# Regles transmises au calcul du navigateur
# --------------------------------------------------------------------------


def regles_calcul() -> dict:
    """Tout ce que `site/calcul.js` doit savoir pour refaire les couches.

    Les palettes, seuils, libelles et phrases ne sont ecrits qu'ici, en
    Python : le navigateur les recoit plutot que d'en garder une copie qui
    finirait par diverger. Le test de parite verifie le reste.
    """

    from src.stats import AMPLITUDES_RENDEMENT

    return {
        "gris": GRIS_DONNEES_INSUFFISANTES,
        "palette_prix": list(PALETTE_SEQUENTIELLE),
        "palette_rendement": list(PALETTE_DIVERGENTE),
        "metriques": [
            {"cle": cle, "libelle": libelle, "colonne": colonne, "agregat": agregat}
            for cle, (libelle, colonne, agregat, _unite) in METRIQUES.items()
        ],
        "metriques_proposees": [list(couple) for couple in METRIQUES_PROPOSEES],
        "metrique_par_defaut": METRIQUE_PAR_DEFAUT,
        "cle_rendement": CLE_RENDEMENT,
        "base_rendement": METRIQUE_BASE_RENDEMENT,
        "seuil_grisage": SEUIL_GRISAGE,
        "volume_min_annuel": VOLUME_MIN_ANNUEL,
        "annees_min": ANNEES_MIN_TENDANCE,
        "amplitudes": list(AMPLITUDES_RENDEMENT),
        "champs_zone": [list(champ) for champ in CHAMPS_ZONE],
        "champs_rendement": [list(champ) for champ in CHAMPS_RENDEMENT],
        "notes": {
            "volume": {
                mot: f"Trop peu de ventes pour être représentatif — {mot} grisée"
                for mot in ("commune", "section")
            },
            "tendance": (
                f"Évolution non calculable — {ANNEES_MIN_TENDANCE} années à "
                f"{VOLUME_MIN_ANNUEL} ventes minimum"
            ),
            "gris_prix": (
                f"Gris : moins de {SEUIL_GRISAGE + 1} ventes"
                if SEUIL_GRISAGE
                else "Gris : aucune vente avec les filtres actifs"
            ),
            "gris_rendement": legende_rendement(2.0, "")["note_gris"],
            "rendement": NOTE_RENDEMENT,
        },
    }
