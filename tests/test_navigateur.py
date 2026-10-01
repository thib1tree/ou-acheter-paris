"""Le site statique, vu dans un vrai navigateur.

Ces tests construisent le site (`scripts/construire_site.py`), le servent par
un simple serveur de fichiers — rien d'autre ne tourne en production — et
l'ouvrent dans Chromium, sur ordinateur et sur telephone. Ils interrogent la
carte MapLibre elle-meme (`window.carteDvf`) et l'etat de la page
(`window.siteDvf`) : ce qu'elle affiche, ce qu'elle laisse faire, ce qu'elle
telecharge.

Tout appel hors de la machine est coupe : les fonds de carte ne chargent pas,
et c'est voulu — la carte doit s'afficher sans eux.

Ils sont ignores si Playwright ou Chromium manquent, sauf quand la variable
`NAVIGATEUR_OBLIGATOIRE=1` est posee (c'est le cas en integration continue) :
un navigateur absent y est alors une erreur. `CHROMIUM_EXECUTABLE` designe un
Chromium deja installe, a la place de celui que Playwright telecharge.
"""

from __future__ import annotations

import functools
import http.server
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
OBLIGATOIRE = os.environ.get("NAVIGATEUR_OBLIGATOIRE") == "1"

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depend de l'environnement
    if OBLIGATOIRE:
        raise
    pytest.skip("Playwright absent", allow_module_level=True)

#: Caracteres emoji (pictogrammes, transports, symboles divers).
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")

#: Delai d'attente des etapes lentes : chargement des ventes dans le Worker,
#: rendu logiciel de WebGL.
ATTENTE_MS = 120_000


@pytest.fixture(scope="module")
def annees(site_construit) -> list[int]:
    """Les millesimes du site : ils glissent a chaque mise a jour des donnees."""

    return site_construit[1]["filtres"]["annees"]


def _bouton(annee: int) -> str:
    """Libelle du bouton d'une annee : ses deux derniers chiffres."""

    return f"{annee % 100:02d}"


class _Silencieux(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - pas de bruit dans la sortie des tests
        pass


@pytest.fixture(scope="module")
def adresse(site_construit):
    """Un serveur de fichiers statiques, comme Cloudflare Pages : rien d'autre."""

    dossier, _ = site_construit
    serveur = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Silencieux, directory=str(dossier))
    )
    fil = threading.Thread(target=serveur.serve_forever, daemon=True)
    fil.start()
    yield f"http://127.0.0.1:{serveur.server_address[1]}"
    serveur.shutdown()


@pytest.fixture(scope="module")
def navigateur():
    options = {
        "args": ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
    }
    if os.environ.get("CHROMIUM_EXECUTABLE"):
        options["executable_path"] = os.environ["CHROMIUM_EXECUTABLE"]
    with sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch(**options)
        except Exception as erreur:  # noqa: BLE001 - navigateur absent ou incomplet
            if OBLIGATOIRE:
                raise
            pytest.skip(f"Chromium indisponible : {erreur}")
        yield chromium
        chromium.close()


class Page:
    """Une page ouverte sur le site, et la carte qu'elle contient."""

    def __init__(self, page, erreurs: list[str], requetes: list[str], terminees: list[str]):
        self.page = page
        self.erreurs = erreurs
        #: Adresses demandees par la page, la carte et le Worker, dans l'ordre.
        self.requetes = requetes
        #: Adresses dont la reponse est arrivee (ou a echoue).
        self.terminees = terminees

    def carte(self, expression: str, argument=None):
        """Evalue `expression` (une fonction de `carte`) dans la page."""

        return self.page.evaluate(
            f"([argument]) => {{ const carte = window.carteDvf; return ({expression})(carte, argument); }}",
            [argument],
        )

    def attendre(self, expression: str, argument=None):
        self.page.wait_for_function(
            f"([argument]) => {{ const carte = window.carteDvf; return !!carte && ({expression})(carte, argument); }}",
            arg=[argument],
            timeout=ATTENTE_MS,
        )

    def aller(self, longitude: float, latitude: float, zoom: float) -> None:
        """Deplace la carte comme le ferait le visiteur.

        `originalEvent` fait passer le deplacement pour un geste : sans lui, la
        carte se recadrerait sur sa vue d'ouverture au rendu suivant, comme
        elle le fait tant que personne n'y a touche.
        """

        self.carte(
            "(carte, v) => carte.jumpTo({center: [v[0], v[1]], zoom: v[2]}, {originalEvent: true})",
            [longitude, latitude, zoom],
        )
        self.attendre("(carte) => !carte.isMoving() && carte.areTilesLoaded()")

    def attendre_ventes(self) -> int:
        """Attend que toutes les tuiles de ventes demandees soient posees sur
        la carte, et rend le nombre de ventes de la source.

        La carte montre des ventes des la premiere tuile arrivee : compter
        avant la derniere, c'est compter un quartier a moitie charge — et
        attendre ensuite de retrouver ce compte, c'est attendre pour rien.
        """

        def en_vol() -> int:
            return sum("/donnees/dvf-pts-" in url for url in self.requetes) - sum(
                "/donnees/dvf-pts-" in url for url in self.terminees
            )

        compter = "(carte) => carte.isSourceLoaded('points') ? carte.querySourceFeatures('points').length : -1"
        limite = time.monotonic() + ATTENTE_MS / 1000
        precedent, compte = None, -1
        # Une tuile arrivee n'est posee qu'au rendu suivant, puis decoupee par
        # MapLibre : le compte doit aussi rester le meme d'une lecture a l'autre.
        while en_vol() or compte < 0 or compte != precedent:
            if time.monotonic() > limite:
                raise AssertionError(f"ventes jamais posees : {en_vol()} tuile(s) en vol, compte {compte}")
            self.page.wait_for_timeout(500)
            precedent, compte = compte, self.carte(compter)
        return compte

    def suivi(self) -> dict:
        return self.page.evaluate("() => Object.assign({}, window.siteDvf)")

    def attendre_calcul(self, apres: int) -> dict:
        """Attend qu'un calcul complet posterieur au n-ieme soit pose sur la carte."""

        self.page.wait_for_function(
            "(n) => window.siteDvf.calculs > n && window.siteDvf.complet",
            arg=apres, timeout=ATTENTE_MS,
        )
        return self.suivi()

    def point_ecran(self, longitude: float, latitude: float) -> tuple[float, float]:
        """Position d'un lieu dans la page."""

        x, y = self.carte("(carte, v) => { const p = carte.project(v); return [p.x, p.y]; }",
                          [longitude, latitude])
        boite = self.page.locator("#carte").bounding_box()
        return boite["x"] + x, boite["y"] + y


def _ouvrir(navigateur, adresse, **contexte) -> Page:
    session = navigateur.new_context(**contexte)
    # Rien ne sort de la machine : la carte doit vivre sans ses fonds.
    session.route(
        re.compile(r"^https?://(?!127\.0\.0\.1|localhost)"), lambda route: route.abort()
    )
    page = session.new_page()
    erreurs: list[str] = []
    requetes: list[str] = []
    terminees: list[str] = []
    page.on("pageerror", lambda erreur: erreurs.append(str(erreur)))
    session.on("request", lambda requete: requetes.append(requete.url))
    session.on("requestfinished", lambda requete: terminees.append(requete.url))
    session.on("requestfailed", lambda requete: terminees.append(requete.url))
    page.goto(adresse)
    ouverte = Page(page, erreurs, requetes, terminees)
    ouverte.attendre(
        "(carte) => carte.isStyleLoaded() && carte.getSource('communes')"
        " && carte.isSourceLoaded('communes') && carte.isSourceLoaded('sections')"
        " && carte.queryRenderedFeatures({layers: ['zones-communes']}).length > 0"
    )
    return ouverte


@pytest.fixture(scope="module")
def bureau(navigateur, adresse):
    page = _ouvrir(navigateur, adresse, viewport={"width": 1400, "height": 900})
    yield page
    page.page.context.close()


@pytest.fixture(scope="module")
def telephone(navigateur, adresse):
    page = _ouvrir(
        navigateur, adresse,
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
        device_scale_factor=2,
    )
    yield page
    page.page.context.close()


# --------------------------------------------------------------------------
# Ouverture
# --------------------------------------------------------------------------


def test_la_page_s_ouvre_sur_les_communes_sans_erreur(bureau):
    import construire_site

    assert bureau.page.title() == construire_site.NOM_SITE
    # Le titre reste lisible sur telephone aussi (voir le test du telephone).
    assert bureau.page.locator("#entete h1").inner_text() == construire_site.NOM_SITE
    assert not bureau.erreurs, bureau.erreurs
    assert not EMOJI.search(bureau.page.locator("body").inner_text())

    # Les communes sont colorees, et le bouton dit ce qu'on regarde.
    couleurs = bureau.carte(
        "(carte) => carte.queryRenderedFeatures({layers: ['zones-communes']})"
        ".slice(0, 50).map((e) => carte.getFeatureState({source: 'communes', id: e.id}).couleur)"
    )
    assert couleurs and all(couleurs), "chaque commune porte sa couleur en etat d'entite"
    actif = bureau.page.locator("#mode button[aria-pressed='true']")
    assert actif.first.inner_text() == "Auto"


def test_l_ouverture_ne_telecharge_pas_les_ventes(bureau):
    """Les statistiques d'ouverture sont precalculees : les 3 Mo de ventes ne
    sont pas necessaires pour voir la carte coloree."""

    suivi = bureau.suivi()
    assert suivi["calculs"] >= 1 and suivi["metrique"] == "prix_m2_median"
    premier = next(i for i, url in enumerate(bureau.requetes) if "/donnees/defaut-" in url)
    ventes = [i for i, url in enumerate(bureau.requetes) if re.search(r"/donnees/ventes-\w+\.bin", url)]
    assert not ventes or ventes[0] > premier


def test_le_pied_de_page_cite_la_source_et_la_periode_lue_dans_les_donnees(bureau, site_construit):
    _, manifeste = site_construit
    pied = bureau.page.locator("#pied")
    texte = pied.inner_text()
    debut = "/".join(reversed(manifeste["periode"]["debut"].split("-")))
    fin = "/".join(reversed(manifeste["periode"]["fin"].split("-")))
    assert f"ventes du {debut} au {fin}" in texte
    assert "DGFiP / Etalab" in texte and "Licence Ouverte 2.0" in texte
    assert pied.locator("a").first.get_attribute("href") == manifeste["source"]["url"]


def test_la_carte_s_affiche_sans_fond_de_carte(bureau):
    """Aucune tuile de fond n'arrive ici : zones, gares et boutons sont la quand meme."""

    for couche in ("zones-communes", "zones-sections", "transactions", "gares", "gares-projet", "trajet"):
        assert bureau.carte("(carte, id) => !!carte.getLayer(id)", couche), couche
    assert bureau.page.locator("#choix-metrique option").count() >= 5


# --------------------------------------------------------------------------
# Gestes
# --------------------------------------------------------------------------


def test_la_carte_reste_plane_et_orientee_au_nord(bureau):
    reglages = bureau.carte(
        "(carte) => ({rotation: carte.dragRotate.isEnabled(), pitch: carte.getMaxPitch(),"
        " clavier: carte.keyboard._rotationDisabled, doigts: carte.touchZoomRotate._rotationDisabled})"
    )
    assert reglages == {"rotation": False, "pitch": 0, "clavier": True, "doigts": True}

    # Un glisser au bouton droit (qui fait pivoter par defaut) ne tourne rien.
    x, y = bureau.point_ecran(2.35, 48.86)
    bureau.page.mouse.move(x, y)
    bureau.page.mouse.down(button="right")
    bureau.page.mouse.move(x + 150, y + 80, steps=8)
    bureau.page.mouse.up(button="right")
    assert bureau.carte("(carte) => [carte.getBearing(), carte.getPitch()]") == [0, 0]


def test_le_zoom_choisit_la_maille(bureau):
    bureau.aller(2.35, 48.86, 10)
    assert bureau.carte("(carte) => carte.queryRenderedFeatures({layers: ['zones-communes']}).length") > 0
    assert bureau.carte("(carte) => carte.queryRenderedFeatures({layers: ['zones-sections']}).length") == 0

    bureau.aller(2.35, 48.86, 13.5)
    assert bureau.carte("(carte) => carte.queryRenderedFeatures({layers: ['zones-communes']}).length") == 0
    assert bureau.carte("(carte) => carte.queryRenderedFeatures({layers: ['zones-sections']}).length") > 0


def test_le_mode_sections_montre_la_maille_cadastrale_des_la_vue_d_ensemble(bureau):
    bureau.aller(2.35, 48.86, 10)
    bureau.page.locator("#mode button[data-mode='sections']").click()
    bureau.attendre("(carte) => carte.queryRenderedFeatures({layers: ['zones-sections']}).length > 0")
    bureau.page.locator("#mode button[data-mode='communes']").click()
    bureau.attendre("(carte) => carte.queryRenderedFeatures({layers: ['zones-sections']}).length == 0")


def test_a_la_souris_le_survol_informe_et_le_clic_zoome_sur_la_zone(bureau):
    """Sur grand ecran, le survol d'une commune donne ses chiffres ; le clic y
    descend, comme « Aller à » : la commune entiere a l'ecran. Rien n'est
    recalcule."""

    bureau.aller(2.35, 48.86, 10)
    calculs = bureau.suivi()["calculs"]
    point = bureau.point_ecran(2.296, 48.840)  # au coeur du 15e arrondissement
    bureau.page.mouse.move(*point)
    infobulle = bureau.page.locator("#infobulle")
    infobulle.wait_for(state="visible")
    assert "Prix médian au m²" in infobulle.inner_text()
    code = bureau.carte(
        "(carte, p) => { const b = carte.getContainer().getBoundingClientRect();"
        " return carte.queryRenderedFeatures([p[0] - b.left, p[1] - b.top],"
        " {layers: ['zones-communes']})[0].properties.c; }", list(point),
    )

    bureau.page.mouse.click(*point)
    bureau.attendre("(carte) => carte.getZoom() > 11 && !carte.isMoving()")
    # Le clic n'epingle rien : sous le curseur, c'est le survol qui reprend.
    assert "fixee" not in (infobulle.get_attribute("class") or "")
    assert bureau.suivi()["calculs"] == calculs
    # Le centre de la vue est dans la commune cliquee, et la commune tient a
    # l'ecran : ses morceaux ne debordent pas du cadre.
    tient = bureau.carte(
        "(carte, code) => { const vue = carte.getBounds(); const c = carte.getCenter();"
        " const morceaux = carte.querySourceFeatures('communes', {filter: ['==', ['get', 'c'], code]});"
        " const emprise = new maplibregl.LngLatBounds(); let tout = true;"
        " morceaux.forEach((m) => { const polys = m.geometry.type === 'Polygon' ? [m.geometry.coordinates] : m.geometry.coordinates;"
        "  polys.forEach((p) => p[0].forEach((q) => { emprise.extend(q); if (!vue.contains(q)) tout = false; })); });"
        " return [morceaux.length > 0, emprise.contains(c), tout]; }", code,
    )
    assert tient == [True, True, True]


def test_l_ouverture_ne_demande_que_les_tuiles_du_fond_par_defaut(bureau, site_construit):
    """Aucune tuile n'est demandee a un autre serveur que celui du fond par
    defaut : la carte s'ouvre sans fond, et le manifeste le designe."""

    manifeste = site_construit[1]
    hote = manifeste["fonds"][manifeste["fond_initial"]]["url"].split("/")[2]
    externes = [url.split("/")[2] for url in bureau.requetes if not url.startswith("http://127.0.0.1")]
    assert externes and set(externes) == {hote}, set(externes)


def test_le_fond_se_change_sans_rien_recalculer(bureau):
    calculs = bureau.suivi()["calculs"]
    bureau.page.locator("#choix-fond").select_option("OpenStreetMap")
    tuiles = bureau.carte("(carte) => carte.getStyle().sources.fond.tiles[0]")
    assert "openstreetmap" in tuiles
    assert bureau.suivi()["calculs"] == calculs
    bureau.page.locator("#choix-fond").select_option("Clair (Esri)")


def test_la_legende_montre_l_echelle_et_les_transports_et_se_replie(bureau):
    legende = bureau.page.locator("#legende")
    bascule = bureau.page.locator("#legende-bascule")
    assert legende.is_visible() and bascule.is_visible()
    # L'echelle, puis une ligne pour les transports, dessines comme sur la carte.
    assert [e.get_attribute("class") for e in legende.locator(":scope > div").all()] == [
        "titre", "degrade", "graduations", "transports",
    ]
    transports = legende.locator(".transports")
    assert transports.locator("svg").count() == 4
    assert "À venir" in transports.inner_text()
    # Le gris s'explique au survol.
    assert "Gris" in legende.get_attribute("title")

    bascule.click()
    assert legende.is_hidden()
    assert bascule.get_attribute("aria-expanded") == "false"
    bascule.click()
    assert legende.is_visible()


def test_un_indice_invite_a_zoomer_et_zoome_quand_on_le_clique(navigateur, adresse):
    """Discret, cliquable, et il s'efface une fois les ventes atteintes.

    Sur une page a lui : l'indice se souvient, pour la session, que les ventes
    ont ete vues, et un autre test a pu y descendre avant."""

    bureau = _ouvrir(navigateur, adresse, viewport={"width": 1200, "height": 800})
    indice = bureau.page.locator("#indice-zoom")
    bureau.aller(2.35, 48.86, 10)
    assert indice.is_visible() and "gares" in indice.inner_text()
    indice.click()
    bureau.attendre("(carte) => !carte.isMoving() && carte.getZoom() > 11.5")
    bureau.aller(2.35, 48.86, 13)
    assert "rue par rue" in indice.inner_text()
    bureau.aller(2.35, 48.86, 16)
    bureau.aller(2.35, 48.86, 10)
    assert indice.is_hidden(), "les ventes ont ete vues : l'indice ne revient pas"
    bureau.page.context.close()


def test_aller_a_une_commune_sans_service_exterieur(bureau):
    """La recherche lit les noms de la carte, tolere les accents et « st »."""

    bureau.aller(2.35, 48.86, 10)
    champ = bureau.page.locator("#aller-a")
    assert bureau.page.locator("#liste-communes option").count() > 400
    champ.fill("st maur des fosses")
    champ.press("Enter")
    bureau.attendre("(carte) => !carte.isMoving() && carte.getZoom() >= 12.9")
    centre = bureau.carte("(carte) => [carte.getCenter().lng, carte.getCenter().lat]")
    assert centre == pytest.approx([2.49, 48.80], abs=0.03)
    assert champ.input_value() == ""
    # Un nom inconnu ne deplace rien, et le champ le signale.
    champ.fill("Xyzzy")
    champ.press("Enter")
    assert "introuvable" in champ.get_attribute("class")
    champ.fill("")


def test_un_bouton_ramene_a_la_vue_d_ensemble(navigateur, adresse):
    """Sous « + » et « − », dans le meme bloc : la vue d'ouverture revient, et
    l'adresse perd la vue qu'elle portait."""

    page = _ouvrir(navigateur, adresse, viewport={"width": 1100, "height": 750})
    ouverture = page.carte("(carte) => [carte.getCenter().lng, carte.getCenter().lat, carte.getZoom()]")
    bouton = page.page.locator(".maplibregl-ctrl-group button.vue-initiale")
    assert bouton.count() == 1
    assert page.page.locator(".maplibregl-ctrl-group:has(.maplibregl-ctrl-zoom-in) .vue-initiale").count() == 1
    page.aller(2.45, 48.80, 14)
    assert page.page.evaluate("() => location.hash")
    bouton.click()
    page.attendre("(carte) => !carte.isMoving() && Math.abs(carte.getZoom() - %f) < 0.05" % ouverture[2])
    retour = page.carte("(carte) => [carte.getCenter().lng, carte.getCenter().lat, carte.getZoom()]")
    assert retour == pytest.approx(ouverture, abs=0.01)
    assert page.page.evaluate("() => location.hash") == ""
    page.page.context.close()


def test_l_adresse_de_la_page_partage_la_vue(navigateur, adresse):
    """La vue courante s'ecrit dans l'adresse ; rouverte, elle revient telle
    quelle, sans que la carte ne la recadre."""

    page = _ouvrir(navigateur, adresse + "/#10.50/48.85000/2.30000",
                   viewport={"width": 1000, "height": 700})
    vue = page.carte("(carte) => [carte.getZoom(), carte.getCenter().lat, carte.getCenter().lng]")
    assert vue == pytest.approx([10.5, 48.85, 2.30], abs=1e-3)
    page.aller(2.40, 48.87, 12)
    assert page.page.evaluate("() => location.hash") == "#12.00/48.87000/2.40000"
    page.page.context.close()


# --------------------------------------------------------------------------
# Gares et ventes
# --------------------------------------------------------------------------


def test_les_gares_a_venir_ont_leur_propre_couche(bureau):
    bureau.aller(2.35, 48.86, 13.5)
    statuts = bureau.carte(
        "(carte) => ({service: carte.queryRenderedFeatures({layers: ['gares']}).map((e) => e.properties.statut),"
        " projet: carte.queryRenderedFeatures({layers: ['gares-projet']}).map((e) => e.properties.statut)})"
    )
    assert statuts["service"] and set(statuts["service"]) == {"service"}
    assert "service" not in set(statuts["projet"])


def test_l_infobulle_d_une_gare_reprend_son_pictogramme(bureau):
    bureau.aller(2.3375, 48.8606, 15)  # Palais-Royal
    gare = bureau.carte(
        "(carte) => { const e = carte.queryRenderedFeatures({layers: ['gares']})[0];"
        " return e && [e.geometry.coordinates[0], e.geometry.coordinates[1], e.properties.nom]; }"
    )
    assert gare, "une gare est visible"
    bureau.page.mouse.click(*bureau.point_ecran(gare[0], gare[1]))

    infobulle = bureau.page.locator("#infobulle")
    infobulle.wait_for(state="visible")
    assert gare[2] in infobulle.inner_text()
    assert infobulle.locator(".entete svg.ico").count() == 1
    assert not EMOJI.search(infobulle.inner_text())
    infobulle.locator(".fermer").click()


def test_de_loin_une_gare_lourde_se_designe_et_une_station_de_metro_non(bureau):
    """Un demi-cran sous le plancher des stations de metro, les gares RER
    repondent deja au clic, meme vise a quelques pixels pres ; les stations
    de metro, une tous les trois cents metres, laissent la commune repondre."""

    bureau.aller(2.40, 48.86, 12.25)
    gares = bureau.carte(
        "(carte) => carte.queryRenderedFeatures({layers: ['gares']}).map((e) => {"
        " const p = carte.project(e.geometry.coordinates);"
        " return [p.x, p.y, e.properties.rang, e.properties.nom]; })"
    )
    lourdes = [g for g in gares if g[2] == 2]
    assert lourdes, "des gares RER sont visibles"
    boite = bureau.page.locator("#carte").bounding_box()

    def infobulle_apres_clic(x, y):
        bureau.page.mouse.click(boite["x"] + x, boite["y"] + y)
        infobulle = bureau.page.locator("#infobulle")
        infobulle.wait_for(state="visible")
        texte = infobulle.inner_text()
        infobulle.locator(".fermer").click()
        return texte

    x, y, _, nom = lourdes[0]
    assert nom in infobulle_apres_clic(x + 5, y)

    isolees = [
        g for g in gares
        if g[2] == 1 and all((g[0] - l[0]) ** 2 + (g[1] - l[1]) ** 2 > 40 ** 2 for l in lourdes)
    ]
    if isolees:
        # C'est la zone qui repond : a la souris, le clic y zoome, et aucune
        # infobulle de gare ne s'epingle.
        x, y, _, _ = isolees[0]
        bureau.page.mouse.click(boite["x"] + x, boite["y"] + y)
        bureau.attendre("(carte) => carte.getZoom() > 12.3 && !carte.isMoving()")
        infobulle = bureau.page.locator("#infobulle")
        assert "fixee" not in (infobulle.get_attribute("class") or "")
        assert infobulle.is_hidden() or infobulle.locator(".entete svg.ico").count() == 0


def test_un_pole_desservi_annonce_a_part_sa_ligne_a_venir(bureau):
    """Les Ardoines : le RER C y roule, la ligne 15 y est attendue. Les deux ne
    se confondent pas — le jour ou la 15 ouvre, seule la seconde ligne change."""

    bureau.aller(2.4096, 48.7822, 15)
    gare = bureau.carte(
        "(carte) => { const e = carte.queryRenderedFeatures({layers: ['gares']})"
        ".find((e) => e.properties.a_venir); return e && [e.geometry.coordinates[0],"
        " e.geometry.coordinates[1], e.properties.lignes, e.properties.a_venir]; }"
    )
    assert gare, "un pole en service annonce une ligne a venir"
    bureau.page.mouse.click(*bureau.point_ecran(gare[0], gare[1]))
    infobulle = bureau.page.locator("#infobulle")
    infobulle.wait_for(state="visible")
    assert f"À venir : {gare[3]}" in infobulle.inner_text()
    assert gare[3] not in gare[2]
    infobulle.locator(".fermer").click()


def test_le_depot_du_code_est_a_un_clic(bureau):
    lien = bureau.page.locator("#pied #depot")
    assert lien.get_attribute("href") == "https://github.com/thib1tree/ou-acheter-paris"
    assert lien.get_attribute("rel") == "noopener"
    assert lien.bounding_box()["width"] >= 16


def test_la_page_tient_dans_sa_politique_de_securite(navigateur, site_construit):
    """Servie avec ses vrais en-tetes (`_headers`), CSP comprise : aucune
    violation, et la carte s'ouvre, se recolore et montre ses infobulles.

    Les autres tests se passent de ces en-tetes : leurs attentes s'evaluent
    dans la page, ce qu'une politique stricte interdit a juste titre."""

    from conftest import servir

    with servir(site_construit[0]) as adresse:
        session = navigateur.new_context(viewport={"width": 1200, "height": 800})
        session.route(re.compile(r"^https?://(?!127\.0\.0\.1|localhost)"), lambda route: route.abort())
        page = session.new_page()
        violations: list[str] = []
        page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
        page.on("pageerror", lambda erreur: violations.append(str(erreur)))
        page.add_init_script(
            "document.addEventListener('securitypolicyviolation', (e) => "
            "console.error('Content Security Policy : ' + e.violatedDirective + ' ' + e.blockedURI))"
        )
        page.goto(adresse)

        def attendre(expression):
            for _ in range(ATTENTE_MS // 250):
                if page.evaluate(expression):
                    return
                page.wait_for_timeout(250)
            raise AssertionError(expression)

        attendre("() => !!(window.carteDvf && window.carteDvf.isStyleLoaded()"
                 " && window.carteDvf.queryRenderedFeatures({layers: ['zones-communes']}).length)")
        page.locator("#choix-metrique").select_option("prix_total_median")
        attendre("() => window.siteDvf.metrique === 'prix_total_median' && window.siteDvf.complet")
        page.locator("#legende .titre", has_text="Prix total médian").wait_for(timeout=ATTENTE_MS)
        page.mouse.move(700, 400)
        page.locator("#infobulle").wait_for(state="visible")
        session.close()
    assert not violations, violations


def test_les_ventes_arrivent_en_tuiles_et_se_filtrent_dans_le_navigateur(bureau, annees):
    def tuiles():
        return [url for url in bureau.requetes if "/donnees/dvf-pts-" in url]

    bureau.aller(2.3488, 48.8534, 16.5)  # Ile de la Cite
    bureau.attendre("(carte) => carte.queryRenderedFeatures({layers: ['transactions']}).length > 0")
    assert tuiles(), "les ventes viennent de tuiles statiques"
    avant = bureau.attendre_ventes()

    # Une annee de moins : moins de ventes, et pas une tuile de plus.
    demandees = len(tuiles())
    annee = bureau.page.locator("#filtre-annees button", has_text=_bouton(annees[0]))
    annee.click()
    bureau.attendre("(carte, avant) => carte.querySourceFeatures('points').length < avant", avant)
    assert len(tuiles()) == demandees
    annee.click()
    bureau.attendre("(carte, avant) => carte.querySourceFeatures('points').length == avant", avant)


# --------------------------------------------------------------------------
# Distance a pied de la gare
# --------------------------------------------------------------------------

#: Memes regles que `carte.js` : un degre de latitude, et une minute de marche
#: a vol d'oiseau (4,8 km/h, detours compris).
METRES_PAR_DEGRE = 111195
METRES_PAR_MINUTE = 64


def _metres(a, b) -> float:
    import math

    y = (a[1] - b[1]) * METRES_PAR_DEGRE
    x = (a[0] - b[0]) * METRES_PAR_DEGRE * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot(x, y)


@pytest.fixture(scope="module")
def gares_du_site(site_construit):
    import json

    dossier, manifeste = site_construit
    return json.loads((dossier / manifeste["fichiers"]["gares"]).read_text(encoding="utf-8"))["features"]


def test_l_echelle_reste_metrique(bureau):
    assert bureau.page.locator(".maplibregl-ctrl-scale").count() == 1
    bureau.aller(2.35, 48.86, 15)
    assert re.search(r"\d+\s(m|km)$", bureau.page.locator(".maplibregl-ctrl-scale").inner_text())


def test_au_survol_un_trait_mene_a_la_gare_la_plus_proche(bureau, gares_du_site):
    """Un seul trait, du pointeur (ou de la vente qu'il designe) a la gare en
    service la plus proche, avec le temps de marche ; l'infobulle le reprend.
    Rien de loin, rien quand le pointeur quitte la carte."""

    bureau.aller(2.4005, 48.7890, 15.4)  # Vitry-sur-Seine, entre le tram 9 et le RER C
    bureau.page.mouse.move(*bureau.point_ecran(2.4005, 48.7890))
    bureau.page.wait_for_function("() => !!window.CarteDvf.trajet()", timeout=ATTENTE_MS)
    trajet = bureau.page.evaluate("() => window.CarteDvf.trajet()")

    en_service = [g for g in gares_du_site if g["properties"]["statut"] == "service"]
    proche = min(en_service, key=lambda g: _metres(trajet["origine"], g["geometry"]["coordinates"]))
    distance = _metres(trajet["origine"], proche["geometry"]["coordinates"])
    attendu = max(1, -(-int(distance * 1000) // (METRES_PAR_MINUTE * 1000)))
    premiere = trajet["gares"][0]
    assert premiere["nom"] == proche["properties"]["nom"] and not premiere["a_venir"]
    assert premiere["minutes"] == attendu

    bureau.attendre("(carte) => carte.querySourceFeatures('trajet').length > 0")
    pastille = bureau.page.locator("#trajets .pastille-trajet").first
    assert pastille.is_visible() and pastille.inner_text() == f"{attendu} min"
    infobulle = bureau.page.locator("#infobulle .gare-proche")
    assert f"{premiere['nom']}" in infobulle.inner_text()
    assert f"≈ {attendu} min à pied" in infobulle.inner_text()
    # Le plus proche est un tram : la gare RER la plus proche est citee aussi.
    if proche["properties"]["rang"] != 2:
        assert any(g["nom"] != premiere["nom"] for g in trajet["gares"][1:])

    # Du pointeur, le trait part du pointeur meme.
    ecart = bureau.carte(
        "(carte, t) => { const a = carte.project(t.origine), b = carte.project(t.segments[0][0]);"
        " return Math.hypot(a.x - b.x, a.y - b.y); }", trajet,
    )
    assert trajet["ecarts"] == [0] and ecart < 1

    # Le pointeur quitte la carte : le trait s'efface.
    bureau.page.mouse.move(100, 400)
    bureau.page.wait_for_function("() => !window.CarteDvf.trajet()", timeout=ATTENTE_MS)
    assert not bureau.page.locator("#trajets .pastille-trajet:visible").count()

    # De loin, on compare des communes : pas de trait.
    bureau.aller(2.4005, 48.7890, 12)
    bureau.page.mouse.move(*bureau.point_ecran(2.4005, 48.7890))
    bureau.page.wait_for_timeout(300)
    assert bureau.page.evaluate("() => window.CarteDvf.trajet()") is None
    bureau.page.mouse.move(100, 400)


def test_au_dela_d_une_demi_heure_de_marche_rien_n_est_trace(bureau, ventes, gares_du_site):
    """Un logement trop loin de toute gare n'a pas de trait : l'infobulle le dit."""

    import numpy as np

    en_service = np.array([g["geometry"]["coordinates"] for g in gares_du_site
                           if g["properties"]["statut"] == "service"])
    positions = ventes[["longitude", "latitude"]].dropna().to_numpy()[::50]
    positions = positions[(np.abs(positions[:, 1] - 48.8) < 0.4) & (np.abs(positions[:, 0] - 2.35) < 0.6)]
    ecart_x = (positions[:, None, 0] - en_service[None, :, 0]) * METRES_PAR_DEGRE * np.cos(np.radians(48.8))
    ecart_y = (positions[:, None, 1] - en_service[None, :, 1]) * METRES_PAR_DEGRE
    plus_proche = np.hypot(ecart_x, ecart_y).min(axis=1)
    lon, lat = positions[np.argmax(plus_proche)]
    assert plus_proche.max() > 30 * METRES_PAR_MINUTE + 300

    bureau.aller(lon, lat, 15)
    bureau.page.mouse.move(*bureau.point_ecran(lon, lat))
    infobulle = bureau.page.locator("#infobulle .gare-proche")
    infobulle.wait_for(state="visible", timeout=ATTENTE_MS)
    assert "Aucune gare à moins de 30 min à pied" in infobulle.inner_text()
    assert bureau.page.evaluate("() => window.CarteDvf.trajet()") is None
    bureau.page.mouse.move(100, 400)


def test_le_trait_n_empiete_pas_sur_le_logement_clique(bureau):
    """Clic sur une vente : le trait part de la vente, mais s'arrete avant son
    icone, et passe sous les icones des autres ventes."""

    bureau.aller(2.3488, 48.8534, 17)  # Ile de la Cite
    bureau.attendre("(carte) => carte.queryRenderedFeatures({layers: ['transactions']}).length > 0")
    vente = bureau.carte(
        "(carte) => { const c = carte.project(carte.getCenter());"
        " const v = carte.queryRenderedFeatures({layers: ['transactions']});"
        " const d = (e) => { const p = carte.project(e.geometry.coordinates); return Math.hypot(p.x - c.x, p.y - c.y); };"
        " v.sort((a, b) => d(a) - d(b)); return v[0].geometry.coordinates; }"
    )
    bureau.page.mouse.click(*bureau.point_ecran(*vente))
    bureau.page.locator("#infobulle").wait_for(state="visible")
    trajet = bureau.page.evaluate("() => window.CarteDvf.trajet()")
    assert trajet["origine"] == pytest.approx(vente, abs=1e-9), "le trait part de la vente"
    mesures = bureau.carte(
        "(carte, t) => { const o = carte.project(t.origine);"
        " const a = carte.project(t.segments[0][0]), b = carte.project(t.segments[0][1]);"
        " return [Math.hypot(a.x - o.x, a.y - o.y), Math.hypot(b.x - o.x, b.y - o.y)]; }", trajet,
    )
    # Le trait commence au bord du dessin, cerne compris : il touche l'icone
    # sans la couvrir. A ce zoom, l'icone fait une vingtaine de pixels.
    assert mesures[0] == pytest.approx(trajet["ecarts"][0], abs=0.6)
    assert 6 < trajet["ecarts"][0] < 14 and mesures[1] > mesures[0]
    ordre = bureau.carte("(carte) => carte.getStyle().layers.map((c) => c.id)")
    assert ordre.index("trajet") < ordre.index("transactions") < ordre.index("gares")
    bureau.page.locator("#infobulle .fermer").click()
    bureau.page.wait_for_function("() => !window.CarteDvf.trajet()", timeout=ATTENTE_MS)


# --------------------------------------------------------------------------
# Filtres et calcul dans le navigateur
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ventes():
    from src.ingestion import construire_transactions_territoire

    transactions, _ = construire_transactions_territoire("grand-paris")
    return transactions


def test_un_filtre_est_recalcule_dans_le_worker_avec_les_chiffres_de_python(bureau, ventes, annees):
    """Le calcul lui-meme est verifie chiffre pour chiffre par `test_parite.py` ;
    ici, on verifie qu'il tourne bien dans le navigateur, hors du fil de la
    carte, et que la page en pose le resultat."""

    bureau.aller(2.35, 48.86, 10)
    calculs = bureau.suivi()["calculs"]
    premiere = annees[0]
    bureau.page.locator("#filtre-annees button", has_text=_bouton(premiere)).click()
    suivi = bureau.attendre_calcul(calculs)
    assert suivi["nb"] == int((ventes["annee"] != premiere).sum())
    assert any(url.endswith("/calcul-worker.js") for url in bureau.requetes)
    assert any(re.search(r"/donnees/ventes-\w+\.bin$", url) for url in bureau.requetes)

    # Surface bornee, maisons seules.
    calculs = suivi["calculs"]
    bureau.page.locator("#surface-min").fill("50")
    bureau.page.locator("#surface-max").fill("100")
    bureau.page.locator("#surface-max").press("Enter")
    bureau.page.locator("#surface-max").blur()
    bureau.page.locator("#filtre-types input[value='Appartement']").uncheck()
    suivi = bureau.attendre_calcul(calculs)
    bureau.page.wait_for_function(
        "(n) => window.siteDvf.nb === n",
        arg=int(((ventes["annee"] != premiere) & (ventes["type_bien"] == "Maison")
                 & (ventes["surface_bati"] >= 50) & (ventes["surface_bati"] <= 100)).sum()),
        timeout=ATTENTE_MS,
    )
    assert "De 50 à 100 m²." in bureau.page.locator("#surface-rappel").inner_text()

    # Retour aux filtres d'ouverture : les statistiques precalculees reviennent.
    calculs = bureau.suivi()["calculs"]
    bureau.page.locator("#filtre-annees button", has_text=_bouton(premiere)).click()
    bureau.page.locator("#filtre-types input[value='Appartement']").check()
    bureau.page.locator("#surface-min").fill("0")
    plafond = bureau.page.locator("#surface-max").get_attribute("max")
    bureau.page.locator("#surface-max").fill(plafond)
    bureau.page.locator("#surface-max").blur()
    bureau.page.wait_for_function("(n) => window.siteDvf.nb === n", arg=len(ventes), timeout=ATTENTE_MS)
    assert "et plus." in bureau.page.locator("#surface-rappel").inner_text()
    assert bureau.suivi()["calculs"] > calculs


def test_aucune_vente_retenue_se_dit_dans_le_panneau(bureau):
    # Aucune vente de moins de 8 m² ne passe le nettoyage.
    calculs = bureau.suivi()["calculs"]
    bureau.page.locator("#surface-min").fill("0")
    bureau.page.locator("#surface-max").fill("5")
    bureau.page.locator("#surface-max").blur()
    bureau.attendre_calcul(calculs)
    bureau.page.locator("#alertes", has_text="Aucune vente ne correspond aux filtres").wait_for(
        timeout=ATTENTE_MS
    )
    plafond = bureau.page.locator("#surface-max").get_attribute("max")
    bureau.page.locator("#surface-max").fill(plafond)
    bureau.page.locator("#surface-max").blur()
    bureau.page.locator("#alertes .alerte").wait_for(state="detached", timeout=ATTENTE_MS)


def test_la_metrique_se_choisit_sur_la_carte_et_recolore_tout(bureau, annees):
    bureau.aller(2.35, 48.86, 10)
    bureau.page.locator("#choix-metrique").select_option("rendement")
    bureau.page.locator("#legende .titre", has_text="Évolution annuelle").wait_for(
        timeout=ATTENTE_MS
    )
    bureau.page.mouse.move(*bureau.point_ecran(2.33, 48.87))
    infobulle = bureau.page.locator("#infobulle")
    infobulle.wait_for(state="visible")
    assert "Régularité (R²)" in infobulle.inner_text()
    bureau.page.mouse.move(100, 400)

    # Deux annees seulement : l'evolution annuelle n'est pas calculable, et le
    # panneau dit quoi faire.
    decochees = [_bouton(a) for a in annees[:-2]]
    for annee in decochees:
        bureau.page.locator("#filtre-annees button", has_text=annee).click()
    bureau.page.locator("#alertes", has_text="il faut au moins 3 années").wait_for(timeout=ATTENTE_MS)
    for annee in decochees:
        bureau.page.locator("#filtre-annees button", has_text=annee).click()

    bureau.page.locator("#choix-metrique").select_option("prix_total_median")
    bureau.page.locator("#legende .titre", has_text="Prix total médian").wait_for(timeout=ATTENTE_MS)
    bureau.page.locator("#choix-metrique").select_option("prix_m2_median")
    bureau.page.locator("#legende .titre", has_text="Prix médian au m²").wait_for(timeout=ATTENTE_MS)


# --------------------------------------------------------------------------
# Telephone
# --------------------------------------------------------------------------


def test_sur_telephone_la_carte_prend_l_ecran_et_la_legende_se_replie(telephone):
    assert not telephone.erreurs, telephone.erreurs

    boite = telephone.page.locator("#zone-carte").bounding_box()
    assert boite["y"] + boite["height"] >= 844 - 40, "la carte descend jusqu'au bas de l'ecran"
    assert boite["width"] == 390
    assert telephone.page.locator("#legende-bascule").get_attribute("aria-expanded") == "false"
    # Le titre tient dans la bande du haut, a cote de la pastille, sans la
    # faire grandir.
    titre = telephone.page.locator("#entete h1")
    assert titre.is_visible()
    # « Aller a » est reserve au grand ecran.
    assert telephone.page.locator("#aller-a").is_hidden()
    assert telephone.page.locator(".vue-initiale").is_visible()
    assert telephone.page.locator("#entete").bounding_box()["height"] <= 52

    # Les filtres s'annoncent, et leur tiroir couvre l'ecran.
    bouton = telephone.page.locator("#bouton-filtres")
    assert bouton.is_visible() and bouton.inner_text().strip().endswith("Filtres")
    assert not telephone.page.locator("#panneau").is_visible() or \
        telephone.page.locator("#panneau").bounding_box()["x"] < -300
    bouton.click()
    telephone.page.wait_for_function(
        "() => document.querySelector('#panneau').getBoundingClientRect().left >= 0"
    )
    assert telephone.page.locator("#panneau").bounding_box()["width"] >= 390 * 0.8
    telephone.page.locator("#fermer-panneau").click()
    telephone.page.wait_for_function(
        "() => document.querySelector('#panneau').getBoundingClientRect().right <= 0"
    )


def test_sur_telephone_l_infobulle_d_une_gare_se_pose_au_dessus_d_elle(telephone):
    telephone.aller(2.3375, 48.8606, 15)
    # La gare la plus proche du centre : pres d'un bord, la bulle n'aurait pas
    # la place de se poser au-dessus.
    gare = telephone.carte(
        "(carte) => { const c = carte.project(carte.getCenter());"
        " const gares = carte.queryRenderedFeatures({layers: ['gares']});"
        " const d = (e) => { const p = carte.project(e.geometry.coordinates); return Math.hypot(p.x - c.x, p.y - c.y); };"
        " gares.sort((a, b) => d(a) - d(b)); return gares.length && gares[0].geometry.coordinates; }"
    )
    assert gare, "une gare est visible"
    x, y = telephone.point_ecran(*gare)
    telephone.page.touchscreen.tap(x, y)

    infobulle = telephone.page.locator("#infobulle")
    infobulle.wait_for(state="visible")
    assert "ancree" in infobulle.get_attribute("class")
    bulle = infobulle.bounding_box()
    assert bulle["y"] + bulle["height"] <= y, "la bulle est au-dessus de la gare"


def test_sur_telephone_un_appui_sur_une_zone_ouvre_l_infobulle_sans_zoomer(telephone):
    """Pas de survol au doigt : l'appui reste le seul moyen de lire une zone."""

    infobulle = telephone.page.locator("#infobulle")
    if infobulle.is_visible():
        infobulle.locator(".fermer").click()
    telephone.aller(2.40, 48.85, 11)
    avant = telephone.carte("(carte) => carte.getZoom()")
    telephone.page.touchscreen.tap(*telephone.point_ecran(2.40, 48.85))
    infobulle = telephone.page.locator("#infobulle")
    infobulle.wait_for(state="visible")
    assert "Prix médian au m²" in infobulle.inner_text()
    telephone.page.wait_for_timeout(600)
    assert telephone.carte("(carte) => carte.getZoom()") == pytest.approx(avant)
    infobulle.locator(".fermer").click()


def test_sur_telephone_un_appui_montre_le_chemin_de_la_gare(telephone):
    """Pas de survol au doigt : l'appui trace le trait, et le bandeau de
    l'infobulle donne la gare et le temps de marche."""

    telephone.aller(2.4005, 48.7890, 15.2)
    telephone.page.touchscreen.tap(*telephone.point_ecran(2.4005, 48.7890))
    telephone.page.wait_for_function("() => !!window.CarteDvf.trajet()", timeout=ATTENTE_MS)
    infobulle = telephone.page.locator("#infobulle")
    infobulle.wait_for(state="visible")
    assert "min à pied" in infobulle.locator(".gare-proche").inner_text()
    assert telephone.page.locator("#trajets .pastille-trajet").first.is_visible()
    infobulle.locator(".fermer").click()
    telephone.page.wait_for_function("() => !window.CarteDvf.trajet()", timeout=ATTENTE_MS)


def test_sur_telephone_le_bandeau_ne_cache_ni_le_logement_ni_la_gare(telephone):
    """Un emplacement de 84 ventes, touche en bas de l'ecran : la carte glisse
    pour le garder au-dessus du bandeau, la gare la plus proche est citee avant
    la liste des ventes, et la pastille du temps de marche reste en vue."""

    lon, lat = 2.24702, 48.88346  # Puteaux, square Leon-Blum
    telephone.aller(lon, lat, 16.6)
    telephone.carte("(carte) => { const h = carte.getCanvas().clientHeight;"
                    " carte.panBy([0, -0.35 * h], {duration: 0}); }")
    telephone.attendre("(carte) => !carte.isMoving() && carte.areTilesLoaded()")
    telephone.page.touchscreen.tap(*telephone.point_ecran(lon, lat))
    infobulle = telephone.page.locator("#infobulle")
    infobulle.wait_for(state="visible")
    telephone.attendre("(carte) => !carte.isMoving()")
    telephone.page.wait_for_timeout(400)

    bandeau = infobulle.bounding_box()
    assert bandeau["height"] <= 844 * 0.42
    _, y = telephone.point_ecran(lon, lat)
    assert y < bandeau["y"] - 40, "le logement reste visible au-dessus du bandeau"
    gare = infobulle.locator(".gare-proche")
    boite = gare.bounding_box()
    assert boite["y"] + boite["height"] <= bandeau["y"] + bandeau["height"], "la gare est lisible sans defiler"
    assert gare.bounding_box()["y"] < infobulle.locator(".ventes").bounding_box()["y"]
    pastille = telephone.page.locator("#trajets .pastille-trajet").first
    assert pastille.is_visible()
    assert pastille.bounding_box()["y"] + pastille.bounding_box()["height"] <= bandeau["y"]
    infobulle.locator(".fermer").click()


def test_en_paysage_le_telephone_garde_la_carte_en_grand(navigateur, adresse):
    """Telephone tourne : la mise en page du telephone, pas celle de
    l'ordinateur, dont le panneau fixe ne laisserait qu'une bande de carte."""

    page = _ouvrir(navigateur, adresse, viewport={"width": 844, "height": 390},
                   is_mobile=True, has_touch=True, device_scale_factor=2)
    assert page.page.locator("#bouton-filtres").is_visible()
    assert not page.page.locator("#panneau").is_visible() or \
        page.page.locator("#panneau").bounding_box()["x"] < 0
    carte = page.page.locator("#zone-carte").bounding_box()
    assert carte["width"] == 844 and carte["height"] >= 390 - 90
    # Les reglages tiennent sur la ligne du selecteur de maille.
    mode = page.page.locator("#mode").bounding_box()
    reglages = page.page.locator("#reglages").bounding_box()
    assert abs(reglages["y"] - mode["y"]) < 4
    page.page.context.close()
