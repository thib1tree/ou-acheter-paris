/* Statistiques par zone, recalculees dans le navigateur.
 *
 * C'est le portage de `src/stats.py` et `src/charge.py` : filtrer les ventes,
 * les agreger par section et par commune, en tirer couleurs, legendes et
 * chiffres des infobulles. Le site statique n'a pas de serveur pour le
 * faire, et les filtres ont trop de combinaisons pour tout precalculer.
 *
 * **Les memes chiffres que Python, a l'arrondi pres.** Chaque operation
 * reprend celle de pandas ou de numpy, jusqu'a leurs details :
 *
 * - arrondi **au pair** (`numpy.round`), pas « au-dessus » comme `Math.round` ;
 * - sommes **compensees** (Kahan), comme les agregats groupes de pandas ;
 * - centiles par **interpolation lineaire**, avec la formule de `numpy`
 *   (qui calcule depuis la borne haute au-dela de la moitie de l'intervalle) ;
 * - mise en forme des nombres comme le `format` de Python (arrondi au pair
 *   des cas exactement a mi-chemin, que `toFixed` arrondit au-dessus).
 *
 * `tests/test_parite.py` le verifie sur un jeu de reference produit par
 * Python. Les palettes, seuils et phrases ne sont pas recopies ici : ils
 * arrivent de Python avec les donnees (`charge.regles_calcul`).
 *
 * Le fichier sert tel quel dans la page, dans le Web Worker et sous Node.
 */
(function (racine, fabrique) {
  "use strict";
  var module_ = fabrique();
  if (typeof module === "object" && module.exports) module.exports = module_;
  else racine.CalculDvf = module_;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var CLE_SECTION = "code_section";
  var CLE_COMMUNE = "code_commune";

  // ---------------------------------------------------------------- Nombres

  /* `numpy.round` : au plus proche, et au pair quand la valeur tombe
   * exactement a mi-chemin. `Math.round` arrondirait 2,5 a 3. */
  function arrondiPair(x) {
    var r = Math.round(x);
    if (r - x === 0.5 && r % 2 !== 0) r -= 1;
    return r;
  }

  /* Somme compensee de Kahan, telle que l'ecrivent les agregats groupes de
   * pandas (`group_sum`, `group_mean`). */
  function Somme() { this.s = 0; this.c = 0; }
  Somme.prototype.ajouter = function (v) {
    var y = v - this.c;
    var t = this.s + y;
    this.c = t - this.s - y;
    this.s = t;
  };

  /* Centile lineaire de `numpy.quantile` sur un tableau deja trie. */
  function centile(tries, q) {
    var n = tries.length;
    if (!n) return NaN;
    var vi = (n - 1) * q;
    if (vi >= n - 1) return tries[n - 1];
    var bas = Math.floor(vi);
    var g = vi - bas;
    var a = tries[bas], b = tries[bas + 1], d = b - a;
    return g >= 0.5 ? b - d * (1 - g) : a + d * g;
  }

  function trier(valeurs) {
    return Float64Array.from(valeurs).sort();
  }

  /* Mediane d'une tranche triee `[bas, haut)`, comme pandas : la moyenne des
   * deux valeurs centrales quand leur nombre est pair. */
  function mediane(tableau, bas, haut) {
    var n = haut - bas;
    if (!n) return NaN;
    var m = bas + (n >> 1);
    return n & 1 ? tableau[m] : (tableau[m - 1] + tableau[m]) / 2;
  }

  // ------------------------------------------------------------ Mise en forme

  /* Chiffres de `abs(x)` arrondis a `f` decimales, comme le `format` de
   * Python : valeur binaire exacte, et cas exactement a mi-chemin arrondis au
   * pair. `toFixed` fait tout pareil, sauf ce dernier cas, qu'il arrondit
   * au-dessus : on le detecte et on le corrige. */
  function chiffres(x, f) {
    var a = Math.abs(x);
    var texte = a.toFixed(f);
    var n = Number(texte.replace(".", ""));
    var cinq = Math.pow(5, f);
    var impair = 2 * n - 1;
    if (n % 2 === 1 && impair % cinq === 0
        && a === (impair / cinq) / Math.pow(2, f + 1)) {
      var entier = String(n - 1);
      while (entier.length <= f) entier = "0" + entier;
      texte = f ? entier.slice(0, -f) + "." + entier.slice(-f) : entier;
    }
    return texte;
  }

  function negatif(x) { return x < 0 || Object.is(x, -0); }

  function milliers(entier) {
    return entier.replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  function absente(x) { return x === null || x === undefined || !isFinite(x); }

  /* `stats.formater_euros` */
  function formaterEuros(valeur, suffixe) {
    if (suffixe === undefined) suffixe = " €";
    if (absente(valeur)) return "n/d";
    return (negatif(valeur) ? "-" : "") + milliers(chiffres(valeur, 0)) + suffixe;
  }

  /* `stats.formater_taux_annuel` */
  function formaterTauxAnnuel(valeur) {
    if (absente(valeur)) return "n/d";
    return (negatif(valeur) ? "-" : "+") + chiffres(valeur, 1).replace(".", ",") + " %/an";
  }

  // ---------------------------------------------------------------- Couleurs

  function composantes(couleur) {
    var h = couleur.replace("#", "");
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }

  function hex2(v) { return (v < 16 ? "0" : "") + v.toString(16); }

  /* `stats.interpoler_serie`, pour une position. */
  function interpoler(palette, rgb, position, gris) {
    if (!isFinite(position)) return gris;
    var e = Math.min(Math.max(position, 0), 1) * (palette.length - 1);
    var bas = Math.floor(e);
    var haut = Math.min(bas + 1, palette.length - 1);
    var part = e - bas;
    var sortie = "#";
    for (var k = 0; k < 3; k++) {
      sortie += hex2(arrondiPair(rgb[bas][k] + (rgb[haut][k] - rgb[bas][k]) * part));
    }
    return sortie;
  }

  /* `stats.bornes_couleur` puis `EchelleCouleur.depuis` */
  function echelleDepuis(valeurs) {
    var finies = valeurs.filter(function (v) { return !absente(v); });
    if (!finies.length) return { bas: 0, haut: 1, vide: true };
    var tries = trier(finies);
    var bas = centile(tries, 0.05), haut = centile(tries, 0.95);
    if (!isFinite(bas) || !isFinite(haut) || haut <= bas) {
      bas = tries[0];
      haut = tries[tries.length - 1];
    }
    if (haut <= bas) return { bas: bas - 0.5, haut: bas + 0.5, vide: false };
    return { bas: bas, haut: haut, vide: false };
  }

  function positionEchelle(echelle, valeur) {
    if (absente(valeur)) return NaN;
    if (echelle.haut <= echelle.bas) return 0.5;
    return (valeur - echelle.bas) / (echelle.haut - echelle.bas);
  }

  function graduations(echelle, nombre) {
    var sortie = [];
    for (var i = 0; i < nombre; i++) {
      sortie.push(echelle.bas + i / (nombre - 1) * (echelle.haut - echelle.bas));
    }
    return sortie;
  }

  /* `stats.amplitude_rendement` */
  function amplitudeRendement(taux, paliers) {
    var abs = taux.filter(function (v) { return !absente(v); }).map(Math.abs);
    if (!abs.length) return paliers[0];
    var tries = trier(abs);
    var observee = centile(tries, 0.9);
    if (!isFinite(observee) || observee <= 0) observee = tries[tries.length - 1] || 0;
    for (var i = 0; i < paliers.length; i++) if (observee <= paliers[i]) return paliers[i];
    return paliers[paliers.length - 1];
  }

  // ------------------------------------------------------------------ Ventes

  var TYPES = { u1: Uint8Array, u2: Uint16Array, u4: Uint32Array };

  /* Lit le fichier binaire de `src/binaire.py`. Les colonnes sont des vues
   * sur le tampon, sans copie ; valeur, surface et prix au m² sont
   * reconstitues une fois pour toutes. */
  function lireVentes(tampon, meta) {
    if (meta.version !== 1) throw new Error("format de ventes inconnu : " + meta.version);
    var c = {};
    meta.colonnes.forEach(function (col) {
      c[col.nom] = new TYPES[col.type](tampon, col.debut, col.n);
    });
    var n = meta.n;
    var valeur = new Float64Array(n), surface = new Float64Array(n), m2 = new Float64Array(n);
    for (var i = 0; i < n; i++) {
      valeur[i] = (c.euros[i] * 100 + c.centimes[i]) / 100;
      surface[i] = (c.metres[i] * 100 + c.centiemes[i]) / 100;
      m2[i] = arrondiPair(valeur[i] / surface[i]);
    }
    return {
      meta: meta, n: n, bornesCommunes: c.communes, section: c.section,
      annee: c.annee, type: c.type, etat: c.etat || new Uint8Array(n),
      valeur: valeur, surface: surface, m2: m2,
      nbSections: meta.sections.codes.length, nbCommunes: meta.communes.codes.length,
    };
  }

  // ------------------------------------------------------------ Statistiques

  /* `stats.filtrer`, rendu comme la liste des lignes retenues, dans l'ordre du
   * fichier (commune, puis prix au m² croissant). */
  function filtrer(v, filtres) {
    var anOk = new Uint8Array(256), tyOk = new Uint8Array(256), etOk = new Uint8Array(256);
    var annees = filtres.annees && filtres.annees.length ? filtres.annees : null;
    var types = filtres.types && filtres.types.length ? filtres.types : null;
    var etats = filtres.etats && filtres.etats.length ? filtres.etats : null;
    if (annees) annees.forEach(function (a) { var k = a - v.meta.annee0; if (k >= 0 && k < 256) anOk[k] = 1; });
    else anOk.fill(1);
    if (types) types.forEach(function (t) { var k = v.meta.types.indexOf(t); if (k >= 0) tyOk[k] = 1; });
    else tyOk.fill(1);
    // Un fichier sans colonne d'etat ne connait que l'ancien (`stats.etats_des_ventes`).
    var etatsConnus = v.meta.etats || ["Ancien"];
    if (etats) etats.forEach(function (e) { var k = etatsConnus.indexOf(e); if (k >= 0) etOk[k] = 1; });
    else etOk.fill(1);
    var surface = filtres.surface || null;
    var mini = surface ? surface[0] : -Infinity;
    var maxi = surface && !filtres.ouvert ? surface[1] : Infinity;

    var retenues = new Uint32Array(v.n);
    var debuts = new Uint32Array(v.nbCommunes + 1);
    var k = 0;
    for (var g = 0; g < v.nbCommunes; g++) {
      debuts[g] = k;
      for (var i = v.bornesCommunes[g]; i < v.bornesCommunes[g + 1]; i++) {
        var s = v.surface[i];
        if (anOk[v.annee[i]] && tyOk[v.type[i]] && etOk[v.etat[i]] && s >= mini && s <= maxi) retenues[k++] = i;
      }
    }
    debuts[v.nbCommunes] = k;
    return { lignes: retenues.subarray(0, k), debuts: debuts };
  }

  /* Les lignes retenues regroupees par section, par un tri par comptage
   * stable : chaque section garde l'ordre des prix au m². Seules les
   * sections a cheval sur deux communes (quelques dizaines) doivent etre
   * retriees. */
  function parSection(v, retenues) {
    var nz = v.nbSections;
    var compte = new Uint32Array(nz + 2);
    var lignes = retenues.lignes, k = lignes.length, j, s;
    for (j = 0; j < k; j++) { s = v.section[lignes[j]]; compte[(s === 0xffff ? nz : s) + 1]++; }
    for (s = 0; s <= nz; s++) compte[s + 1] += compte[s];
    var debuts = compte.slice(0, nz + 1);
    var position = compte.slice(0, nz + 1);
    var sortie = new Uint32Array(k);
    for (j = 0; j < k; j++) { s = v.section[lignes[j]]; sortie[position[s === 0xffff ? nz : s]++] = lignes[j]; }
    for (s = 0; s < nz; s++) {
      for (j = debuts[s] + 1; j < debuts[s + 1]; j++) {
        if (v.m2[sortie[j]] < v.m2[sortie[j - 1]]) {
          var tranche = Array.from(sortie.subarray(debuts[s], debuts[s + 1]));
          tranche.sort(function (a, b) { return v.m2[a] - v.m2[b]; });
          sortie.set(tranche, debuts[s]);
          break;
        }
      }
    }
    return { lignes: sortie, debuts: debuts };
  }

  /* `stats.statistiques_sections` pour toutes les zones d'une maille (celles
   * sans vente retenue ont `n = 0`), plus les medianes annuelles du prix au
   * m² qu'utilise l'evolution annuelle. `complet` ajoute les medianes du prix total
   * et de la surface, les seules qui demandent un tri : c'est la moitie du
   * temps de calcul. */
  function agreger(v, groupes, nz, complet) {
    var lignes = groupes.lignes, debuts = groupes.debuts;
    var r = {
      n: new Uint32Array(nz), m2med: new Float64Array(nz), m2moy: new Float64Array(nz),
      vmed: new Float64Array(nz).fill(NaN), vmoy: new Float64Array(nz),
      smed: new Float64Array(nz).fill(NaN), vmax: new Float64Array(nz), vmin: new Float64Array(nz),
      annuel: new Array(nz), complet: !!complet,
    };
    var nbAnnees = 256;
    var parAn = new Uint32Array(nbAnnees), vus = new Uint32Array(nbAnnees);
    var v0 = new Float64Array(nbAnnees), v1 = new Float64Array(nbAnnees);
    var valeurs = new Float64Array(lignes.length);
    for (var z = 0; z < nz; z++) {
      var bas = debuts[z], haut = debuts[z + 1], n = haut - bas;
      r.n[z] = n;
      if (!n) continue;
      var m2 = new Somme(), va = new Somme(), vmax = -Infinity, vmin = Infinity, j, i, a;
      parAn.fill(0);
      for (j = bas; j < haut; j++) {
        i = lignes[j];
        m2.ajouter(v.m2[i]);
        va.ajouter(v.valeur[i]);
        if (v.valeur[i] > vmax) vmax = v.valeur[i];
        if (v.valeur[i] < vmin) vmin = v.valeur[i];
        parAn[v.annee[i]]++;
      }
      var centre = bas + (n >> 1);
      r.m2med[z] = n & 1 ? v.m2[lignes[centre]] : (v.m2[lignes[centre - 1]] + v.m2[lignes[centre]]) / 2;
      r.m2moy[z] = arrondiPair(m2.s / n);
      r.vmoy[z] = arrondiPair(va.s / n);
      r.vmax[z] = vmax;
      r.vmin[z] = vmin;

      // Medianes annuelles : les lignes sont triees par prix au m², donc
      // chaque annee aussi ; on n'en lit que les deux valeurs centrales.
      vus.fill(0);
      for (j = bas; j < haut; j++) {
        a = v.annee[lignes[j]];
        var rang = vus[a]++, na = parAn[a];
        if (rang === (na - 1) >> 1) v0[a] = v.m2[lignes[j]];
        if (rang === na >> 1) v1[a] = v.m2[lignes[j]];
      }
      var points = [];
      for (a = 0; a < nbAnnees; a++) {
        if (!parAn[a]) continue;
        points.push(a + v.meta.annee0, parAn[a], parAn[a] & 1 ? v1[a] : (v0[a] + v1[a]) / 2);
      }
      r.annuel[z] = points;

      if (complet) {
        for (j = bas; j < haut; j++) valeurs[j] = v.valeur[lignes[j]];
        valeurs.subarray(bas, haut).sort();
        r.vmed[z] = arrondiPair(mediane(valeurs, bas, haut));
        for (j = bas; j < haut; j++) valeurs[j] = v.surface[lignes[j]];
        valeurs.subarray(bas, haut).sort();
        r.smed[z] = arrondiPair(mediane(valeurs, bas, haut));
      }
    }
    return r;
  }

  /* Statistiques des deux mailles pour un jeu de filtres. */
  function statistiques(v, filtres, complet) {
    var communes = filtrer(v, filtres);
    var sections = parSection(v, communes);
    var s = {};
    s[CLE_COMMUNE] = agreger(v, communes, v.nbCommunes, complet);
    s[CLE_SECTION] = agreger(v, sections, v.nbSections, complet);
    s.nb = communes.lignes.length;
    return s;
  }

  /* `stats.rendement_sections` : regression ponderee sur le logarithme des
   * medianes annuelles, par sommes compensees comme `groupby().sum()`. */
  function rendements(agregats, nz, regles) {
    var volumeMin = Math.max(regles.volume_min_annuel, 1);
    var anneesMin = Math.max(regles.annees_min, 2);
    var premiere = Infinity, z, k;
    for (z = 0; z < nz; z++) {
      var p = agregats.annuel[z];
      if (!p) continue;
      for (k = 0; k < p.length; k += 3) {
        if (p[k + 1] >= volumeMin && p[k + 2] > 0 && p[k] < premiere) premiere = p[k];
      }
    }
    var sortie = new Array(nz);
    if (premiere === Infinity) return sortie;
    for (z = 0; z < nz; z++) {
      var pts = agregats.annuel[z];
      if (!pts) continue;
      var W = new Somme(), A = new Somme(), L = new Somme(), AA = new Somme(), AL = new Somme(), LL = new Somme();
      var nb = 0, debut = null, fin = null;
      for (k = 0; k < pts.length; k += 3) {
        if (!(pts[k + 1] >= volumeMin && pts[k + 2] > 0)) continue;
        var poids = pts[k + 1], annee = pts[k] - premiere, lv = Math.log(pts[k + 2]);
        W.ajouter(poids);
        A.ajouter(poids * annee);
        L.ajouter(poids * lv);
        AA.ajouter(poids * annee * annee);
        AL.ajouter(poids * annee * lv);
        LL.ajouter(poids * lv * lv);
        nb++;
        if (debut === null) debut = pts[k];
        fin = pts[k];
      }
      if (!nb) continue;
      var am = A.s / W.s, lm = L.s / W.s;
      var dispersion = AA.s - W.s * (am * am);
      var covariance = AL.s - W.s * am * lm;
      var varianceLog = LL.s - W.s * (lm * lm);
      var etendue = fin - debut;
      var fiable = nb >= anneesMin && etendue > 0 && dispersion > 0;
      var pente = fiable ? covariance / dispersion : NaN;
      var residuelle = varianceLog - pente * covariance;
      sortie[z] = {
        nb_annees: nb,
        taux: Math.expm1(pente) * 100,
        variation: Math.expm1(pente * etendue) * 100,
        regularite: fiable && varianceLog > 0 ? 1 - residuelle / varianceLog : NaN,
        fiable: fiable,
      };
    }
    return sortie;
  }

  // ------------------------------------------------------------------ Couches

  function nombre(x) { return absente(x) ? null : x; }

  function metriqueDe(regles, cle) {
    for (var i = 0; i < regles.metriques.length; i++) {
      if (regles.metriques[i].cle === cle) return regles.metriques[i];
    }
    throw new Error("métrique inconnue : " + cle);
  }

  var COLONNE_STATS = {
    prix_m2_median: "m2med", prix_m2_moyen: "m2moy",
    prix_total_median: "vmed", prix_total_moyen: "vmoy",
  };

  /* `charge.couche_complete` pour une maille. */
  function coucheComplete(v, stats, maille, cleMetrique, modeRendement, regles, reperes) {
    var agregats = stats[maille];
    var zones = maille === CLE_COMMUNE ? v.meta.communes : v.meta.sections;
    var nz = zones.codes.length;
    var mot = maille === CLE_COMMUNE ? "commune" : "section";
    var colonne = agregats[COLONNE_STATS[cleMetrique]];
    var metrique = metriqueDe(regles, cleMetrique);

    var presentes = [];
    for (var z = 0; z < nz; z++) if (agregats.n[z]) presentes.push(z);

    // Echelle des prix, meme en mode evolution : elle colore les ventes.
    var seuil = Math.max(regles.seuil_grisage, 0);
    var fournies = presentes.filter(function (z) { return agregats.n[z] > seuil; });
    var reference = (fournies.length ? fournies : presentes).map(function (z) { return colonne[z]; });
    var echelle = echelleDepuis(reference);

    var codes = presentes.map(function (z) { return zones.codes[z]; });
    var valeurs = presentes.map(function (z) {
      return [
        agregats.n[z], nombre(agregats.m2med[z]), nombre(agregats.m2moy[z]),
        nombre(agregats.vmed[z]), nombre(agregats.vmoy[z]), nombre(agregats.smed[z]),
        nombre(agregats.vmax[z]), nombre(agregats.vmin[z]),
      ];
    });
    var champs = regles.champs_zone.slice();
    var couleurs, notes, legende, resume;
    var gris = regles.gris;

    if (modeRendement) {
      var tendances = rendements(agregats, nz, regles);
      var fiables = [];
      presentes.forEach(function (z) { var t = tendances[z]; if (t && t.fiable) fiables.push(t.taux); });
      var amplitude = amplitudeRendement(fiables, regles.amplitudes);
      var rgbR = regles.palette_rendement.map(composantes);
      champs = champs.concat(regles.champs_rendement);
      couleurs = [];
      notes = [];
      presentes.forEach(function (z, rang) {
        var t = tendances[z];
        var sur = !!(t && t.fiable);
        couleurs.push(sur
          ? (amplitude > 0 ? interpoler(regles.palette_rendement, rgbR, (t.taux + amplitude) / (2 * amplitude), gris) : gris)
          : gris);
        valeurs[rang].push(
          sur ? nombre(t.taux) : null, sur ? nombre(t.variation) : null,
          sur ? nombre(t.regularite) : null, t ? t.nb_annees : null
        );
        notes.push(sur ? null : regles.notes.tendance);
      });
      legende = {
        titre: "Évolution annuelle du " + metrique.libelle.charAt(0).toLowerCase() + metrique.libelle.slice(1),
        palette: regles.palette_rendement.slice(),
        graduations: [formaterTauxAnnuel(-amplitude), formaterTauxAnnuel(0), formaterTauxAnnuel(amplitude)],
        note_gris: regles.notes.gris_rendement,
        note: regles.notes.rendement,
      };
      var tries = trier(fiables);
      resume = fiables.length + " " + mot + "(s) sur " + presentes.length + " atteignent "
        + regles.annees_min + " années à " + regles.volume_min_annuel + " ventes ou plus ; "
        + "médiane du territoire " + formaterTauxAnnuel(mediane(tries, 0, tries.length)) + ".";
    } else {
      var rgbP = regles.palette_prix.map(composantes);
      var grisees = 0;
      couleurs = [];
      notes = [];
      presentes.forEach(function (z) {
        var grisee = agregats.n[z] <= seuil;
        if (grisee) grisees++;
        couleurs.push(grisee || echelle.vide ? gris
          : interpoler(regles.palette_prix, rgbP, positionEchelle(echelle, colonne[z]), gris));
        notes.push(grisee ? regles.notes.volume[mot] : null);
      });
      legende = {
        titre: metrique.libelle,
        palette: regles.palette_prix.slice(),
        graduations: graduations(echelle, 5).map(function (x) { return formaterEuros(x, ""); }),
        note_gris: regles.notes.gris_prix,
      };
      resume = milliers(String(stats.nb)) + " ventes sur " + presentes.length + " " + mot + "s"
        + (grisees ? ", dont " + grisees + " grisée(s) faute de volume." : ".");
    }

    var voies = reperes ? codes.map(function (code) { return reperes[code] || null; }) : null;
    return {
      couche: {
        codes: codes,
        couleurs: couleurs,
        entetes: presentes.map(function (z) { return zones.entetes[z]; }),
        reperes: voies && voies.some(Boolean) ? voies : null,
        notes: notes.some(function (x) { return x !== null; }) ? notes : null,
        champs: champs,
        valeurs: valeurs,
        legende: legende,
        resume: resume,
      },
      echelle: echelle,
      colonne: metrique.colonne,
    };
  }

  /* `charge.calculer_couches` : couches des deux mailles, echelle des ventes
   * (celle des sections) et nombre de ventes retenues. */
  function couches(v, stats, choixMetrique, regles, reperes) {
    var modeRendement = choixMetrique === regles.cle_rendement;
    var cle = modeRendement ? regles.base_rendement : choixMetrique;
    var sortie = {}, echellePoints = null, colonne = null;
    [CLE_SECTION, CLE_COMMUNE].forEach(function (maille) {
      var c = coucheComplete(v, stats, maille, cle, modeRendement, regles, reperes);
      sortie[maille] = c.couche;
      if (maille === CLE_SECTION) { echellePoints = c.echelle; colonne = c.colonne; }
    });
    return {
      couches: sortie,
      echelle_points: {
        bas: echellePoints.bas, haut: echellePoints.haut, vide: echellePoints.vide,
        palette: regles.palette_prix.slice(),
        colonne: colonne === "prix_m2" ? "m2" : "va",
      },
      nb: stats.nb,
    };
  }

  /* La metrique a-t-elle besoin des medianes qui demandent un tri ? */
  function exigeComplet(choixMetrique) {
    return choixMetrique === "prix_total_median";
  }

  return {
    lireVentes: lireVentes,
    statistiques: statistiques,
    couches: couches,
    exigeComplet: exigeComplet,
    formaterEuros: formaterEuros,
    formaterTauxAnnuel: formaterTauxAnnuel,
    arrondiPair: arrondiPair,
    centile: centile,
  };
});
