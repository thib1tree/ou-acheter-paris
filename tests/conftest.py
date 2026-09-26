"""Ressources partagees entre fichiers de tests."""

from __future__ import annotations

import fnmatch
import functools
import http.server
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def site_construit(tmp_path_factory):
    """Le site statique, construit une fois pour toute la session de tests.

    Rend `(dossier, manifeste)`. Construit dans un dossier temporaire plutot
    que dans `dist/`, pour ne jamais tester un site perime.
    """

    sys.path.insert(0, str(RACINE))
    sys.path.insert(0, str(RACINE / "scripts"))
    from src.ingestion import territoire_prepare

    if not territoire_prepare("grand-paris"):
        pytest.skip("territoire non livre")
    import construire_site

    sortie = tmp_path_factory.mktemp("site") / "dist"
    manifeste = construire_site.construire("grand-paris", sortie)
    return sortie, manifeste


@contextmanager
def servir(dossier: Path):
    """Sert `dossier` comme Cloudflare Pages : fichiers statiques, et les
    en-tetes de son `_headers` (motifs `/chemin/*`). Rend l'adresse."""

    regles = []
    for bloc in (dossier / "_headers").read_text(encoding="utf-8").split("\n\n"):
        lignes = [ligne.strip() for ligne in bloc.splitlines() if ligne.strip()]
        if lignes:
            regles.append((lignes[0], [ligne.split(": ", 1) for ligne in lignes[1:]]))

    class Gestionnaire(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            for motif, entetes in regles:
                if fnmatch.fnmatch(self.path.split("?")[0], motif):
                    for nom, valeur in entetes:
                        self.send_header(nom, valeur)
            super().end_headers()

        def log_message(self, *args):
            pass

    serveur = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Gestionnaire, directory=str(dossier))
    )
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{serveur.server_address[1]}/"
    finally:
        serveur.shutdown()
