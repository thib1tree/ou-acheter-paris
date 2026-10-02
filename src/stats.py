"""Filtres, agregations par section cadastrale, rendement et couleurs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from src.ingestion import ETAT_ANCIEN

# --------------------------------------------------------------------------
# Metriques exposees dans l'interface
# --------------------------------------------------------------------------

#: cle interne -> (libelle, colonne source, fonction d'agregation, unite)
METRIQUES: dict[str, tuple[str, str, str, str]] = {
    "prix_m2_median": ("Prix médian au m²", "prix_m2", "median", "€/m²"),
    "prix_m2_moyen": ("Prix moyen au m²", "prix_m2", "mean", "€/m²"),
    "prix_total_median": ("Prix total médian", "valeur_fonciere", "median", "€"),
    "prix_total_moyen": ("Prix total moyen", "valeur_fonciere", "mean", "€"),
}

#: Vert fonce -> jaune -> rouge fonce (niveaux de prix).
#:
#: La repartition compte autant que les teintes. Les jalons sont espaces
#: regulierement par l'interpolation, donc leur *nombre* par famille determine
#: la part du degrade allouee a chaque couleur : 27 % pour les verts, 14 % pour
#: les jaunes, 27 % pour les oranges. C'est dans cette zone intermediaire que
#: se situe la majorite des sections, et c'est donc la qu'il faut de la nuance.
#:
#: Le vert tire volontairement vers le bleu-vert plutot que vers le vert
#: prairie. Un vert franc et un rouge sombre se confondent en vision
#: deuteranope (environ 8 % des hommes) : mesure faite, l'ecart entre les deux
#: extremes passe de 4,0 a 8,7 (distance OKLab x100, simulation Machado 2009)
#: grace a cette inclinaison, sans rien changer a la lecture en vision normale.
PALETTE_SEQUENTIELLE: tuple[str, ...] = (
    # Verts : 4 jalons seulement, le bas de gamme n'a pas besoin de nuances.
    "#00695c",
    "#2f9a83",
    "#6fbd9f",
    "#a8dcbe",
    # Bascule vers le jaune.
    "#cfe9ad",
    "#e6f3ab",
    # Jaunes.
    "#f9f7a0",
    "#ffe988",
    "#ffd96b",
    # Ambres et oranges : la zone la plus fournie en sections.
    "#fdc453",
    "#fbab48",
    "#f7913f",
    "#f2763a",
    # Rouges.
    "#e55334",
    "#d02c28",
    "#a50026",
)

#: Rouge fonce -> blanc -> vert fonce (evolution annuelle du prix).
PALETTE_DIVERGENTE: tuple[str, ...] = (
    "#67001f",
    "#b2182b",
    "#d6604d",
    "#f4a582",
    "#fddbc7",
    "#ffffff",
    "#d9f0d3",
    "#a6dba0",
    "#5aae61",
    "#1b7837",
    "#00441b",
)

#: Remplissage des sections sans donnee exploitable.
GRIS_DONNEES_INSUFFISANTES = "#d9d9d9"


@dataclass
class Filtres:
    """Etat des filtres de la barre laterale.

    `annees` est un **ensemble de valeurs retenues**, et non un intervalle : on
    decoche une annee creuse sans toucher aux autres.
    """

    annees: tuple[int, ...] | None = None
    surface: tuple[float, float] | None = None
    surface_max_ouvert: bool = True
    types_bien: Sequence[str] | None = None
    #: Neuf (VEFA) et/ou ancien (`ingestion.ETAT_NEUF`, `ETAT_ANCIEN`).
    etats: Sequence[str] | None = None


def filtrer(transactions: pd.DataFrame, filtres: Filtres) -> pd.DataFrame:
    """Applique les filtres de la barre laterale."""

    df = transactions
    if df.empty:
        return df

    masque = pd.Series(True, index=df.index)

    if filtres.annees:
        masque &= df["annee"].isin(list(filtres.annees))

    if filtres.surface:
        mini, maxi = filtres.surface
        borne_haute = (
            pd.Series(True, index=df.index)
            if filtres.surface_max_ouvert
            else df["surface_bati"] <= maxi
        )
        masque &= (df["surface_bati"] >= mini) & borne_haute

    if filtres.types_bien:
        masque &= df["type_bien"].isin(list(filtres.types_bien))

    if filtres.etats:
        masque &= etats_des_ventes(df).isin(list(filtres.etats))

    return df[masque.fillna(False)]


def etats_des_ventes(transactions: pd.DataFrame) -> pd.Series:
    """L'etat (neuf ou ancien) de chaque vente ; « Ancien » faute de colonne."""

    if "etat" in transactions.columns:
        return transactions["etat"].astype(str)
    return pd.Series(ETAT_ANCIEN, index=transactions.index)


# --------------------------------------------------------------------------
# Agregation par section
# --------------------------------------------------------------------------


#: Nom de sortie de chaque colonne decrivant la vente extreme d'une zone.
#: `{sens}` vaut `prix_max` ou `prix_min`.
_COLONNES_EXTREME = {
    "valeur_fonciere": "{sens}",
    "surface_bati": "surface_{sens}",
    "prix_m2": "prix_m2_{sens}",
    "type_bien": "type_{sens}",
    "date_mutation": "date_{sens}",
}


def _extremes(transactions: pd.DataFrame, cle: str) -> pd.DataFrame:
    """Vente la plus chere et la moins chere de chaque zone.

    Vectorise : un `idxmax`/`idxmin` par groupe puis une seule indexation, au
    lieu d'une boucle Python sur les groupes. Sur une agglomeration, la
    difference n'est pas cosmetique — c'est ce calcul qui decide du temps
    d'attente apres un changement de filtre, a dix mille sections.
    """

    colonnes = [c for c in _COLONNES_EXTREME if c in transactions.columns]
    sortie = [
        _COLONNES_EXTREME[colonne].format(sens=sens)
        for sens in ("prix_max", "prix_min")
        for colonne in colonnes
    ]
    zones = pd.Index(transactions[cle].dropna().unique(), name=cle)
    valides = transactions.dropna(subset=["valeur_fonciere"])
    if valides.empty:
        return pd.DataFrame(columns=sortie, index=zones)

    prix = valides.groupby(cle)["valeur_fonciere"]
    morceaux = []
    for sens, index in (("prix_max", prix.idxmax()), ("prix_min", prix.idxmin())):
        extrait = valides.loc[index, colonnes]
        extrait.index = index.index
        extrait.columns = [_COLONNES_EXTREME[c].format(sens=sens) for c in colonnes]
        morceaux.append(extrait)
    # `reindex` : une zone dont toutes les ventes sont sans prix n'apparait pas
    # dans les extremes, mais doit rester dans le tableau.
    return pd.concat(morceaux, axis=1).reindex(zones)[sortie]


#: Maille d'agregation de la carte. La section cadastrale est la maille de
#: lecture du projet ; la commune est le niveau large, indispensable des que le
#: territoire depasse quelques centaines de sections — a l'echelle d'une
#: agglomeration, une section ne fait que quelques pixels.
CLE_SECTION = "code_section"
CLE_COMMUNE = "code_commune"


def statistiques_sections(
    transactions: pd.DataFrame, cle: str = CLE_SECTION
) -> pd.DataFrame:
    """Statistiques par zone (section cadastrale par defaut) pour le jeu filtre.

    `cle` choisit la maille : `code_section` pour la vue de detail,
    `code_commune` pour la vue large. La colonne de regroupement garde son nom
    dans le resultat, de sorte que le reste du code manipule les deux niveaux
    exactement de la meme facon.
    """

    colonnes = [
        cle,
        "section_courte",
        "nom_commune",
        "code_commune",
        "nb_transactions",
        "prix_m2_median",
        "prix_m2_moyen",
        "prix_total_median",
        "prix_total_moyen",
        "surface_mediane",
        "prix_max",
        "surface_prix_max",
        "prix_m2_prix_max",
        "type_prix_max",
        "prix_min",
        "surface_prix_min",
        "prix_m2_prix_min",
        "type_prix_min",
    ]
    colonnes = list(dict.fromkeys(colonnes))
    if transactions.empty:
        return pd.DataFrame(columns=colonnes)

    descriptifs = {
        "section_courte": ("section_courte", "first"),
        "nom_commune": ("nom_commune", "first"),
        "code_commune": ("code_commune", "first"),
    }
    # La colonne de regroupement devient l'index : la reprendre en agregat
    # creerait un doublon de nom au moment du `reset_index`.
    descriptifs.pop(cle, None)

    base = transactions.groupby(cle).agg(
        **descriptifs,
        nb_transactions=("id_mutation", "size"),
        prix_m2_median=("prix_m2", "median"),
        prix_m2_moyen=("prix_m2", "mean"),
        prix_total_median=("valeur_fonciere", "median"),
        prix_total_moyen=("valeur_fonciere", "mean"),
        surface_mediane=("surface_bati", "median"),
    )

    stats = base.join(_extremes(transactions, cle))
    for colonne in ("prix_m2_moyen", "prix_total_moyen", "prix_total_median", "surface_mediane"):
        stats[colonne] = stats[colonne].astype(float).round(0)
    stats = stats.reset_index()
    if cle == CLE_COMMUNE:
        # Au niveau large, « la section » d'une commune n'a pas de sens : la
        # colonne reste presente pour que les deux niveaux aient la meme forme,
        # mais elle porte le nom de la commune plutot qu'un libelle trompeur.
        stats["section_courte"] = stats["nom_commune"]
    return stats


# --------------------------------------------------------------------------
# Reperes de voirie : donner un nom parlant a une section
# --------------------------------------------------------------------------
#
# Le cadastre ouvert ne **nomme** pas ses sections : une section n'a qu'un code
# (prefixe + lettres), herite du decoupage des feuilles de plan. Les noms de
# lieux-dits existent dans un autre jeu de donnees, mal couvert en milieu
# urbain, et ne correspondent pas aux sections.
#
# Le seul repere parlant et disponible partout est donc tire des ventes
# elles-memes : les voies ou elles se situent. « Section AB » ne dit rien,
# « Section AB — Rue de Paris, Avenue Gambetta » situe immediatement.

#: Numero de voirie en tete d'adresse : « 12 », « 12 Bis », « 3 B »…
_PREFIXE_NUMERO = r"^\s*\d+\s*(?:(?:bis|ter|quater|quinquies)\b|[a-z]\b)?\s*"

#: Nombre de voies citees en repere. Au-dela, le libelle cesse d'etre lisible.
VOIES_PAR_ZONE = 3


def voies_dominantes(
    transactions: pd.DataFrame, cle: str = "code_section", nb_voies: int = VOIES_PAR_ZONE
) -> pd.Series:
    """Voies les plus frequentes de chaque zone, de la plus fournie a la moins.

    Rend une serie `code de zone -> "Rue A, Avenue B"`. Le calcul ne depend
    d'aucun filtre : c'est une propriete du territoire, calculee une fois et
    mise en cache par l'appelant.
    """

    if transactions.empty or "adresse" not in transactions.columns:
        return pd.Series(dtype=object)

    voie = (
        transactions["adresse"]
        .astype("string")
        .str.replace(_PREFIXE_NUMERO, "", regex=True, case=False)
        .str.strip()
    )
    retenues = pd.DataFrame({cle: transactions[cle], "voie": voie}).dropna()
    retenues = retenues[retenues["voie"].str.len() > 2]
    if retenues.empty:
        return pd.Series(dtype=object)

    compte = retenues.groupby([cle, "voie"], observed=True).size().rename("n").reset_index()
    compte = compte.sort_values([cle, "n", "voie"], ascending=[True, False, True])
    premieres = compte.groupby(cle, observed=True).head(max(int(nb_voies), 1))
    return premieres.groupby(cle, observed=True)["voie"].apply(", ".join)


def echelle_et_grisees(
    stats: pd.DataFrame,
    metrique: str,
    seuil_volume: int = 0,
    cle: str = CLE_SECTION,
) -> tuple["EchelleCouleur", set[str]]:
    """Echelle de couleurs retenue pour une maille, et zones ecartees du calcul.

    Extrait de `couleurs_sections` parce que l'echelle sert aussi ailleurs :
    les points de vente d'une section sont colores avec elle, de sorte qu'une
    couleur designe le meme prix sur un point et sur une zone.
    """

    if stats.empty:
        return EchelleCouleur.depuis(pd.Series(dtype=float)), set()

    seuil = max(int(seuil_volume), 0)
    assez_fourni = stats["nb_transactions"] > seuil
    grisees = set(stats.loc[~assez_fourni, cle])

    # Si tout est sous le seuil, mieux vaut une echelle calee sur l'ensemble
    # qu'une echelle vide : la carte reste grise, mais la legende reste juste.
    reference = stats.loc[assez_fourni, metrique] if assez_fourni.any() else stats[metrique]
    return EchelleCouleur.depuis(reference), grisees


def couleurs_sections(
    stats: pd.DataFrame,
    metrique: str,
    seuil_volume: int = 0,
    cle: str = CLE_SECTION,
) -> tuple[dict[str, str], "EchelleCouleur", set[str]]:
    """Couleur de chaque section, echelle retenue, et sections grisees.

    Une section reposant sur une poignee de ventes ne dit pas grand-chose : sa
    mediane bouge du tout au tout selon le bien vendu. Au-dela d'un simple
    signal visuel, ces sections sont **exclues du calcul de l'echelle** — sans
    quoi une vente hors norme dans une section a deux transactions etirerait le
    degrade pour toutes les autres. Meme principe que le mode rendement.
    """

    echelle, grisees = echelle_et_grisees(stats, metrique, seuil_volume, cle)
    if stats.empty:
        return {}, echelle, grisees

    # Colonne par colonne : un `iterrows()` sur dix mille sections couterait
    # plus d'une seconde a lui seul.
    codes = stats[cle].to_numpy()
    teintes = echelle.couleurs(stats[metrique])
    grise = np.isin(codes, list(grisees))
    couleurs = {
        code: (GRIS_DONNEES_INSUFFISANTES if est_grise else teinte)
        for code, teinte, est_grise in zip(codes, teintes, grise)
    }
    return couleurs, echelle, grisees


# --------------------------------------------------------------------------
# Rendement : evolution annuelle du prix median au m2 par section
# --------------------------------------------------------------------------

#: Une mediane annuelle calculee sur moins de ventes que ce seuil n'est pas un
#: point de tendance : elle bouge du tout au tout selon le bien vendu.
VOLUME_MIN_ANNUEL = 5

#: Nombre d'annees exploitables exige pour ajuster une tendance. Deux points
#: suffisent geometriquement mais ne disent rien de la regularite ; trois
#: permettent de distinguer une tendance d'un accident.
ANNEES_MIN_TENDANCE = 3


def rendement_sections(
    transactions: pd.DataFrame,
    metrique: str,
    volume_min_annuel: int = VOLUME_MIN_ANNUEL,
    annees_min: int = ANNEES_MIN_TENDANCE,
    cle: str = CLE_SECTION,
) -> pd.DataFrame:
    """Evolution annuelle du prix de chaque section, en % par an.

    Une simple comparaison « annee N contre annee N-k » mesurerait surtout le
    bruit : sur les donnees du depot, elle donne un ecart-type de 13,7 points entre sections (de -47 % a +49 %) alors que la
    tendance ajustee ci-dessous tombe a 3,1 points (de -17 % a +8 %/an). Deux
    medianes annuelles reposant sur quelques dizaines de ventes se croisent au
    hasard du bien vendu ; l'information de marche est dans la *pente*, pas
    dans l'ecart entre deux millesimes.

    La methode, en trois temps :

    1. une **mediane par annee** et par section — la mediane absorbe les ventes
       hors norme, contrairement a la moyenne ;
    2. seules les annees comptant au moins `volume_min_annuel` ventes sont
       retenues comme points de tendance ;
    3. une **regression sur le logarithme** de ces medianes, ponderee par le
       nombre de ventes de chaque annee. La pente d'une droite en log est un
       taux de croissance : `exp(pente) - 1` se lit directement en % par an, et
       se compare d'une section a l'autre quelle que soit la profondeur
       d'historique disponible.

    Le R2 pondere mesure la regularite de cette tendance : proche de 1, les
    medianes annuelles s'alignent ; proche de 0, le prix oscille sans direction
    nette et le taux ne doit pas etre lu comme une prevision.

    Une section qui n'atteint pas `annees_min` points exploitables reste
    presente dans le resultat, avec `fiable=False` et un taux `NaN` : la carte
    la grise et son infobulle explique ce qui manque.
    """

    _, colonne, fonction, _ = METRIQUES[metrique]

    colonnes = [
        cle,
        "taux_annuel",
        "variation_cumulee",
        "nb_annees",
        "nb_ventes",
        "annee_debut",
        "annee_fin",
        "valeur_debut",
        "valeur_fin",
        "regularite",
        "fiable",
    ]
    if transactions.empty:
        return pd.DataFrame(columns=colonnes)

    annuel = (
        transactions.groupby([cle, "annee"])
        .agg(valeur=(colonne, fonction), nb=("id_mutation", "size"))
        .reset_index()
    )
    # Une mediane nulle ou negative rendrait le logarithme impossible ; le
    # pipeline l'exclut deja, ce garde-fou protege un appel direct.
    exploitables = annuel[(annuel["nb"] >= max(int(volume_min_annuel), 1)) & (annuel["valeur"] > 0)]

    if exploitables.empty:
        return pd.DataFrame(columns=colonnes)

    # Regression ponderee sur le logarithme, calculee par sommes groupees
    # plutot que section par section. Les sommes suffisent : une droite des
    # moindres carres ne depend de ses points qu'a travers elles, et une
    # boucle Python sur dix mille sections couterait quelques secondes a
    # chaque changement de filtre.
    points = exploitables.sort_values([cle, "annee"]).copy()
    poids = points["nb"].astype(float)
    # Annees comptees depuis la premiere du jeu : la pente est la meme, mais
    # les sommes de carres restent petites (quelques dizaines au lieu de
    # quatre millions), et la soustraction qui donne la dispersion ne perd
    # plus de chiffres significatifs.
    annee = points["annee"].astype(float) - float(points["annee"].min())
    log_valeur = np.log(points["valeur"].astype(float))
    points["_w"] = poids
    points["_wa"] = poids * annee
    points["_wl"] = poids * log_valeur
    points["_waa"] = poids * annee * annee
    points["_wal"] = poids * annee * log_valeur
    points["_wll"] = poids * log_valeur * log_valeur

    groupes = points.groupby(cle, sort=True)
    sommes = groupes[["_w", "_wa", "_wl", "_waa", "_wal", "_wll"]].sum()
    resultat = pd.DataFrame(index=sommes.index)
    resultat["nb_annees"] = groupes.size()
    resultat["nb_ventes"] = sommes["_w"].astype(int)
    resultat["annee_debut"] = groupes["annee"].first().astype(int)
    resultat["annee_fin"] = groupes["annee"].last().astype(int)
    resultat["valeur_debut"] = groupes["valeur"].first().astype(float)
    resultat["valeur_fin"] = groupes["valeur"].last().astype(float)

    total_poids = sommes["_w"]
    annee_moyenne = sommes["_wa"] / total_poids
    log_moyen = sommes["_wl"] / total_poids
    dispersion = sommes["_waa"] - total_poids * annee_moyenne**2
    covariance = sommes["_wal"] - total_poids * annee_moyenne * log_moyen
    variance_log = sommes["_wll"] - total_poids * log_moyen**2
    etendue = (resultat["annee_fin"] - resultat["annee_debut"]).astype(float)

    exploitable = (
        (resultat["nb_annees"] >= max(int(annees_min), 2)) & (etendue > 0) & (dispersion > 0)
    )
    pente = (covariance / dispersion).where(exploitable)
    resultat["taux_annuel"] = np.expm1(pente) * 100
    resultat["variation_cumulee"] = np.expm1(pente * etendue) * 100
    # Somme des carres des residus = variance totale - part expliquee par la
    # pente : l'identite evite de reparcourir chaque point.
    residuelle = variance_log - pente * covariance
    resultat["regularite"] = (1 - residuelle / variance_log).where(
        exploitable & (variance_log > 0)
    )
    resultat["fiable"] = exploitable

    return resultat.reset_index()[colonnes]


#: Amplitudes proposees pour la legende du rendement, en % par an. Une echelle
#: figee sur un de ces paliers reste comparable d'un filtre a l'autre, alors
#: qu'une amplitude recalculee au centieme changerait de sens a chaque clic.
AMPLITUDES_RENDEMENT: tuple[float, ...] = (2.0, 3.0, 5.0, 8.0, 12.0, 20.0)


def amplitude_rendement(taux: pd.Series) -> float:
    """Demi-amplitude de l'echelle divergente, arrondie a un palier lisible."""

    taux = pd.to_numeric(taux, errors="coerce").dropna().abs()
    if taux.empty:
        return AMPLITUDES_RENDEMENT[0]
    observee = float(taux.quantile(0.9))
    if not np.isfinite(observee) or observee <= 0:
        observee = float(taux.max() or 0)
    for palier in AMPLITUDES_RENDEMENT:
        if observee <= palier:
            return palier
    return AMPLITUDES_RENDEMENT[-1]


def couleur_rendement(taux_annuel: float | None, amplitude: float) -> str:
    """Couleur rouge fonce -> blanc -> vert fonce pour un taux en % par an."""

    if _absente(taux_annuel) or amplitude <= 0:
        return GRIS_DONNEES_INSUFFISANTES
    return _interpoler(PALETTE_DIVERGENTE, (float(taux_annuel) + amplitude) / (2 * amplitude))


def couleurs_rendement(taux_annuels, amplitude: float) -> list[str]:
    """`couleur_rendement` sur toute une colonne de taux."""

    taux = pd.to_numeric(pd.Series(taux_annuels), errors="coerce").to_numpy(dtype=float)
    if amplitude <= 0:
        return [GRIS_DONNEES_INSUFFISANTES] * len(taux)
    return interpoler_serie(PALETTE_DIVERGENTE, (taux + amplitude) / (2 * amplitude))


# --------------------------------------------------------------------------
# Couleurs
# --------------------------------------------------------------------------


def _absente(valeur) -> bool:
    """`None`, `NaN`, `pd.NA`, `NaT` ou valeur non numerique."""

    try:
        if valeur is None or pd.isna(valeur):
            return True
        return not np.isfinite(float(valeur))
    except (TypeError, ValueError):
        return True


def _interpoler(palette: Sequence[str], position: float) -> str:
    """Interpolation lineaire entre les couleurs d'une palette (position 0-1)."""

    if not np.isfinite(position):
        return GRIS_DONNEES_INSUFFISANTES
    position = float(min(max(position, 0.0), 1.0))
    echelle = position * (len(palette) - 1)
    bas = int(np.floor(echelle))
    haut = min(bas + 1, len(palette) - 1)
    part = echelle - bas

    def composantes(couleur: str) -> tuple[int, int, int]:
        couleur = couleur.lstrip("#")
        return tuple(int(couleur[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]

    debut, fin = composantes(palette[bas]), composantes(palette[haut])
    melange = [round(d + (f - d) * part) for d, f in zip(debut, fin)]
    return "#{:02x}{:02x}{:02x}".format(*melange)


def interpoler_serie(palette: Sequence[str], positions) -> list[str]:
    """`_interpoler` applique a tout un tableau de positions d'un coup.

    Meme resultat, couleur pour couleur (arrondi au pair le plus proche des
    deux cotes) : c'est la version qu'utilisent les couches de la carte, ou
    l'appel scalaire repete dix mille fois pese plus que tout le reste.
    """

    positions = pd.to_numeric(pd.Series(positions), errors="coerce").to_numpy(dtype=float)
    if not len(positions):
        return []
    rgb = np.array(
        [[int(couleur.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)] for couleur in palette],
        dtype=float,
    )
    finies = np.isfinite(positions)
    echelle = np.clip(np.where(finies, positions, 0.0), 0.0, 1.0) * (len(palette) - 1)
    bas = np.floor(echelle).astype(int)
    haut = np.minimum(bas + 1, len(palette) - 1)
    part = (echelle - bas)[:, None]
    melange = np.round(rgb[bas] + (rgb[haut] - rgb[bas]) * part).astype(int)
    return [
        "#{:02x}{:02x}{:02x}".format(*ligne) if fini else GRIS_DONNEES_INSUFFISANTES
        for ligne, fini in zip(melange.tolist(), finies)
    ]


@dataclass
class EchelleCouleur:
    """Correspondance valeur -> position 0-1 dans le degrade des prix.

    L'echelle est **lineaire et absolue** : une couleur designe toujours le
    meme prix, comparable d'un filtre a l'autre et d'une capture d'ecran a la
    suivante. C'est la repartition de la palette elle-meme (voir
    `PALETTE_SEQUENTIELLE`) qui determine ou basculent le jaune et l'orange.

    Les bornes sont prises aux 5e et 95e centiles plutot qu'aux extremes : une
    section hors norme ne doit pas tasser toutes les autres dans le vert. Les
    valeurs situees au-dela sont simplement saturees a la couleur de bout.
    """

    bas: float
    haut: float
    #: Aucune donnee : tout est grise plutot que colore arbitrairement.
    vide: bool = False

    @classmethod
    def depuis(cls, valeurs: pd.Series | Sequence[float]) -> "EchelleCouleur":
        series = pd.to_numeric(pd.Series(list(valeurs)), errors="coerce").dropna()
        if series.empty:
            return cls(bas=0.0, haut=1.0, vide=True)
        bas, haut = bornes_couleur(series)
        return cls(bas=bas, haut=haut)

    def position(self, valeur: float | None) -> float:
        if self.vide or _absente(valeur):
            return float("nan")
        if self.haut <= self.bas:
            return 0.5
        return float((float(valeur) - self.bas) / (self.haut - self.bas))

    def graduations(self, nombre: int = 5) -> list[float]:
        """Valeurs jalonnant le degrade, a positions regulierement espacees."""

        nombre = max(int(nombre), 2)
        return [self.bas + i / (nombre - 1) * (self.haut - self.bas) for i in range(nombre)]

    def couleur(self, valeur: float | None) -> str:
        return _interpoler(PALETTE_SEQUENTIELLE, self.position(valeur))

    def couleurs(self, valeurs) -> list[str]:
        """`couleur` sur toute une colonne, sans boucle Python par valeur."""

        reels = pd.to_numeric(pd.Series(valeurs), errors="coerce").to_numpy(dtype=float)
        if self.vide:
            return [GRIS_DONNEES_INSUFFISANTES] * len(reels)
        if self.haut <= self.bas:
            positions = np.where(np.isfinite(reels), 0.5, np.nan)
        else:
            positions = (reels - self.bas) / (self.haut - self.bas)
        return interpoler_serie(PALETTE_SEQUENTIELLE, positions)


def bornes_couleur(valeurs: pd.Series, quantiles: tuple[float, float] = (0.05, 0.95)) -> tuple[float, float]:
    """Bornes robustes : les extremes ne doivent pas ecraser le degrade."""

    valeurs = pd.to_numeric(valeurs, errors="coerce").dropna()
    if valeurs.empty:
        return 0.0, 1.0
    bas, haut = valeurs.quantile(quantiles[0]), valeurs.quantile(quantiles[1])
    if not np.isfinite(bas) or not np.isfinite(haut) or haut <= bas:
        bas, haut = valeurs.min(), valeurs.max()
    if haut <= bas:
        # Toutes les sections au meme prix (ou une seule section) : il n'y a
        # aucune amplitude a representer. On centre l'intervalle sur la valeur
        # pour qu'elle tombe au milieu du degrade — la colorer en vert fonce
        # laisserait croire a un territoire bon marche.
        milieu = float(bas)
        return milieu - 0.5, milieu + 0.5
    return float(bas), float(haut)


# --------------------------------------------------------------------------
# Mise en forme
# --------------------------------------------------------------------------


def formater_euros(valeur: float | None, suffixe: str = " €") -> str:
    if _absente(valeur):
        return "n/d"
    valeur = float(valeur)
    return f"{valeur:,.0f}".replace(",", " ") + suffixe


def formater_taux_annuel(valeur: float | None) -> str:
    if _absente(valeur):
        return "n/d"
    # Virgule decimale : toute l'interface est en francais.
    return f"{float(valeur):+.1f} %/an".replace(".", ",")
