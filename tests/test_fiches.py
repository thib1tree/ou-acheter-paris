"""Les pages par commune : leurs chiffres (`src/fiches.py`) et leur mise en
page (`src/pages.py`), puis ce que le site en publie.

Ce qui est verrouille ici : qu'aucune statistique n'est publiee sous le seuil
(ces pages sont indexees, a la difference des donnees de la carte), que les
deux fenetres de temps sont celles annoncees, qu'une adresse ne designe jamais
deux communes, et que les pages tiennent dans la politique de securite du site.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
sys.path.insert(0, str(RACINE / "scripts"))

import construire_site  # noqa: E402

from src import fiches, pages  # noqa: E402


def _ventes(lignes: list[dict]) -> pd.DataFrame:
    """Des ventes d'une commune, avec ce que `fiches.fiche` lit."""

    base = {
        "code_commune": "93048", "nom_commune": "Montreuil", "code_section": "93048000AB",
        "section_courte": "AB", "type_bien": "Appartement", "etat": "Ancien", "nb_pieces": 2,
        "surface_bati": 50.0,
    }
    ventes = pd.DataFrame([{**base, **ligne} for ligne in lignes])
    ventes["date_mutation"] = pd.to_datetime(ventes["date_mutation"])
    ventes["annee"] = ventes["date_mutation"].dt.year.astype("Int64")
    ventes["valeur_fonciere"] = ventes["prix_m2"] * ventes["surface_bati"]
    ventes["id_mutation"] = [f"m{i}" for i in range(len(ventes))]
    return ventes


FIN = pd.Timestamp("2026-06-30")


# --------------------------------------------------------------------------
# Noms et adresses
# --------------------------------------------------------------------------


def test_le_nom_d_une_commune_devient_une_adresse_lisible():
    assert fiches.identifiant("L'Haÿ-les-Roses") == "l-hay-les-roses"
    assert fiches.identifiant("Paris 11e Arrondissement") == "paris-11e"
    assert fiches.identifiant("Saint-Maur-des-Fossés") == "saint-maur-des-fosses"
    assert fiches.nom_affiche("Paris 1er Arrondissement") == "Paris 1er"


def test_une_adresse_ne_designe_jamais_deux_communes():
    """Deux homonymes prennent le numero de leur departement."""

    noms = {"93048": "Montreuil", "78418": "Montreuil", "75111": "Paris 11e Arrondissement"}
    assert fiches.identifiants(noms) == {
        "93048": "montreuil-93", "78418": "montreuil-78", "75111": "paris-11e",
    }
    with pytest.raises(ValueError):
        fiches.identifiants({"93048": "Montreuil", "93049": "Montreuil"})


def test_la_preposition_suit_le_nom():
    assert fiches.a_commune("Montreuil") == "à Montreuil"
    assert fiches.a_commune("Le Blanc-Mesnil") == "au Blanc-Mesnil"
    assert fiches.a_commune("Les Lilas") == "aux Lilas"
    assert fiches.a_commune("La Courneuve") == "à La Courneuve"
    assert fiches.a_commune("L'Haÿ-les-Roses") == "à L'Haÿ-les-Roses"
    assert fiches.a_commune("Paris 11e Arrondissement") == "à Paris 11e"


# --------------------------------------------------------------------------
# Chiffres
# --------------------------------------------------------------------------


def test_les_douze_derniers_mois_sont_les_memes_pour_toutes_les_communes():
    recent, precedent = fiches.fenetres(FIN)
    assert (recent, precedent) == (pd.Timestamp("2025-07-01"), pd.Timestamp("2024-07-01"))
    assert pages.periode_recente(FIN) == "juillet 2025 à juin 2026"


def test_aucune_mediane_sous_le_seuil_de_publication():
    """Une mediane sur une poignee de ventes est le prix de l'une d'elles :
    l'effectif se dit, la mediane non."""

    assert fiches.SEUIL_PUBLICATION == 11
    dix = _ventes([{"date_mutation": "2026-01-15", "prix_m2": 5000 + i} for i in range(10)])
    assert fiches.resume(dix) == {"n": 10, "m2": None, "prix": None, "surface": None}
    onze = _ventes([{"date_mutation": "2026-01-15", "prix_m2": 5000 + i} for i in range(11)])
    assert fiches.resume(onze) == {"n": 11, "m2": 5005, "prix": 250250, "surface": 50}


def test_l_evolution_compare_deux_annees_assez_fournies():
    recent = [{"date_mutation": "2026-02-01", "prix_m2": 6300} for _ in range(30)]
    avant = [{"date_mutation": "2025-02-01", "prix_m2": 6000} for _ in range(30)]
    fiche = fiches.fiche(_ventes(recent + avant), FIN)
    appartements = fiche["types"]["Appartement"]
    assert appartements["evolution"] == 5.0
    assert appartements["recent"] == {"n": 30, "m2": 6300, "prix": 315000, "surface": 50}
    assert appartements["periode"]["n"] == 60
    # Une vente de moins l'annee d'avant : plus d'evolution annoncee.
    assert fiches.fiche(_ventes(recent + avant[:-1]), FIN)["types"]["Appartement"]["evolution"] is None
    # Les maisons, absentes : un effectif nul, aucun chiffre.
    assert fiche["types"]["Maison"]["recent"] == {"n": 0, "m2": None, "prix": None, "surface": None}


def test_la_fiche_detaille_annees_pieces_neuf_et_quartiers():
    lignes = (
        [{"date_mutation": "2024-03-01", "prix_m2": 5000, "nb_pieces": 1} for _ in range(12)]
        + [{"date_mutation": "2025-09-01", "prix_m2": 7000, "nb_pieces": 7, "etat": "Neuf (VEFA)",
            "code_section": "93048000AC", "section_courte": "AC"} for _ in range(11)]
        + [{"date_mutation": "2025-10-01", "prix_m2": 9000, "nb_pieces": 3,
            "code_section": "93048000AD", "section_courte": "AD"} for _ in range(3)]
    )
    fiche = fiches.fiche(_ventes(lignes), FIN, {"93048000AB": "Rue de Paris"})

    assert [a["annee"] for a in fiche["annees"]] == [2024, 2025]
    assert fiche["annees"][0]["Appartement"]["m2"] == 5000
    par_taille = {p["pieces"]: p for p in fiche["pieces"]}
    assert par_taille[1]["n"] == 12 and par_taille[1]["m2"] == 5000
    # Sept pieces comptent avec « 5 et plus » ; trois ventes ne font pas une mediane.
    assert par_taille[5]["n"] == 11 and par_taille[5]["m2"] == 7000
    assert par_taille[3] == {"pieces": 3, "n": 3, "m2": None, "prix": None, "surface": None}
    assert fiche["neuf"]["neuf"]["m2"] == 7000 and fiche["neuf"]["ancien"]["n"] == 15
    # Les quartiers publiables, du plus cher au moins cher ; celui de trois
    # ventes n'y est pas.
    assert [(q["section"], q["m2"]) for q in fiche["quartiers"]] == [("AC", 7000), ("AB", 5000)]
    assert fiche["quartiers"][1]["voies"] == "Rue de Paris"
    assert fiche["sections"] == 3


def test_les_communes_proches_sont_les_plus_proches():
    ancres = {"A": (2.30, 48.85, 1.0), "B": (2.31, 48.85, 1.0), "C": (2.40, 48.85, 1.0),
              "D": (2.30, 48.95, 1.0)}
    proches = fiches.communes_proches(ancres, nombre=2)
    assert proches["A"] == ["B", "C"]
    assert proches["C"] == ["B", "A"]
    assert "A" not in proches["A"]


# --------------------------------------------------------------------------
# Mise en page
# --------------------------------------------------------------------------


def _annee(annee, appartement=None, maison=None):
    vide = {"n": 0, "m2": None, "prix": None, "surface": None}
    plein = lambda m2: {"n": 40, "m2": m2, "prix": m2 * 50, "surface": 50}  # noqa: E731
    return {"annee": annee,
            "Appartement": plein(appartement) if appartement else vide,
            "Maison": plein(maison) if maison else vide}


def test_le_graphique_ne_relie_pas_par_dessus_une_annee_sans_chiffre():
    annees = [_annee(2021, 6000), _annee(2022, 6200), _annee(2023), _annee(2024, 5800), _annee(2025, 5900)]
    dessin = pages.graphique(annees, "Prix")
    # Deux troncons, de part et d'autre de 2023 ; quatre points ; une seule
    # serie, donc pas de legende.
    assert dessin.count("<polyline") == 2 and dessin.count("<circle") == 4
    assert 'class="legende"' not in dessin
    assert "Appartements, 2022 : 6 200 €/m² (40 ventes)" in dessin
    # Ni style ni script dans le dessin : la politique de securite les refuserait.
    assert " style=" not in dessin and "<style" not in dessin

    deux = pages.graphique([_annee(2024, 6000, 7000), _annee(2025, 6100, 7100)], "Prix")
    assert 'class="legende"' in deux and "ligne s1" in deux and "ligne s2" in deux
    # Un seul point ne fait pas une evolution.
    assert pages.graphique([_annee(2024, 6000)], "Prix") == ""


def test_les_nombres_se_lisent_en_francais():
    assert pages.euros_m2(6850) == "6 850 €/m²"
    assert pages.euros(None) == "—"
    assert pages.pourcentage(2.14) == "+2,1 %"
    assert pages.pourcentage(-0.4) == "−0,4 %"
    assert pages.pourcentage(0.0) == "stable"
    assert pages.ventes(1) == "1 vente" and pages.ventes(1200) == "1 200 ventes"


# --------------------------------------------------------------------------
# Le site construit
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def communes(site_construit):
    sortie, manifeste = site_construit
    dossier = sortie / pages.DOSSIER
    return dossier, manifeste, sorted(d.name for d in dossier.iterdir() if d.is_dir())


def test_chaque_commune_publiable_a_sa_page_et_la_liste_les_cite_toutes(communes):
    from src.ingestion import construire_transactions_territoire

    dossier, manifeste, noms = communes
    ventes, _ = construire_transactions_territoire("grand-paris")
    par_commune = ventes.groupby("code_commune").size()
    attendues = int((par_commune >= fiches.SEUIL_PUBLICATION).sum())
    assert len(noms) == attendues == manifeste["pages"]["nombre"]
    assert manifeste["pages"]["communes"] == "prix-immobilier/"
    assert {"montreuil", "paris-11e", "le-blanc-mesnil", "l-hay-les-roses"} <= set(noms)

    liste = (dossier / "index.html").read_text(encoding="utf-8")
    assert sorted(re.findall(r'<a href="([a-z0-9-]+)/">', liste)) == noms
    # Par departement, sans classement : Paris dans l'ordre de ses arrondissements.
    assert liste.index('href="paris-2e/"') < liste.index('href="paris-10e/"')
    # La carte y mene, et les moteurs avec elle.
    accueil = (dossier.parent / "index.html").read_text(encoding="utf-8")
    assert 'href="prix-immobilier/"' in accueil


def test_une_page_dit_ses_chiffres_et_ramene_a_la_carte(communes):
    dossier, _, _ = communes
    page = (dossier / "montreuil" / "index.html").read_text(encoding="utf-8")
    assert "<title>Prix immobilier à Montreuil (93) : prix au m², évolution, quartiers</title>" in page
    assert "<h1>Prix de l'immobilier à Montreuil</h1>" in page
    assert "Seine-Saint-Denis (93)" in page
    for ancre in ("evolution", "pieces", "quartiers", "gares", "proches", "sources"):
        assert f'<h2 id="{ancre}">' in page, ancre
    assert re.search(r'<span class="valeur">\d \d{3} €/m²</span>', page)
    assert "<svg" in page and "<table>" in page
    # Le lien vers la carte porte la vue de la commune (`#zoom/lat/lon`).
    assert re.search(r'href="\.\./\.\./#13\.00/48\.8\d+/2\.4\d+"', page)
    assert 'href="../../mentions-legales.html"' in page and 'href="../vincennes/"' in page
    assert "Mairie de Montreuil" in page and "Métro 9" in page
    # Des statistiques, jamais une vente : ni adresse numerotee, ni « la plus chere ».
    assert "Vente la plus" not in page
    assert f"moins de {fiches.SEUIL_PUBLICATION} ventes" in page


def test_un_arrondissement_sans_maisons_ne_montre_que_ses_appartements(communes):
    dossier, _, _ = communes
    page = (dossier / "paris-2e" / "index.html").read_text(encoding="utf-8")
    assert "à Paris 2e" in page and "Paris (75)" in page
    chiffres = page[page.index('<div class="chiffres">'):page.index('<p class="note">')]
    assert "Appartements" in chiffres and "Maisons" not in chiffres
    assert 'class="legende"' not in page
    assert "des appartements vendus chaque année" in page


def test_les_pages_tiennent_dans_la_politique_de_securite(communes):
    """Comme la carte : ni script ni style dans le HTML, aucun gestionnaire
    `on…=`, et un texte qui se laisse agrandir."""

    dossier, _, noms = communes
    chemins = [dossier / "index.html"] + [dossier / nom / "index.html" for nom in noms]
    for chemin in chemins:
        page = chemin.read_text(encoding="utf-8")
        for balise in re.findall(r"<script\b[^>]*>", page):
            assert 'type="application/ld+json"' in balise, (chemin, balise)
        assert not re.search(r"<style\b", page), chemin
        assert not re.search(r"\son[a-z]+\s*=", page), chemin
        assert not re.search(r"\sstyle\s*=", page), chemin
        # Aucune valeur absente ecrite telle quelle par Python.
        assert "{{" not in page and not re.search(r"\b(None|nan|NaN)\b", page), chemin
        viewport = re.search(r'<meta name="viewport" content="([^"]*)"', page).group(1)
        assert "user-scalable" not in viewport and "maximum-scale" not in viewport
        # Sans adresse publique : ni adresse canonique, ni fil d'Ariane.
        assert 'rel="canonical"' not in page and "application/ld+json" not in page
    assert (dossier.parent / "fiches.css").is_file() and (dossier.parent / "textes.css").is_file()


def test_avec_une_adresse_publique_les_pages_entrent_dans_le_plan_du_site(tmp_path):
    adresse = "https://prix.exemple.fr/"
    construire_site.ecrire_referencement(
        tmp_path, adresse, "2026-09-25", ("prix-immobilier/", "prix-immobilier/montreuil/")
    )
    plan = (tmp_path / "sitemap.xml").read_text()
    for chemin in ("", "prix-immobilier/", "prix-immobilier/montreuil/"):
        assert f"<loc>{adresse}{chemin}</loc><lastmod>2026-09-25</lastmod>" in plan

    fiche = fiches.fiche(_ventes(
        [{"date_mutation": "2026-02-01", "prix_m2": 6300} for _ in range(30)]
        + [{"date_mutation": "2025-02-01", "prix_m2": 6000} for _ in range(30)]
    ), FIN)
    commune = {"code": "93048", "nom": "Montreuil", "identifiant": "montreuil", "fiche": fiche,
               "gares": [], "proches": [], "ancre": (2.44, 48.86)}
    contexte = {"nom_site": "Carte", "adresse": adresse, "debut": pd.Timestamp("2021-07-01"),
                "fin": FIN, "url_jeu": "https://exemple.fr/jeu", "url_licence": "https://exemple.fr/licence",
                "jour": "25/09/2026"}
    page = pages.page_commune(commune, contexte)
    assert f'<link rel="canonical" href="{adresse}prix-immobilier/montreuil/" />' in page
    assert '"@type": "BreadcrumbList"' in page and f'"item": "{adresse}prix-immobilier/"' in page
    assert "+5,0 % sur un an" in page
    assert "appartements 6 300 €/m²" in page  # la description
