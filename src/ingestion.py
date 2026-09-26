"""Ingestion et nettoyage des donnees DVF (Etalab).

Le module ne lit pas de fichiers : il recoit des lignes DVF deja chargees —
`src/telechargement.py` les tire des fichiers departementaux geo-dvf — les
normalise, puis reconstruit une ligne par *mutation* (une vente) exploitable
pour le calcul du prix au m2. Il gere aussi les territoires precalcules, ou
cette reconstruction est figee dans un parquet.

Les pieges DVF pris en charge :

* une mutation (`id_mutation`) occupe plusieurs lignes : un local principal,
  des dependances (cave, parking), parfois plusieurs lots ;
* la `valeur_fonciere` est repetee a l'identique sur toutes ces lignes : il
  faut la prendre une seule fois, jamais la sommer ;
* certaines lignes sont des doublons stricts (meme local repete pour chaque lot
  ou chaque parcelle) : les sommer diviserait le prix au m2 par deux ;
* les valeurs manquantes arrivent sous forme de chaines `"None"` / `"nan"` ;
* le separateur decimal varie selon l'export ;
* une mutation peut s'etaler sur plusieurs sections cadastrales ;
* les ventes symboliques (1 EUR) et les mutations ou la valeur couvre bien plus
  que la surface residentielle produisent des prix au m2 aberrants.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# --------------------------------------------------------------------------
# Constantes metier
# --------------------------------------------------------------------------

#: Typologies conservees par defaut (immobilier residentiel uniquement).
TYPES_RESIDENTIELS: tuple[str, ...] = ("Appartement", "Maison")

#: Type d'une mutation qui reunit une maison et un appartement.
TYPE_MIXTE = "Mixte"

#: Libelle DVF des locaux d'activite (detecte de maniere tolerante).
MOTIF_LOCAL_ACTIVITE = re.compile(r"local\s+industriel", re.IGNORECASE)

#: Natures de mutation conservees par defaut (les echanges, adjudications et
#: expropriations ne refletent pas un prix de marche).
NATURES_MUTATION_DEFAUT: tuple[str, ...] = (
    "Vente",
    "Vente en l'état futur d'achèvement",
)

#: Marqueurs de valeur manquante rencontres dans les exports DVF.
NA_VALUES: tuple[str, ...] = ("None", "nan", "NaN", "NULL", "null", "", " ", "-")

#: Colonnes indispensables au pipeline.
COLONNES_REQUISES: tuple[str, ...] = (
    "id_mutation",
    "date_mutation",
    "valeur_fonciere",
    "code_commune",
    "type_local",
    "surface_reelle_bati",
)

COLONNES_NUMERIQUES: tuple[str, ...] = (
    "valeur_fonciere",
    "surface_reelle_bati",
    "surface_terrain",
    "nombre_pieces_principales",
    "nombre_lots",
    "longitude",
    "latitude",
    "lot1_surface_carrez",
    "lot2_surface_carrez",
    "lot3_surface_carrez",
    "lot4_surface_carrez",
    "lot5_surface_carrez",
)

#: Colonnes identifiant un local au sein d'une mutation. Deux lignes identiques
#: sur ces colonnes decrivent le meme bien vu sous deux lots / parcelles.
CLES_DEDOUBLONNAGE: tuple[str, ...] = (
    "id_mutation",
    "id_parcelle",
    "type_local",
    "surface_reelle_bati",
    "nombre_pieces_principales",
)


@dataclass(frozen=True)
class OptionsNettoyage:
    """Parametres du nettoyage, tous ajustables depuis l'interface."""

    types_locaux: Sequence[str] = TYPES_RESIDENTIELS
    natures_mutation: Sequence[str] | None = NATURES_MUTATION_DEFAUT
    prix_m2_min: float = 500.0
    prix_m2_max: float = 30_000.0
    valeur_fonciere_min: float = 1_000.0
    surface_min: float = 8.0
    exclure_mutations_avec_local_activite: bool = True
    exclure_ventes_en_bloc: bool = True
    #: Nombre de logements a partir duquel une mutation est une vente en bloc
    #: (immeuble entier, portefeuille) : son prix au m2 ne reflete pas le
    #: marche du logement a l'unite.
    seuil_vente_en_bloc: int = 3
    #: Une mutation qui reunit une maison **et** un appartement (moins d'une
    #: vente sur mille) n'a pas de type a elle : son prix au m2 melange deux
    #: marches.
    exclure_ventes_mixtes: bool = True
    dedoublonner: bool = True


@dataclass
class RapportQualite:
    """Trace de ce que le pipeline a retire, etape par etape."""

    fichiers: list[str] = field(default_factory=list)
    lignes_brutes: int = 0
    lignes_apres_nature: int = 0
    lignes_residentielles: int = 0
    doublons_supprimes: int = 0
    mutations_brutes: int = 0
    mutations_multi_sections: int = 0
    mutations_avec_local_activite: int = 0
    surfaces_reprises_carrez: int = 0
    rejets: dict[str, int] = field(default_factory=dict)
    mutations_finales: int = 0
    sans_coordonnees: int = 0
    avertissements: list[str] = field(default_factory=list)

    @property
    def taux_retenu(self) -> float:
        if not self.mutations_brutes:
            return 0.0
        return self.mutations_finales / self.mutations_brutes


# --------------------------------------------------------------------------
# Typage des colonnes DVF
# --------------------------------------------------------------------------


def racine_projet() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _en_flottant(serie: pd.Series) -> pd.Series:
    """Convertit en `float64` numpy, quel que soit le type d'arrivee.

    `astype("float64")` ne suffit pas : sur un `Int64` contenant des valeurs
    manquantes, il leve plutot que de produire des `NaN` selon la version de
    pandas. `to_numpy(na_value=...)` est explicite et se comporte pareil
    partout.
    """

    valeurs = pd.to_numeric(serie, errors="coerce")
    return pd.Series(
        valeurs.to_numpy(dtype="float64", na_value=np.nan), index=serie.index, name=serie.name
    )


def _vers_numerique(serie: pd.Series) -> pd.Series:
    """Conversion numerique tolerante au separateur decimal virgule."""

    nettoyee = serie.astype("string").str.strip()
    valeurs = pd.to_numeric(nettoyee, errors="coerce")
    a_reprendre = valeurs.isna() & nettoyee.notna()
    if a_reprendre.any():
        secours = (
            nettoyee[a_reprendre]
            .str.replace(r"\s| |\xa0", "", regex=True)
            .str.replace(",", ".", regex=False)
        )
        # Une colonne de nombres ronds devient un entier nullable : y ecrire
        # « 50,5 » echouerait. On repasse en flottant avant de completer, car
        # c'est precisement une decimale qu'on s'apprete a y mettre.
        #
        # Les deux cotes doivent etre des flottants *numpy* : `to_numeric` sur
        # une colonne `string` rend un `Float64` nullable, et ecrire un
        # `Float64` dans un bloc `float64` leve des pandas 3 des qu'une valeur
        # de secours reste manquante — c'est-a-dire des qu'une cellule n'est ni
        # un nombre ni un nombre a virgule decimale, ce qui arrive dans les
        # DVF reelles.
        valeurs = _en_flottant(valeurs)
        valeurs.loc[a_reprendre] = _en_flottant(pd.to_numeric(secours, errors="coerce"))
    return valeurs


def typer_colonnes_dvf(brut: pd.DataFrame, origine: str = "") -> pd.DataFrame:
    """Met en forme des lignes DVF lues en texte : colonnes, types, blancs.

    Appelee par `src/telechargement.py` sur les fichiers departementaux
    geo-dvf, qui arrivent en `dtype=str`. Le typage est isole ici plutot que
    dans le lecteur : c'est lui qui decide du resultat de tout le pipeline, et
    il doit pouvoir etre teste sur des lignes construites a la main.
    """

    brut.columns = [str(col).strip().lower() for col in brut.columns]
    brut = brut.loc[:, ~brut.columns.duplicated()]

    manquantes = [col for col in COLONNES_REQUISES if col not in brut.columns]
    if manquantes:
        raise ValueError(
            f"{origine or 'Donnees DVF'} : colonnes DVF absentes {manquantes}. "
            "Le fichier n'a pas le format DVF attendu (source : "
            "https://files.data.gouv.fr/geo-dvf/)."
        )

    for colonne in COLONNES_NUMERIQUES:
        if colonne in brut.columns:
            brut[colonne] = _vers_numerique(brut[colonne])

    for colonne in ("type_local", "nature_mutation", "nom_commune", "section_prefixe"):
        if colonne in brut.columns:
            brut[colonne] = brut[colonne].astype("string").str.strip()

    brut["_fichier"] = origine
    return brut


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def _code_section(df: pd.DataFrame) -> pd.Series:
    """Identifiant unique de section : code INSEE + prefixe + lettres.

    Le meme libelle de section (`000AB`) existe dans plusieurs communes : la
    cle doit donc toujours embarquer le code commune. `id_parcelle` fait foi
    quand il est present (11 premiers caracteres = commune + prefixe + section),
    sinon on retombe sur `section_prefixe`.
    """

    depuis_parcelle = pd.Series(pd.NA, index=df.index, dtype="string")
    if "id_parcelle" in df.columns:
        parcelle = df["id_parcelle"].astype("string").str.strip()
        valide = parcelle.str.len() >= 10
        depuis_parcelle = parcelle.where(valide).str[:10]

    commune = df["code_commune"].astype("string").str.strip().str.zfill(5)
    if "section_prefixe" in df.columns:
        section = df["section_prefixe"].astype("string").str.strip().str.zfill(5)
        depuis_colonne = commune + section
    else:
        depuis_colonne = pd.Series(pd.NA, index=df.index, dtype="string")

    return depuis_parcelle.fillna(depuis_colonne)


def normaliser(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute les colonnes derivees utilisees par tout le reste du projet."""

    df = df.copy()
    df["date_mutation"] = pd.to_datetime(df["date_mutation"], errors="coerce", format="mixed")
    df["annee"] = df["date_mutation"].dt.year.astype("Int64")
    df["code_section"] = _code_section(df)
    df["section_courte"] = df["code_section"].str[5:].str.lstrip("0")
    df["code_commune"] = df["code_commune"].astype("string").str.strip().str.zfill(5)
    if "nom_commune" not in df.columns:
        df["nom_commune"] = df["code_commune"]
    df["nom_commune"] = df["nom_commune"].fillna(df["code_commune"])
    df["est_local_activite"] = (
        df["type_local"].astype("string").fillna("").str.match(MOTIF_LOCAL_ACTIVITE)
    )
    return df


def _adresse(df: pd.DataFrame) -> pd.Series:
    """Reconstitue une adresse lisible a partir des colonnes DVF."""

    morceaux: list[pd.Series] = []
    for colonne, formatteur in (
        ("adresse_numero", lambda s: s.str.replace(r"\.0$", "", regex=True)),
        ("adresse_suffixe", lambda s: s),
        ("adresse_nom_voie", lambda s: s.str.title()),
    ):
        if colonne in df.columns:
            morceaux.append(formatteur(df[colonne].astype("string").fillna("").str.strip()))
    if not morceaux:
        return pd.Series("", index=df.index, dtype="string")
    adresse = morceaux[0]
    for suite in morceaux[1:]:
        adresse = (adresse + " " + suite).str.strip()
    return adresse.str.replace(r"\s+", " ", regex=True).str.strip()


# --------------------------------------------------------------------------
# Agregation par mutation
# --------------------------------------------------------------------------


def _premiere_valeur_valide(serie: pd.Series):
    valides = serie.dropna()
    return valides.iloc[0] if len(valides) else pd.NA


def agreger_par_mutation(
    df: pd.DataFrame, options: OptionsNettoyage, rapport: RapportQualite
) -> pd.DataFrame:
    """Reconstruit une ligne par vente a partir des lignes DVF.

    La surface est la somme des locaux principaux *dedoublonnes*, la valeur
    fonciere est prise une seule fois (elle est repetee sur chaque ligne de la
    mutation), et la mutation est rattachee a la section qui porte le plus de
    surface residentielle.
    """

    mutations_avec_activite = set(
        df.loc[df["est_local_activite"].fillna(False), "id_mutation"].unique()
    )

    # Valeur fonciere : une seule par mutation. On retient le maximum pour
    # rester robuste si un export presente plusieurs dispositions.
    valeurs = (
        df.groupby("id_mutation", dropna=True)["valeur_fonciere"].max().rename("valeur_fonciere")
    )
    incoherentes = df.groupby("id_mutation")["valeur_fonciere"].nunique(dropna=True)
    nb_incoherentes = int((incoherentes > 1).sum())
    if nb_incoherentes:
        rapport.avertissements.append(
            f"{nb_incoherentes} mutation(s) presentent plusieurs valeurs foncieres "
            "distinctes ; la plus elevee a ete retenue."
        )

    residentiel = df[df["type_local"].isin(list(options.types_locaux))].copy()
    rapport.lignes_residentielles = len(residentiel)

    # Seules comptent les mutations qui **melent** logement et local d'activite :
    # une mutation purement commerciale n'entre de toute facon pas dans le
    # perimetre residentiel, et l'annoncer gonflerait le chiffre sans que
    # l'option « exclure les locaux d'activite » y change quoi que ce soit.
    rapport.mutations_avec_local_activite = len(
        mutations_avec_activite & set(residentiel["id_mutation"].unique())
    )

    if options.dedoublonner:
        cles = [c for c in CLES_DEDOUBLONNAGE if c in residentiel.columns]
        avant = len(residentiel)
        residentiel = residentiel.drop_duplicates(subset=cles)
        rapport.doublons_supprimes = avant - len(residentiel)

    residentiel["adresse"] = _adresse(residentiel)
    residentiel["surface_carrez"] = (
        residentiel[
            [c for c in residentiel.columns if c.startswith("lot") and c.endswith("surface_carrez")]
        ].sum(axis=1, min_count=1)
        if any(c.endswith("surface_carrez") for c in residentiel.columns)
        else np.nan
    )

    # Section de rattachement : celle qui concentre le plus de surface batie.
    surface_par_section = (
        residentiel.groupby(["id_mutation", "code_section"], dropna=False)["surface_reelle_bati"]
        .sum(min_count=1)
        .reset_index()
        .sort_values(["id_mutation", "surface_reelle_bati"], ascending=[True, False])
    )
    rapport.mutations_multi_sections = int(
        (surface_par_section.groupby("id_mutation").size() > 1).sum()
    )
    section_principale = (
        surface_par_section.drop_duplicates("id_mutation")
        .set_index("id_mutation")["code_section"]
        .rename("code_section")
    )

    # La ligne « principale » (le plus grand local) porte l'adresse, le type et
    # le nombre de pieces affiches dans le detail d'une section.
    principale = (
        residentiel.sort_values("surface_reelle_bati", ascending=False)
        .drop_duplicates("id_mutation")
        .set_index("id_mutation")
    )

    agregats = residentiel.groupby("id_mutation").agg(
        date_mutation=("date_mutation", "min"),
        nature_mutation=("nature_mutation", _premiere_valeur_valide),
        code_commune=("code_commune", _premiere_valeur_valide),
        nom_commune=("nom_commune", _premiere_valeur_valide),
        surface_bati=("surface_reelle_bati", "sum"),
        surface_carrez=("surface_carrez", "sum"),
        nb_pieces=("nombre_pieces_principales", "sum"),
        nb_locaux=("type_local", "size"),
        types_locaux=("type_local", lambda s: sorted(set(s.dropna()))),
        longitude=("longitude", "mean"),
        latitude=("latitude", "mean"),
    )

    mutations = agregats.join(valeurs, how="left")
    mutations["code_section"] = section_principale
    mutations["type_bien"] = mutations["types_locaux"].apply(
        lambda types: types[0] if len(types) == 1 else TYPE_MIXTE
    )
    # Colonne de travail remplacee par du texte : les listes ne se serialisent
    # pas proprement dans le parquet.
    mutations["types_locaux"] = mutations["types_locaux"].apply(", ".join)
    mutations["adresse"] = principale["adresse"]
    mutations["id_parcelle"] = principale.get("id_parcelle")
    mutations["section_courte"] = principale["section_courte"]
    mutations["contient_local_activite"] = mutations.index.isin(mutations_avec_activite)

    # Dependances vendues avec le bien (cave, parking) : leur valeur est incluse
    # dans la valeur fonciere mais leur surface n'entre pas au denominateur.
    dependances = (
        df[df["type_local"].astype("string").str.lower() == "dépendance"]
        .groupby("id_mutation")
        .size()
        .rename("nb_dependances")
    )
    mutations = mutations.join(dependances, how="left")
    mutations["nb_dependances"] = mutations["nb_dependances"].fillna(0).astype(int)

    mutations["annee"] = mutations["date_mutation"].dt.year.astype("Int64")
    return mutations.reset_index()


# --------------------------------------------------------------------------
# Filtres qualite
# --------------------------------------------------------------------------


#: Grandeurs continues du referentiel de mutations. Elles doivent etre des
#: `float64` *avant* tout calcul, pas seulement a la sortie.
#:
#: Les DVF livrent souvent des surfaces rondes (« 60 »), dont pandas fait un
#: entier nullable `Int64`. Affecter alors une surface Carrez fractionnaire
#: dans `surface_bati` — c'est le repli documente plus bas — echoue avec un
#: « cannot safely cast non-equivalent object to int64 » : un entier ne peut
#: pas accueillir 50,5 m2. L'aller-retour parquet d'un territoire preserve
#: fidelement le type entier : tout passe donc en flottant.
COLONNES_CONTINUES: tuple[str, ...] = (
    "valeur_fonciere",
    "surface_bati",
    "surface_carrez",
    "nb_pieces",
    "nb_locaux",
    "nb_dependances",
    "longitude",
    "latitude",
    "prix_m2",
)


def appliquer_filtres_qualite(
    mutations: pd.DataFrame, options: OptionsNettoyage, rapport: RapportQualite
) -> pd.DataFrame:
    """Retire les mutations inexploitables et calcule le prix au m2.

    Les regles s'appliquent dans l'ordre et chacune ne compte que ce qu'elle
    retire parmi ce qui restait. Elles alimentent un seul masque, et la copie
    du tableau n'a lieu qu'une fois, a la fin : sur 650 000 ventes, une copie
    par regle doublerait la memoire occupee au chargement.
    """

    df = mutations.copy()
    for colonne in COLONNES_CONTINUES:
        if colonne in df.columns:
            df[colonne] = _en_flottant(df[colonne])

    rapport.mutations_brutes = len(df)
    rejets: dict[str, int] = {}
    garde = np.ones(len(df), dtype=bool)

    def retirer(masque: pd.Series, libelle: str) -> None:
        nonlocal garde
        masque = masque.fillna(False).to_numpy(dtype=bool)
        nb = int((masque & garde).sum())
        if nb:
            rejets[libelle] = nb
        garde &= ~masque

    # Le parquet d'un territoire conserve toutes les natures de mutation : le
    # tri se fait ici.
    if options.natures_mutation and "nature_mutation" in df.columns:
        retirer(
            ~df["nature_mutation"].isin(list(options.natures_mutation)),
            "Nature de mutation ecartee (echange, adjudication, expropriation…)",
        )

    # Repli sur la surface Carrez : certains appartements sont declares sans
    # `surface_reelle_bati` alors que la surface de leurs lots est renseignee.
    surface_absente = df["surface_bati"].isna() | (df["surface_bati"] <= 0)
    if "surface_carrez" in df.columns:
        reprises = (
            surface_absente & df["surface_carrez"].notna() & (df["surface_carrez"] > 0)
        ).fillna(False)
        rapport.surfaces_reprises_carrez = int(reprises.sum())
        if rapport.surfaces_reprises_carrez:
            df.loc[reprises, "surface_bati"] = df.loc[reprises, "surface_carrez"]

    retirer(df["date_mutation"].isna(), "Date de mutation invalide")
    retirer(df["valeur_fonciere"].isna(), "Valeur fonciere absente")
    retirer(df["valeur_fonciere"] <= 0, "Valeur fonciere nulle ou negative")
    retirer(
        df["valeur_fonciere"] < options.valeur_fonciere_min,
        f"Valeur fonciere < {options.valeur_fonciere_min:,.0f} EUR (vente symbolique)".replace(
            ",", " "
        ),
    )
    retirer(df["surface_bati"].isna() | (df["surface_bati"] <= 0), "Surface batie nulle ou absente")
    retirer(df["surface_bati"] < options.surface_min, f"Surface < {options.surface_min:g} m2")
    retirer(df["code_section"].isna(), "Section cadastrale indeterminee")

    if options.exclure_mutations_avec_local_activite:
        retirer(
            df["contient_local_activite"],
            "Mutation incluant un local d'activite (valeur non imputable au logement)",
        )

    if options.exclure_ventes_en_bloc:
        retirer(
            df["nb_locaux"] >= options.seuil_vente_en_bloc,
            f"Vente en bloc (>= {options.seuil_vente_en_bloc} logements dans la meme mutation)",
        )

    if options.exclure_ventes_mixtes and "type_bien" in df.columns:
        retirer(
            df["type_bien"] == TYPE_MIXTE,
            "Vente mixte (maison et appartement dans la meme mutation)",
        )

    prix_m2 = df["valeur_fonciere"] / df["surface_bati"]
    retirer(prix_m2 < options.prix_m2_min, f"Prix/m2 < {options.prix_m2_min:g} EUR")
    retirer(prix_m2 > options.prix_m2_max, f"Prix/m2 > {options.prix_m2_max:g} EUR")

    df = df[garde]
    df["prix_m2"] = prix_m2[garde].round(0)

    rapport.rejets = rejets
    rapport.mutations_finales = len(df)
    rapport.sans_coordonnees = int(df["longitude"].isna().sum())

    # Types simples en sortie : `pd.NA` fait echouer les formateurs et la
    # serialisation vers la carte. Les nombres passent en `float64` (`NaN`),
    # le texte en chaines ou l'absence est `""`.
    for colonne in (*COLONNES_CONTINUES, "annee"):
        if colonne in df.columns:
            df[colonne] = _en_flottant(df[colonne])
    df["annee"] = df["annee"].astype("Int64")

    for colonne in df.select_dtypes(include=["string", "object"]).columns:
        if colonne == "date_mutation":
            continue
        df[colonne] = df[colonne].fillna("").astype(str)

    return df.sort_values("date_mutation").reset_index(drop=True)


def reconstruire_mutations(
    brut: pd.DataFrame, options: OptionsNettoyage, rapport: RapportQualite
) -> pd.DataFrame:
    """Des lignes DVF brutes a une ligne par vente, sans filtre de qualite.

    C'est l'etape couteuse du pipeline — normalisation, dedoublonnage,
    agregation par mutation — et la seule que `scripts/preparer_territoire.py`
    fige dans un parquet. Les filtres de qualite, eux, restent appliques a
    chaque interaction : ils sont bon marche et pilotes depuis la barre
    laterale.
    """

    df = normaliser(brut)
    if options.natures_mutation:
        df = df[df["nature_mutation"].isin(list(options.natures_mutation))]
    rapport.lignes_apres_nature = len(df)

    if df.empty:
        return pd.DataFrame(columns=["id_mutation", "code_section"])
    return agreger_par_mutation(df, options, rapport)


# --------------------------------------------------------------------------
# Territoires precalcules (parquet)
# --------------------------------------------------------------------------
#
# Lire et reconstruire les mutations d'une agglomeration entiere a chaque
# demarrage couterait des dizaines de secondes et plusieurs centaines de Mo de
# memoire. `scripts/preparer_territoire.py` fige donc l'etape couteuse
# (normalisation, dedoublonnage, agregation par mutation) dans un parquet, une
# fois pour toutes, et l'application ne fait que poser les filtres de qualite.
#
# Ce qui est fige : le dedoublonnage et le perimetre residentiel
# (Appartement / Maison). Ce qui reste parametrable (`OptionsNettoyage`) :
# bornes de plausibilite, valeur fonciere minimale, surface minimale,
# exclusion des locaux d'activite et des ventes en bloc, et natures de
# mutation — le parquet les conserve **toutes** pour que ce filtre garde un
# sens.

#: Nom du parquet d'un territoire, dans `data/territoires/<cle>/`.
FICHIER_MUTATIONS = "mutations.parquet"

#: Trace de la preparation : comptages amont, annees couvertes, provenance.
FICHIER_PREPARATION = "preparation.json"


def repertoire_territoires() -> str:
    """Repertoire des territoires, surchargeable via `DVF_TERRITOIRES_DIR`."""

    return os.environ.get("DVF_TERRITOIRES_DIR") or os.path.join(
        racine_projet(), "data", "territoires"
    )


def repertoire_territoire(cle: str) -> str:
    return os.path.join(repertoire_territoires(), cle)


def fichier_mutations(cle: str) -> str:
    return os.path.join(repertoire_territoire(cle), FICHIER_MUTATIONS)


def fichier_preparation(cle: str) -> str:
    return os.path.join(repertoire_territoire(cle), FICHIER_PREPARATION)


def territoire_prepare(cle: str) -> bool:
    """Vrai si le parquet du territoire est disponible localement."""

    return os.path.exists(fichier_mutations(cle))


def territoires_prepares() -> list[str]:
    """Cles des territoires dont les donnees sont deja figees."""

    racine = repertoire_territoires()
    if not os.path.isdir(racine):
        return []
    return sorted(
        nom
        for nom in os.listdir(racine)
        if os.path.exists(os.path.join(racine, nom, FICHIER_MUTATIONS))
    )


def ecrire_territoire(
    cle: str,
    mutations: pd.DataFrame,
    rapport: RapportQualite,
    metadonnees: dict | None = None,
) -> str:
    """Fige les mutations d'un territoire et la trace de leur preparation."""

    repertoire = repertoire_territoire(cle)
    os.makedirs(repertoire, exist_ok=True)

    chemin = fichier_mutations(cle)
    mutations.to_parquet(chemin, index=False, compression="zstd")

    trace = {
        "territoire": cle,
        "lignes_brutes": rapport.lignes_brutes,
        "lignes_residentielles": rapport.lignes_residentielles,
        "doublons_supprimes": rapport.doublons_supprimes,
        "mutations_multi_sections": rapport.mutations_multi_sections,
        "mutations_avec_local_activite": rapport.mutations_avec_local_activite,
        "surfaces_reprises_carrez": rapport.surfaces_reprises_carrez,
        "mutations": len(mutations),
        "fichiers": rapport.fichiers,
        "avertissements": rapport.avertissements,
        **(metadonnees or {}),
    }
    with open(fichier_preparation(cle), "w", encoding="utf-8") as flux:
        json.dump(trace, flux, ensure_ascii=False, indent=2, sort_keys=True)
    return chemin


def lire_preparation(cle: str) -> dict:
    """Trace de preparation d'un territoire, ou un dictionnaire vide."""

    try:
        with open(fichier_preparation(cle), "r", encoding="utf-8") as flux:
            return json.load(flux)
    except (OSError, ValueError):
        return {}


#: Colonnes du parquet lues par l'application : celles qu'exigent les filtres
#: de qualite, et celles qu'utilisent la carte et ses statistiques.
COLONNES_LUES: tuple[str, ...] = (
    "id_mutation",
    "date_mutation",
    "annee",
    "nature_mutation",
    "code_commune",
    "nom_commune",
    "code_section",
    "section_courte",
    "type_bien",
    "adresse",
    "valeur_fonciere",
    "surface_bati",
    "surface_carrez",
    "nb_pieces",
    "nb_locaux",
    "contient_local_activite",
    "longitude",
    "latitude",
)

#: Colonnes gardees une fois les filtres poses : tout le reste ne sert qu'au
#: nettoyage. Un territoire comme le Grand Paris compte 650 000 ventes, et
#: chaque colonne de texte superflue coute une dizaine de mega-octets.
COLONNES_EXPLOITEES: tuple[str, ...] = (
    "id_mutation",
    "date_mutation",
    "annee",
    "code_commune",
    "nom_commune",
    "code_section",
    "section_courte",
    "type_bien",
    "adresse",
    "valeur_fonciere",
    "surface_bati",
    "prix_m2",
    "nb_pieces",
    "longitude",
    "latitude",
)


def charger_mutations_territoire(
    cle: str, colonnes: Sequence[str] | None = None
) -> tuple[pd.DataFrame, RapportQualite]:
    """Relit le parquet d'un territoire et reconstitue son rapport amont.

    `colonnes` restreint la lecture : les colonnes absentes du parquet sont
    ignorees plutot que de faire echouer la lecture.
    """

    chemin = fichier_mutations(cle)
    if not os.path.exists(chemin):
        raise FileNotFoundError(
            f"Territoire « {cle} » non prepare : {chemin} est absent. Lancez "
            f"`python scripts/preparer_territoire.py --territoire {cle}` pour le "
            "construire a partir des DVF geolocalisees."
        )
    if colonnes is not None:
        presentes = set(pq.read_schema(chemin).names)
        colonnes = [colonne for colonne in colonnes if colonne in presentes]
    mutations = pd.read_parquet(chemin, columns=colonnes)

    trace = lire_preparation(cle)
    rapport = RapportQualite(
        fichiers=list(trace.get("fichiers") or [os.path.basename(chemin)]),
        lignes_brutes=int(trace.get("lignes_brutes") or 0),
        lignes_apres_nature=int(trace.get("lignes_brutes") or 0),
        lignes_residentielles=int(trace.get("lignes_residentielles") or 0),
        doublons_supprimes=int(trace.get("doublons_supprimes") or 0),
        mutations_multi_sections=int(trace.get("mutations_multi_sections") or 0),
        mutations_avec_local_activite=int(trace.get("mutations_avec_local_activite") or 0),
        surfaces_reprises_carrez=int(trace.get("surfaces_reprises_carrez") or 0),
        avertissements=list(trace.get("avertissements") or []),
    )
    return mutations, rapport


def construire_transactions_territoire(
    cle: str, options: OptionsNettoyage | None = None
) -> tuple[pd.DataFrame, RapportQualite]:
    """Point d'entree du pipeline pour un territoire precalcule."""

    options = options or OptionsNettoyage()
    mutations, rapport = charger_mutations_territoire(cle, COLONNES_LUES)
    if mutations.empty:
        return pd.DataFrame(columns=["id_mutation", "prix_m2"]), rapport
    transactions = appliquer_filtres_qualite(mutations, options, rapport)
    del mutations
    return transactions[[c for c in COLONNES_EXPLOITEES if c in transactions.columns]], rapport
