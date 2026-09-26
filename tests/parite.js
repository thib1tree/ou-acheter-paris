/* Rejoue `site/calcul.js` sous Node pour `tests/test_parite.py`.
 *
 *   node tests/parite.js <dossier>
 *
 * Le dossier contient `ventes.bin`, `ventes.json` (metadonnees), `regles.json`,
 * `reperes.json` et `scenarios.json` ; le resultat est ecrit dans
 * `resultat.json`, un element par scenario et par metrique. */
"use strict";
const fs = require("fs");
const path = require("path");
const Calcul = require(path.join(__dirname, "..", "site", "calcul.js"));

const dossier = process.argv[2];
const lire = (nom) => JSON.parse(fs.readFileSync(path.join(dossier, nom), "utf8"));
const binaire = fs.readFileSync(path.join(dossier, "ventes.bin"));
const tampon = binaire.buffer.slice(binaire.byteOffset, binaire.byteOffset + binaire.byteLength);
const ventes = Calcul.lireVentes(tampon, lire("ventes.json"));
const regles = lire("regles.json");
const reperes = lire("reperes.json");

const resultat = lire("scenarios.json").map((scenario) => {
  const stats = Calcul.statistiques(ventes, scenario.filtres, true);
  const parMetrique = {};
  for (const metrique of scenario.metriques) {
    parMetrique[metrique] = Calcul.couches(ventes, stats, metrique, regles, reperes);
  }
  return parMetrique;
});
fs.writeFileSync(path.join(dossier, "resultat.json"), JSON.stringify(resultat));
