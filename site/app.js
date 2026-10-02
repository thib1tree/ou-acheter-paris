/* La page du site statique : filtres, calcul, carte.
 *
 * Il n'y a pas de serveur derriere cette page. Tout ce qu'elle montre vient
 * de fichiers prepares par `scripts/construire_site.py`, que designe
 * `manifeste.json` :
 *
 * 1. **Ouverture.** La carte s'affiche avec les statistiques des filtres par
 *    defaut, calculees par Python a la construction : rien a attendre, rien
 *    a calculer.
 * 2. **Changement de filtre ou de metrique.** Les statistiques sont refaites
 *    dans un Web Worker (`calcul-worker.js`), sur les ventes du territoire
 *    en colonnes binaires. La carte ne se fige jamais : elle garde ses
 *    couleurs jusqu'a ce que les nouvelles soient pretes.
 * 3. **Anticipation.** Le fichier des ventes (3 Mo) se telecharge des que le
 *    navigateur a un moment de libre apres l'ouverture — sauf en mode
 *    economie de donnees, ou il attend le premier reglage.
 *
 * La carte elle-meme (`carte/carte.js`) recoit ses arguments par
 * `window.CarteDvf.appliquer`, et rend le choix de metrique par
 * `CarteDvfHote.metrique`.
 */
(function () {
  "use strict";

  var CLE_SECTION = "code_section";
  var CLE_COMMUNE = "code_commune";
  var PREFIXE = "donnees/";
  var MESSAGE_PANNE = "La carte n'a pas pu se charger. Rechargez la page dans un instant.";

  var manifeste = null;
  var regles = null;
  var reperes = { gares: null, etiquettes: null };
  var defaut = null;         // statistiques precalculees des filtres d'ouverture
  var affiche = null;        // dernier resultat pose sur la carte
  var etat = { annees: [], surfaceMin: 0, surfaceMax: 0, types: [], etats: [], metrique: null };

  var worker = null;
  var workerPret = false;
  var workerEnPanne = false;
  var demande = 0;

  //: Poignee pour les tests de bout en bout (`tests/test_navigateur.py`).
  var suivi = { calculs: 0, nb: null, metrique: null, complet: null };
  window.siteDvf = suivi;

  // ------------------------------------------------------------- Utilitaires

  function lireJson(chemin, options) {
    return fetch(chemin, options).then(function (reponse) {
      if (!reponse.ok) throw new Error("HTTP " + reponse.status + " sur " + chemin);
      return reponse.json();
    });
  }

  function sansPrefixe(chemin) {
    return chemin && chemin.indexOf(PREFIXE) === 0 ? chemin.slice(PREFIXE.length) : chemin;
  }

  function milliers(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  function dateFr(iso) {
    var p = String(iso || "").split("-");
    return p.length === 3 ? p[2] + "/" + p[1] + "/" + p[0] : "";
  }

  // ------------------------------------------------------------- Filtres

  /* Les filtres tels que le calcul les attend : les regles de
   * `stats.Filtres`, y compris les replis (aucune annee ou aucun type coche
   * vaut « tous »). */
  function filtresCalcul() {
    var annees = etat.annees.length ? etat.annees.slice() : manifeste.filtres.annees.slice();
    var types = etat.types.length ? etat.types.slice() : manifeste.filtres.types.slice();
    var tousEtats = manifeste.filtres.etats || [];
    var etats = etat.etats.length ? etat.etats.slice() : tousEtats.slice();
    var bas = etat.surfaceMin, haut = etat.surfaceMax;
    if (haut < bas) { var t = bas; bas = haut; haut = t; }
    return {
      annees: annees.sort(function (a, b) { return a - b; }),
      surface: [bas, haut],
      ouvert: haut >= manifeste.filtres.surface_plafond,
      types: types.sort(),
      etats: etats.sort(),
    };
  }

  /* Les memes filtres, sous la forme que la carte rejoue sur les ventes
   * individuelles (`charge.filtres_des_ventes`). */
  function filtresDesVentes(f) {
    return { an: f.annees, su: [f.surface[0], f.surface[1], f.ouvert], ty: f.types,
      et: f.etats.length ? f.etats : null };
  }

  function sontParDefaut(f) {
    var d = manifeste.filtres;
    return f.annees.join() === d.annees.join() && f.types.join() === d.types.slice().sort().join()
      && f.etats.join() === (d.etats || []).slice().sort().join()
      && f.surface[0] === 0 && f.ouvert;
  }

  // ------------------------------------------------------------- Carte

  function poserSurCarte(resultat) {
    var f = filtresCalcul();
    var fichiers = manifeste.fichiers;
    var geometries = {};
    geometries[CLE_SECTION] = sansPrefixe(fichiers.sections);
    if (fichiers.communes) geometries[CLE_COMMUNE] = sansPrefixe(fichiers.communes);
    affiche = resultat;
    window.CarteDvf.appliquer({
      geometries: geometries,
      couches: resultat.couches,
      fonds: manifeste.fonds,
      fond_initial: manifeste.fond_initial,
      metriques: regles.metriques_proposees,
      metrique: etat.metrique,
      mode_initial: "communes",
      points_manifeste: sansPrefixe(fichiers.points),
      points_filtres: filtresDesVentes(f),
      points_echelle: resultat.echelle_points,
      gares: reperes.gares,
      etiquettes: reperes.etiquettes,
      etiquettes_jeton: manifeste.territoire,
      legende: resultat.couches[CLE_SECTION].legende,
      cadrage: manifeste.cadrage,
      cadrage_jeton: manifeste.territoire,
      limites: manifeste.limites,
    });
    suivi.calculs += 1;
    suivi.nb = resultat.nb;
    suivi.metrique = etat.metrique;
    suivi.complet = resultat.complet !== false;
    majAlertes(resultat.nb);
  }

  // ------------------------------------------------------------- Calcul

  function signalerCalcul(texte) {
    var etatCalcul = document.getElementById("etat-calcul");
    var bandeau = document.getElementById("calcul");
    etatCalcul.textContent = texte || "";
    etatCalcul.hidden = !texte;
    bandeau.textContent = texte || "";
    bandeau.hidden = !texte;
  }

  function demarrerWorker() {
    if (worker || workerEnPanne) return;
    if (!window.Worker || !window.DecompressionStream) {
      workerEnPanne = true;
      signalerCalcul("Ce navigateur ne sait pas recalculer les statistiques : "
        + "seuls les filtres par défaut sont disponibles.");
      return;
    }
    var fichiers = manifeste.fichiers;
    worker = new Worker("calcul-worker.js");
    worker.onmessage = recevoir;
    worker.onerror = function (erreur) {
      if (window.console) console.error("[calcul]", erreur);
      panneCalcul();
    };
    worker.postMessage({
      type: "charger", ventes: fichiers.ventes, meta: fichiers.ventes_meta,
      voies: fichiers.voies, regles: regles,
    });
  }

  function panneCalcul() {
    workerEnPanne = true;
    signalerCalcul("Le calcul n'a pas pu se faire. Rechargez la page dans un instant.");
  }

  function recevoir(evenement) {
    var message = evenement.data || {};
    if (message.type === "pret") {
      workerPret = true;
      return;
    }
    if (message.type === "erreur") {
      if (window.console) console.error("[calcul]", message.message);
      panneCalcul();
      return;
    }
    if (message.type !== "couches" || message.id !== demande) return;
    poserSurCarte(message);
    if (message.complet) signalerCalcul("");
  }

  /* Un reglage a change : la carte garde ses couleurs jusqu'a ce que les
   * nouvelles soient pretes. */
  function recalculer() {
    var f = filtresCalcul();
    majRappelSurface(f);
    if (defaut && sontParDefaut(f) && etat.metrique === defaut.metrique) {
      demande += 1;
      signalerCalcul("");
      poserSurCarte(defaut);
      return;
    }
    if (workerEnPanne) return;
    demarrerWorker();
    if (!worker) return;
    demande += 1;
    signalerCalcul(workerPret ? "Calcul…" : "Chargement des ventes…");
    worker.postMessage({ type: "calculer", id: demande, filtres: f, metrique: etat.metrique });
  }

  var minuteur = 0;
  function recalculerBientot() {
    clearTimeout(minuteur);
    minuteur = setTimeout(recalculer, 350);
  }

  // ------------------------------------------------------------- Panneau

  function majAlertes(nb) {
    var alertes = [];
    if (!nb) alertes.push("Aucune vente ne correspond aux filtres. Élargissez la sélection.");
    if (etat.metrique === regles.cle_rendement && filtresCalcul().annees.length < regles.annees_min) {
      alertes.push("<b>Évolution annuelle du prix</b> : il faut au moins " + regles.annees_min
        + " années pour ajuster une tendance. Cochez d'autres années dans les filtres, "
        + "sinon la carte restera grise.");
    }
    document.getElementById("alertes").innerHTML = alertes.map(function (texte) {
      return '<div class="alerte">' + texte + "</div>";
    }).join("");
  }

  function majRappelSurface(f) {
    var texte = "De " + milliers(f.surface[0]) + " à " + milliers(f.surface[1]) + " m²"
      + (f.ouvert ? " et plus." : ".");
    document.getElementById("surface-rappel").textContent = texte;
  }

  function construirePanneau() {
    var filtres = manifeste.filtres;

    // Annees : des boutons en deux chiffres (l'annee entiere au survol), qui se
    // partagent une ligne ; une annee creuse se decoche sans toucher aux autres.
    var zoneAnnees = document.querySelector("#filtre-annees .boutons");
    filtres.annees.forEach(function (annee) {
      var bouton = document.createElement("button");
      bouton.type = "button";
      bouton.textContent = String(annee % 100).padStart(2, "0");
      bouton.title = String(annee);
      bouton.dataset.annee = annee;
      bouton.setAttribute("aria-pressed", "true");
      bouton.addEventListener("click", function () {
        var coche = bouton.getAttribute("aria-pressed") !== "true";
        bouton.setAttribute("aria-pressed", coche ? "true" : "false");
        etat.annees = Array.prototype.filter.call(
          zoneAnnees.querySelectorAll("button"),
          function (b) { return b.getAttribute("aria-pressed") === "true"; }
        ).map(function (b) { return Number(b.dataset.annee); });
        recalculer();
      });
      zoneAnnees.appendChild(bouton);
    });
    etat.annees = filtres.annees.slice();

    // Surface : deux champs. Une borne ronde se tape, elle ne se vise pas.
    var min = document.getElementById("surface-min");
    var max = document.getElementById("surface-max");
    [min, max].forEach(function (champ) {
      champ.max = filtres.surface_plafond;
      champ.title = "La borne haute inclut toutes les surfaces supérieures.";
    });
    max.value = filtres.surface_plafond;
    etat.surfaceMin = 0;
    etat.surfaceMax = filtres.surface_plafond;
    function lireSurfaces() {
      var borne = function (champ, repli) {
        var v = Math.round(Number(champ.value));
        if (champ.value === "" || !isFinite(v)) v = repli;
        return Math.min(Math.max(v, 0), filtres.surface_plafond);
      };
      etat.surfaceMin = borne(min, 0);
      etat.surfaceMax = borne(max, filtres.surface_plafond);
    }
    [min, max].forEach(function (champ) {
      champ.addEventListener("input", function () { lireSurfaces(); recalculerBientot(); });
      champ.addEventListener("change", function () {
        lireSurfaces();
        min.value = etat.surfaceMin;
        max.value = etat.surfaceMax;
        clearTimeout(minuteur);
        recalculer();
      });
    });

    // Types de bien : en dernier, on n'y touche presque jamais.
    var zoneTypes = document.querySelector("#filtre-types .cases");
    filtres.types.forEach(function (type) {
      var etiquette = document.createElement("label");
      var caseType = document.createElement("input");
      caseType.type = "checkbox";
      caseType.checked = true;
      caseType.value = type;
      caseType.addEventListener("change", function () {
        etat.types = Array.prototype.filter.call(
          zoneTypes.querySelectorAll("input"), function (c) { return c.checked; }
        ).map(function (c) { return c.value; });
        recalculer();
      });
      etiquette.appendChild(caseType);
      etiquette.appendChild(document.createTextNode(type));
      zoneTypes.appendChild(etiquette);
    });
    etat.types = filtres.types.slice();

    // Neuf ou ancien : le neuf (ventes sur plan) se paie plus cher, et
    // domine la ou l'on construit, autour des gares a venir.
    var zoneEtats = document.querySelector("#filtre-etats .cases");
    (filtres.etats || []).forEach(function (valeur) {
      var etiquette = document.createElement("label");
      var caseEtat = document.createElement("input");
      caseEtat.type = "checkbox";
      caseEtat.checked = true;
      caseEtat.value = valeur;
      caseEtat.addEventListener("change", function () {
        etat.etats = Array.prototype.filter.call(
          zoneEtats.querySelectorAll("input"), function (c) { return c.checked; }
        ).map(function (c) { return c.value; });
        recalculer();
      });
      etiquette.appendChild(caseEtat);
      etiquette.appendChild(document.createTextNode(valeur));
      zoneEtats.appendChild(etiquette);
    });
    etat.etats = (filtres.etats || []).slice();
    document.getElementById("filtre-etats").hidden = (filtres.etats || []).length < 2;
    majRappelSurface(filtresCalcul());
  }

  // Tiroir des filtres, sur telephone.
  function ouvrirFiltres(ouverts) {
    document.body.classList.toggle("filtres-ouverts", ouverts);
    var bouton = document.getElementById("bouton-filtres");
    bouton.setAttribute("aria-expanded", ouverts ? "true" : "false");
    bouton.classList.remove("appel");
  }

  document.getElementById("bouton-filtres").addEventListener("click", function () {
    ouvrirFiltres(!document.body.classList.contains("filtres-ouverts"));
  });
  document.getElementById("fermer-panneau").addEventListener("click", function () {
    ouvrirFiltres(false);
  });
  document.getElementById("zone-carte").addEventListener("pointerdown", function () {
    if (document.body.classList.contains("filtres-ouverts")) ouvrirFiltres(false);
  });
  document.addEventListener("keydown", function (evenement) {
    if (evenement.key === "Escape") ouvrirFiltres(false);
  });

  (function appelerVersLesFiltres() {
    try {
      if (window.sessionStorage.getItem("filtres-reperes")) return;
      window.sessionStorage.setItem("filtres-reperes", "1");
    } catch (erreur) { /* stockage indisponible : on appelle quand meme */ }
    document.getElementById("bouton-filtres").classList.add("appel");
  })();

  // ------------------------------------------------------------- Pied de page

  /* La Licence Ouverte demande de citer la source et sa date : la periode
   * des ventes en tient lieu, lue dans les donnees par la construction. */
  function poserPied() {
    var pied = document.getElementById("pied-source");
    pied.textContent = "";
    var lien = function (texte, adresse) {
      var a = document.createElement("a");
      a.href = adresse;
      a.textContent = texte;
      a.rel = "noopener";
      a.target = "_blank";
      return a;
    };
    pied.appendChild(document.createTextNode("Données : "));
    pied.appendChild(lien("DVF géolocalisées", manifeste.source.url));
    pied.appendChild(document.createTextNode(", DGFiP / Etalab, "));
    pied.appendChild(lien("Licence Ouverte 2.0", manifeste.source.licence));
    var periode = manifeste.periode
      ? " · ventes du " + dateFr(manifeste.periode.debut) + " au " + dateFr(manifeste.periode.fin)
      : "";
    pied.appendChild(document.createTextNode(periode + "."));
  }

  // ------------------------------------------------------------- Demarrage

  window.CarteDvfHote.metrique = function (cle) {
    etat.metrique = cle;
    recalculer();
  };

  function anticiper() {
    var connexion = navigator.connection || {};
    if (connexion.saveData) return;
    var lancer = function () { demarrerWorker(); };
    if (window.requestIdleCallback) window.requestIdleCallback(lancer, { timeout: 4000 });
    else setTimeout(lancer, 1500);
  }

  lireJson("manifeste.json", { cache: "no-cache" })
    .then(function (recu) {
      manifeste = recu;
      regles = manifeste.regles;
      etat.metrique = regles.metrique_par_defaut;
      construirePanneau();
      poserPied();
      var f = manifeste.fichiers;
      return Promise.all([lireJson(f.defaut), lireJson(f.gares), lireJson(f.etiquettes)]);
    })
    .then(function (recus) {
      defaut = recus[0];
      reperes.gares = recus[1];
      reperes.etiquettes = recus[2];
      poserSurCarte(defaut);
      anticiper();
    })
    .catch(function (erreur) {
      if (window.console) console.error("[site]", erreur);
      if (window.CarteDvf) window.CarteDvf.panne(MESSAGE_PANNE);
    });
})();
