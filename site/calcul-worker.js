/* Web Worker : les statistiques se recalculent ici, jamais sur le fil de la carte.
 *
 * Un changement de filtre refait les statistiques des 650 000 ventes du
 * territoire : 0,2 a 1,2 s sur un telephone moyen. Sur le fil principal, la
 * carte cesserait de repondre au doigt pendant ce temps ; ici, elle continue
 * de glisser et de zoomer.
 *
 * Le calcul se fait en deux temps. D'abord ce qui decide des couleurs
 * (medianes et moyennes au m², effectifs, evolution annuelle) : c'est ce qu'on
 * regarde, et c'est rapide. Ensuite les medianes du prix total et de la
 * surface, qui demandent un tri et ne servent qu'aux infobulles. Si un autre
 * filtre arrive entre-temps, le second temps est abandonne.
 *
 * Messages recus :
 *   { type: "charger", ventes, meta, voies, regles }   adresses et regles
 *   { type: "calculer", id, filtres, metrique }
 * Messages envoyes :
 *   { type: "pret", nb }  ou  { type: "erreur", message }
 *   { type: "couches", id, complet, couches, echelle_points, nb }
 */
"use strict";
importScripts("calcul.js");

var ventes = null;
var regles = null;
var reperes = null;
var demande = 0;
//: Statistiques deja calculees, par jeu de filtres : revenir a un reglage
//: deja vu, ou changer seulement de metrique, ne recalcule rien.
var memoire = new Map();
var PLAFOND_MEMOIRE = 8;

/* Le fichier des ventes est compresse en gzip. Selon l'hebergeur, il arrive
 * deja decompresse par le navigateur (en-tete `Content-Encoding`) ou tel
 * quel : les deux premiers octets le disent. */
function lireBinaire(adresse) {
  return fetch(adresse).then(function (reponse) {
    if (!reponse.ok) throw new Error("HTTP " + reponse.status + " sur " + adresse);
    return reponse.arrayBuffer();
  }).then(function (tampon) {
    var tete = new Uint8Array(tampon, 0, Math.min(2, tampon.byteLength));
    if (tete[0] !== 0x1f || tete[1] !== 0x8b) return tampon;
    var flux = new Blob([tampon]).stream().pipeThrough(new DecompressionStream("gzip"));
    return new Response(flux).arrayBuffer();
  });
}

function lireJson(adresse) {
  return fetch(adresse).then(function (reponse) {
    if (!reponse.ok) throw new Error("HTTP " + reponse.status + " sur " + adresse);
    return reponse.json();
  });
}

function retenir(cle, stats) {
  memoire.delete(cle);
  memoire.set(cle, stats);
  while (memoire.size > PLAFOND_MEMOIRE) memoire.delete(memoire.keys().next().value);
}

function envoyer(id, stats, metrique, complet) {
  var resultat = CalculDvf.couches(ventes, stats, metrique, regles, reperes);
  postMessage({
    type: "couches", id: id, complet: complet, couches: resultat.couches,
    echelle_points: resultat.echelle_points, nb: resultat.nb,
  });
}

function calculer(message) {
  var id = message.id;
  var cle = JSON.stringify(message.filtres);
  var connues = memoire.get(cle);
  if (connues && connues.complet) {
    retenir(cle, connues);
    envoyer(id, connues, message.metrique, true);
    return;
  }
  var complet = CalculDvf.exigeComplet(message.metrique);
  var stats = connues && !complet ? connues : CalculDvf.statistiques(ventes, message.filtres, complet);
  stats.complet = complet;
  retenir(cle, stats);
  envoyer(id, stats, message.metrique, complet);
  if (complet) return;
  // Le second temps attend son tour : un filtre arrive entre-temps passe
  // devant, et ce calcul-ci devient inutile.
  setTimeout(function () {
    if (id !== demande) return;
    var entieres = CalculDvf.statistiques(ventes, message.filtres, true);
    entieres.complet = true;
    retenir(cle, entieres);
    envoyer(id, entieres, message.metrique, true);
  }, 0);
}

var charge = null;

onmessage = function (evenement) {
  var message = evenement.data || {};
  if (message.type === "charger") {
    regles = message.regles;
    charge = Promise.all([
      lireBinaire(message.ventes), lireJson(message.meta),
      message.voies ? lireJson(message.voies) : Promise.resolve({}),
    ]).then(function (recus) {
      ventes = CalculDvf.lireVentes(recus[0], recus[1]);
      reperes = recus[2];
      postMessage({ type: "pret", nb: ventes.n });
    }).catch(function (erreur) {
      postMessage({ type: "erreur", message: String(erreur && erreur.message || erreur) });
      throw erreur;
    });
  } else if (message.type === "calculer") {
    demande = message.id;
    charge.then(function () {
      if (message.id !== demande) return;
      try {
        calculer(message);
      } catch (erreur) {
        postMessage({ type: "erreur", id: message.id, message: String(erreur && erreur.message || erreur) });
      }
    });
  }
};
