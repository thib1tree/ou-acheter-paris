"""Le site statique tel que `scripts/construire_site.py` l'ecrit dans `dist/`.

Ce qui est verrouille ici : que le site tient dans les limites de Cloudflare
Pages, que chaque fichier de donnees porte l'empreinte de son contenu (donc se
met en cache sans risque), que le manifeste designe des fichiers qui existent
et dit d'ou viennent les donnees, et que les statistiques d'ouverture sont
celles de Python.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
sys.path.insert(0, str(RACINE / "scripts"))

import construire_site  # noqa: E402

from src import charge  # noqa: E402
from src.ingestion import construire_transactions_territoire  # noqa: E402


@pytest.fixture(scope="module")
def site(site_construit):
    return site_construit


def test_le_site_tient_dans_les_limites_de_cloudflare_pages(site):
    sortie, _ = site
    nombre, plus_gros = construire_site.verifier_limites(sortie)
    assert nombre <= construire_site.PLAFOND_FICHIERS
    assert plus_gros <= construire_site.PLAFOND_TAILLE


def test_une_limite_depassee_fait_echouer_la_construction(tmp_path, monkeypatch):
    (tmp_path / "gros.bin").write_bytes(b"")
    with open(tmp_path / "gros.bin", "r+b") as flux:
        flux.truncate(construire_site.PLAFOND_TAILLE + 1)
    with pytest.raises(construire_site.LimiteDepassee, match="gros.bin"):
        construire_site.verifier_limites(tmp_path)

    (tmp_path / "gros.bin").unlink()
    monkeypatch.setattr(construire_site, "PLAFOND_FICHIERS", 3)
    for i in range(4):
        (tmp_path / f"f{i}").write_text("x")
    with pytest.raises(construire_site.LimiteDepassee, match="4 fichiers"):
        construire_site.verifier_limites(tmp_path)


def test_le_manifeste_designe_des_fichiers_nommes_par_leur_empreinte(site):
    sortie, manifeste = site
    relu = json.loads((sortie / "manifeste.json").read_text(encoding="utf-8"))
    assert relu["fichiers"] == manifeste["fichiers"]
    for nom, chemin in manifeste["fichiers"].items():
        assert (sortie / chemin).is_file(), nom
        if nom != "points":
            assert re.search(r"-[0-9a-f]{16}\.(json|bin)$", chemin), chemin
    # Tout ce qui est en cache pour un an est sous `donnees/`, et rien d'autre.
    en_tetes = (sortie / "_headers").read_text(encoding="utf-8")
    assert "/donnees/*\n  Cache-Control: public, max-age=31536000, immutable" in en_tetes
    assert not (sortie / "donnees" / "manifeste.json").exists()


def test_le_manifeste_dit_la_provenance_et_la_periode_lue_dans_les_donnees(site):
    _, manifeste = site
    ventes, _ = construire_transactions_territoire("grand-paris")
    debut, fin = charge.periode(ventes)
    assert manifeste["periode"] == {"debut": f"{debut:%Y-%m-%d}", "fin": f"{fin:%Y-%m-%d}"}
    assert manifeste["nb_ventes"] == len(ventes)
    assert manifeste["source"]["url"] == charge.URL_JEU_DE_DONNEES
    assert "licence-ouverte" in manifeste["source"]["licence"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", manifeste["genere_le"])
    assert manifeste["filtres"]["annees"] == charge.annees_disponibles(ventes)


def test_les_ventes_binaires_se_relisent(site):
    sortie, manifeste = site
    contenu = gzip.decompress((sortie / manifeste["fichiers"]["ventes"]).read_bytes())
    meta = json.loads((sortie / manifeste["fichiers"]["ventes_meta"]).read_text(encoding="utf-8"))
    assert meta["n"] == manifeste["nb_ventes"]
    fin = max(c["debut"] + c["n"] * int(c["type"][1]) for c in meta["colonnes"])
    assert fin == len(contenu)


def test_l_ouverture_est_calculee_par_python_aux_filtres_par_defaut(site):
    sortie, manifeste = site
    defaut = json.loads((sortie / manifeste["fichiers"]["defaut"]).read_text(encoding="utf-8"))
    assert defaut["metrique"] == manifeste["regles"]["metrique_par_defaut"]
    assert defaut["nb"] == manifeste["nb_ventes"]
    sections = defaut["couches"]["code_section"]
    assert len(sections["codes"]) > 8000
    assert sections["legende"]["titre"] == "Prix médian au m²"


def test_la_page_et_la_carte_sont_copiees(site):
    sortie, _ = site
    for chemin in ("calcul.js", "calcul-worker.js", "carte/carte.js", "carte/vendor/maplibre-gl.js"):
        assert (sortie / chemin).is_file(), chemin


def test_la_verification_apres_deploiement_accepte_le_site(site, capsys):
    """Le script que lance le workflow apres `wrangler pages deploy`."""

    import verifier_deploiement
    from conftest import servir

    sortie, _ = site
    # Servi avec les en-tetes de `_headers` : la verification exige ceux de
    # securite, et l'absence de noindex sur les donnees.
    with servir(sortie) as adresse:
        assert verifier_deploiement.main(adresse) == 0
    rapport = capsys.readouterr().out
    assert "manifeste.json" in rapport and "| 200 |" in rapport
    assert "`mentions-legales.html` | 200 |" in rapport


def test_la_verification_attend_le_certificat_d_un_premier_deploiement(monkeypatch):
    """Premier deploiement d'un projet Pages : la poignee de main TLS echoue
    quelques minutes, le temps que Cloudflare emette le certificat."""

    import io
    import urllib.error

    import verifier_deploiement

    essais = []

    class Reponse(io.BytesIO):
        status = 200
        headers = {"Content-Type": "text/html"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(requete, timeout):
        essais.append(requete.full_url)
        if len(essais) < 3:
            raise urllib.error.URLError("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure")
        return Reponse(b"ok")

    monkeypatch.setattr(verifier_deploiement.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(verifier_deploiement.time, "sleep", lambda secondes: None)
    statut, _, corps = verifier_deploiement.lire("https://exemple.pages.dev/")
    assert (statut, corps, len(essais)) == (200, b"ok", 3)

    # Au-dela du delai, la verification echoue en le disant.
    essais.clear()
    monkeypatch.setattr(verifier_deploiement.urllib.request, "urlopen",
                        lambda requete, timeout: (_ for _ in ()).throw(urllib.error.URLError("refus")))
    with pytest.raises(SystemExit, match="ne repond pas"):
        verifier_deploiement.lire("https://exemple.pages.dev/", delai=0)


def test_la_verification_lit_un_fichier_compresse_par_cloudflare(monkeypatch):
    """Cloudflare compresse la page et le JSON : le corps arrive en gzip."""

    import gzip
    import io

    import verifier_deploiement

    class Reponse(io.BytesIO):
        status = 200
        headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(verifier_deploiement.urllib.request, "urlopen",
                        lambda requete, timeout: Reponse(gzip.compress(b"window.CarteDvfHote")))
    _, entetes, corps = verifier_deploiement.lire("https://exemple.pages.dev/")
    assert corps == b"window.CarteDvfHote"
    assert entetes["content-encoding"] == "gzip"


# --------------------------------------------------------------------------
# Securite, referencement, « À propos »
# --------------------------------------------------------------------------


def test_la_page_n_execute_que_des_scripts_du_site(site):
    """Aucun script en ligne ni venu d'ailleurs, aucun gestionnaire `on…=`,
    aucun attribut `style` : c'est ce qui permet une politique de securite
    stricte (`script-src 'self'`, `style-src 'self'`)."""

    sortie, _ = site
    for nom in construire_site.PAGES:
        page = (sortie / nom).read_text(encoding="utf-8")
        for balise in re.findall(r"<script\b[^>]*>", page):
            assert 'src="' in balise or 'type="application/ld+json"' in balise, (nom, balise)
            assert "://" not in balise, (nom, balise)
        assert not re.search(r"<style\b", page), nom
        assert not re.search(r"\son[a-z]+\s*=", page), nom
        assert not re.search(r"\sstyle\s*=", page), nom
        assert "{{" not in page, nom


def test_les_en_tetes_protegent_la_page_et_cachent_les_donnees_aux_moteurs(site):
    sortie, _ = site
    en_tetes = (sortie / "_headers").read_text(encoding="utf-8")
    csp = next(l for l in en_tetes.splitlines() if "Content-Security-Policy" in l)
    assert "script-src 'self';" in csp and "unsafe" not in csp
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    # Seuls les serveurs des fonds de carte sont joignables.
    for fond in charge.FONDS_DE_CARTE.values():
        assert "/".join(fond["url"].split("/")[:3]) in csp
    for attendu in ("X-Frame-Options: DENY", "X-Content-Type-Options: nosniff",
                    "Strict-Transport-Security", "Permissions-Policy"):
        assert attendu in en_tetes
    # OpenStreetMap exige un Referer : pas de `no-referrer`.
    assert "Referrer-Policy: strict-origin-when-cross-origin" in en_tetes
    # Conditions de reutilisation des DVF : les donnees ne s'indexent pas.
    bloc_donnees = en_tetes.split("\n\n")[0]
    assert bloc_donnees.startswith("/donnees/*") and "X-Robots-Tag: noindex" in bloc_donnees
    robots = (sortie / "robots.txt").read_text(encoding="utf-8")
    assert "Disallow: /donnees/" in robots


def test_a_propos_cite_les_regles_appliquees_par_le_code(site):
    """Le texte est rempli a la construction depuis `OptionsNettoyage` : il ne
    peut pas annoncer un filtre que le code n'applique pas."""

    sortie, manifeste = site
    page = (sortie / "index.html").read_text(encoding="utf-8")
    apropos = page[page.index('id="a-propos"'):page.index("</details>")]
    options = construire_site.OPTIONS_NETTOYAGE
    for texte in (
        "data.gouv.fr", "à but non lucratif", "Licence Ouverte 2.0",
        f"{options.seuil_vente_en_bloc} logements ou plus",
        construire_site._milliers(options.prix_m2_min),
        construire_site._milliers(options.prix_m2_max),
        construire_site._milliers(manifeste["nb_ventes"]),
        'href="mentions-legales.html"',
    ):
        assert texte in apropos, texte
    assert construire_site.URL_DEPOT in page


def test_les_mentions_legales_sont_publiees_et_liees_depuis_la_carte(site):
    """LCEN : l'editeur (ou, pour un particulier, l'hebergeur et un contact) ;
    RGPD : le traitement des ventes, les droits et la facon de les exercer."""

    sortie, _ = site
    page = (sortie / "index.html").read_text(encoding="utf-8")
    pied = page[page.index('<footer id="pied">'):page.index("</footer>")]
    assert 'href="mentions-legales.html"' in pied

    mentions = (sortie / "mentions-legales.html").read_text(encoding="utf-8")
    assert (sortie / "textes.css").exists()
    assert f'href="mailto:{construire_site.CONTACT}"' in mentions
    for texte in (
        "Cloudflare, Inc.", "101 Townsend Street",          # hebergeur
        "OVH SAS",                                          # nom de domaine
        "article 6, III, 2 de la loi n° 2004-575",          # editeur non professionnel
        "Responsable du traitement", "Base légale", "Durée",
        "vous opposer", "CNIL", "R. 112 A-3",
        "Esri", "OpenStreetMap", "IGN",                     # qui recoit l'IP du visiteur
        "licence MIT", construire_site.URL_DEPOT,
    ):
        assert texte in mentions, texte
    # Le visiteur peut agrandir le texte : pas de zoom bloque sur cette page.
    assert "user-scalable=no" not in mentions and "maximum-scale" not in mentions


def test_l_adresse_publique_n_entre_dans_la_page_que_si_elle_est_sure(tmp_path):
    assert construire_site.adresse_publique("https://prix.exemple.fr") == "https://prix.exemple.fr/"
    assert construire_site.adresse_publique(None) is None
    for douteuse in ("http://exemple.fr/", 'https://exemple.fr/"><script>', "javascript:alert(1)"):
        with pytest.raises(SystemExit):
            construire_site.adresse_publique(douteuse)

    construire_site.ecrire_referencement(tmp_path, "https://prix.exemple.fr/", "2026-09-25")
    assert "Sitemap: https://prix.exemple.fr/sitemap.xml" in (tmp_path / "robots.txt").read_text()
    assert "<loc>https://prix.exemple.fr/</loc>" in (tmp_path / "sitemap.xml").read_text()


def test_sans_adresse_publique_ni_canonique_ni_plan(site):
    sortie, _ = site
    page = (sortie / "index.html").read_text(encoding="utf-8")
    assert 'rel="canonical"' not in page and "<!--adresse-->" not in page
    assert not (sortie / "sitemap.xml").exists()
    for fichier in ("favicon.svg", "icone-180.png", "icone-512.png", "partage.png", "site.webmanifest"):
        assert (sortie / fichier).is_file(), fichier


def test_les_ventes_mixtes_sont_ecartees_et_le_filtre_n_a_que_deux_types(site):
    """Une mutation maison + appartement n'a pas de type a elle : elle est
    ecartee au nettoyage, et le filtre ne propose que deux types."""

    _, manifeste = site
    assert manifeste["filtres"]["types"] == ["Appartement", "Maison"]
