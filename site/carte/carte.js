/* Carte DVF, adossee a MapLibre GL.
 *
 * Tout ce qui est *navigation* se joue ici : deplacement, zoom, choix de la
 * maille, apparition des ventes, infobulles. La page (`app.js`) n'intervient
 * qu'au changement d'un reglage, et ce qu'elle transmet alors n'est fait que
 * de couleurs et de chiffres — jamais une geometrie, jamais une vente.
 *
 * Trois principes tiennent la fluidite :
 *
 * 1. **Le zoom decide, pas un evenement.** La maille lue (communes ou
 *    sections), l'effacement de l'aplat et l'apparition des points sont des
 *    interpolations d'opacite sur le zoom, resolues par le GPU image par
 *    image. Les deux mailles sont posees en permanence et se fondent l'une
 *    dans l'autre : rien ne bascule, rien ne clignote, rien ne recalcule.
 *
 * 2. **Les ventes sont deja la.** Elles arrivent par tuiles statiques
 *    (`src/points.py`), chargees des qu'on approche du zoom ou elles
 *    apparaissent. Filtres et couleurs s'appliquent dans le navigateur : un
 *    changement de filtre ne retelecharge pas une tuile.
 *
 * 3. **Le clic sert a lire, ou a descendre d'un cran.** Sur une vente ou
 *    une gare, il ouvre une infobulle, et rien d'autre. Sur une zone, il
 *    depend de l'ecran : a la souris, le survol donne deja les chiffres, et
 *    le clic zoome sur la zone (`zoomerSurZone`) ; au doigt, ou le survol
 *    n'existe pas, il ouvre l'infobulle. Sur telephone, quand le bandeau de
 *    l'infobulle recouvrirait le logement touche, la carte glisse juste assez
 *    pour le garder en vue (`degagerDuBandeau`).
 */
(function () {
  "use strict";

  var GRIS = "#d9d9d9";
  var CLE_COMMUNE = "code_commune";
  var CLE_SECTION = "code_section";
  var VIDE = { type: "FeatureCollection", features: [] };

  /* ----------------------------------------------------------------- Zooms
   *
   * Les paliers qui suivent decrivent une seule descente continue : on
   * regarde des communes, puis des sections, puis des logements. Ils se
   * chevauchent volontairement — c'est le chevauchement qui fait le fondu.
   */

  //: Debut et fin du fondu communes -> sections. A 11,2 l'ecran tient une
  //: quinzaine de communes ; a 12,2 il n'en reste que quelques-unes, et la
  //: commune n'est plus la bonne unite de lecture.
  var Z_SECTIONS_DEBUT = 11.2;
  var Z_SECTIONS_PLEIN = 12.2;
  //: Zoom auquel on considere qu'on lit des sections plutot que des communes :
  //: c'est le milieu du fondu, et il ne sert qu'a savoir quelle couche
  //: interroger au clic et quelle legende ecrire.
  var Z_BASCULE = (Z_SECTIONS_DEBUT + Z_SECTIONS_PLEIN) / 2;

  //: Effacement de l'aplat. Passe ce zoom on ne compare plus des zones entre
  //: elles : on regarde *ou* sont les biens. La couleur masquerait alors le
  //: fond de carte — les rues, les parcs, la gare d'a cote — sans plus rien
  //: apprendre. Elle s'efface donc, et les ventes prennent sa place.
  var Z_EFFACEMENT_DEBUT = 14.6;
  //: L'aplat a disparu a l'instant ou les ventes atteignent leur pleine
  //: intensite (`Z_POINTS_PLEIN`), et pas un cran plus tard. Tant qu'il
  //: subsiste, chaque vente se detache sur une couleur tiree de la **meme**
  //: echelle qu'elle : un appartement au prix de sa section y serait
  //: invisible. L'une remplace l'autre, elles ne se
  //: superposent pas.
  var Z_EFFACEMENT_FIN = 15.6;

  //: Apparition des ventes, calee sur l'effacement de l'aplat.
  var Z_POINTS_DEBUT = 14.6;
  var Z_POINTS_PLEIN = 15.6;
  //: Zoom a partir duquel une vente peut etre designee. Cale a mi-fondu : une
  //: pastille encore transparente ne doit pas intercepter le clic destine a
  //: la section qu'elle recouvre.
  var Z_POINTS_POINTABLES = (Z_POINTS_DEBUT + Z_POINTS_PLEIN) / 2;
  //: Zoom a partir duquel les tuiles de ventes sont telechargees. Un cran
  //: avant leur apparition : le temps d'arriver, elles sont la.
  var Z_POINTS_CHARGE = 13.4;

  //: Zoom a partir duquel une gare peut etre designee : celui ou elle est
  //: deja bien lisible, et ou l'on cherche vraiment une gare plutot qu'une
  //: zone.
  //:
  //: En deca, elle n'est qu'un repere : dans Paris, il y a une station tous
  //: les trois cents metres, et un doigt qui vise une commune tomberait
  //: presque toujours sur l'une d'elles.
  var Z_GARES_POINTABLES = 12.5;
  //: Les gares lourdes (RER, Transilien) et les gares a venir se designent un
  //: demi-cran plus tot. Elles sont franchement visibles des le zoom 12, et
  //: dix fois moins serrees que les stations de metro : elles ne volent pas
  //: la commune qu'on vise.
  var Z_GARES_LOURDES_POINTABLES = 12;

  //: Au-dela, on regarde trop large pour que les tuiles de ventes aient un
  //: sens : le garde-fou evite qu'un ecran tres large en demande cinquante.
  var PLAFOND_TUILES_VISIBLES = 24;
  //: Tuiles gardees en memoire. Au-dela, les plus anciennes sont oubliees :
  //: une longue exploration ne doit pas finir par saturer un telephone.
  var PLAFOND_TUILES_MEMOIRE = 48;

  //: Zoom en deca duquel aucun nom de commune n'est ecrit : a cette echelle,
  //: la carte se lit par ses couleurs, pas par ses libelles. Il est cale sous
  //: la vue d'ouverture d'un telephone (zoom 8,3 environ : l'agglomeration
  //: tient dans 390 pixels de large), pour qu'elle porte elle aussi quelques
  //: reperes.
  var ZOOM_MIN_ETIQUETTES = 8;
  //: Combien de noms au plus, selon le zoom. L'anti-collision suffirait a
  //: eviter l'illisible, mais elle remplit tout l'espace disponible : en vue
  //: d'ensemble, une poignee de reperes vaut mieux que soixante.
  var PLAFOND_ETIQUETTES = [[8, 7], [8.6, 12], [10.5, 26], [12, 45], [14, 80]];
  //: Marge autour d'une etiquette, en pixels, pour l'anti-collision.
  var MARGE_ETIQUETTE = 4;

  //: Tolerance de pointage, en pixels. Un doigt ne vise pas au pixel pres :
  //: sans cette marge, on rate une pastille de vente une fois sur deux.
  var MARGE_CLIC_DOIGT = 12;
  var MARGE_CLIC_SOURIS = 2;
  //: Les gares ont leur propre marge : une icone de gare vue de loin ne fait
  //: qu'une dizaine de pixels. Assez pour la toucher sans viser au pixel
  //: pres, pas assez pour qu'elle avale la zone autour — et c'est toujours la
  //: gare **la plus proche** du pointeur qui repond.
  var MARGE_GARE_SOURIS = 6;
  var MARGE_GARE_DOIGT = 11;

  //: Vrai sur la zone survolee : elle s'appuie un peu plus fort.
  var SURVOLEE = ["boolean", ["feature-state", "survol"], false];

  //: Le navigateur sait-il survoler ? Un telephone n'emet de `mousemove` que
  //: des evenements synthetiques apres le tap, qui ouvriraient une infobulle
  //: fantome a chaque effleurement.
  var SURVOL_POSSIBLE =
    window.matchMedia && window.matchMedia("(hover: hover)").matches;

  var carte = null;
  var pret = false;
  var args = null;
  var enAttente = null;       // args recus avant que la carte soit prete
  var contoursPoses = {};     // source -> nom du fichier deja confie
  var contoursEnVol = {};     // source -> contour encore en cours de lecture
  var rangs = {};             // maille -> code de zone -> rang dans `couches`
  var mode = "communes";
  var mailleLue = CLE_COMMUNE;
  var dernierCadrage = null;
  var dernierFond = null;
  var fonds = {};             // nom -> { url, attr, zoom_max }
  var fondChoisi = null;      // fond retenu sur la carte, s'il l'a ete
  var survol = null;          // { source, id }
  var chargements = 0;
  var panne = false;

  //: Etiquettes de communes : une entree par commune, son `div` et sa largeur
  //: mesuree une fois pour toutes. Les frames suivantes ne font que deplacer
  //: ou masquer ces `div`, sans jamais toucher au texte — donc sans reflow.
  var etiquettes = [];
  var etiquettesJeton = null;
  var rafEtiquettes = 0;

  //: Ventes : manifeste des tuiles disponibles, tuiles deja telechargees, et
  //: empreinte des reglages ayant servi au dernier rendu.
  var manifeste = null;       // { zoom, tuiles: Set }
  var manifesteCharge = null; // nom de fichier du manifeste en memoire
  var tuiles = new Map();     // "x/y" -> colonnes de la tuile
  var tuilesEnVol = new Set();
  var pointsPoses = false;
  var rafPoints = 0;

  var infobulleFixee = false;
  //: Entite dont l'infobulle affiche deja le contenu (voir `cleEntite`).
  var infobulleCle = null;

  // ---------------------------------------------------------------- Hote
  //
  // La carte est posee dans la page, qui lui donne ses arguments par
  // `window.CarteDvf.appliquer(args)` et apprend le choix de metrique par
  // `window.CarteDvfHote.metrique(cle)`. `CarteDvfHote.base` est le dossier
  // des donnees.

  var hote = window.CarteDvfHote || {};

  window.addEventListener("resize", function () {
    if (carte) {
      carte.resize();
      // Tant que le visiteur n'a pas touche a la vue, elle se recadre a chaque
      // redimensionnement : l'agglomeration garde tout l'ecran quand on
      // tourne un telephone.
      if (!vueTouchee) cadrer(false);
      suivreAncre();
      suivreTrajet();
    }
    reglerLegende();
  });

  // ------------------------------------------------------------- Formatage

  var NOMBRE = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
  var DECIMAL = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  var DECIMAL2 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function formater(valeur, format) {
    if (valeur === null || valeur === undefined || valeur !== valeur) return "n/d";
    switch (format) {
      case "euros": return NOMBRE.format(valeur) + " €";
      case "euros_m2": return NOMBRE.format(valeur) + " €/m²";
      case "surface": return NOMBRE.format(valeur) + " m²";
      case "entier": return NOMBRE.format(valeur);
      case "reel": return DECIMAL2.format(valeur);
      case "pourcent": return (valeur >= 0 ? "+" : "") + DECIMAL.format(valeur) + " %";
      case "taux": return (valeur >= 0 ? "+" : "") + DECIMAL.format(valeur) + " %/an";
      default: return String(valeur);
    }
  }

  /* Date stockee en `AAAAMMJJ` : un entier, pour que les tuiles restent des
   * colonnes de nombres. */
  function formaterDate(entier) {
    var n = Number(entier);
    if (!(n > 0)) return "date inconnue";
    var texte = String(Math.floor(n));
    return texte.slice(6, 8) + "/" + texte.slice(4, 6) + "/" + texte.slice(0, 4);
  }

  function echapper(texte) {
    return String(texte).replace(/[&<>"']/g, function (caractere) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[caractere];
    });
  }

  // ------------------------------------------------------------- Fichiers

  /* Les donnees sont dans le dossier que la page indique (`CarteDvfHote.base`). */
  function urlStatique(nom) {
    return (hote.base || "") + nom;
  }

  function attendre(actif, message) {
    var boite = document.getElementById("attente");
    if (message) {
      // Une panne se lit jusqu'a ce qu'on recharge : la masquer au chargement
      // suivant laisserait une carte muette sans rien dire de pourquoi.
      boite.textContent = message;
      boite.hidden = false;
      panne = true;
      return;
    }
    chargements = Math.max(0, chargements + (actif ? 1 : -1));
    if (panne) return;
    boite.textContent = "Chargement des contours…";
    boite.hidden = chargements === 0;
  }

  function lireJson(nom) {
    return fetch(urlStatique(nom)).then(function (reponse) {
      if (!reponse.ok) throw new Error("HTTP " + reponse.status + " sur " + nom);
      return reponse.json();
    });
  }

  //: Ce que lit le visiteur quand la carte ne peut pas se dessiner. Une
  //: phrase et un geste — le detail technique part dans la console du
  //: navigateur, ou il sert a qui sait l'y chercher, et nulle part ailleurs.
  var MESSAGE_PANNE = "La carte n'a pas pu se charger. Rechargez la page dans un instant.";
  var MESSAGE_WEBGL =
    "Votre navigateur ne peut pas afficher cette carte (WebGL indisponible). "
    + "Essayez un navigateur récent, ou activez l'accélération graphique.";

  function signalerPanne(message, erreur) {
    if (erreur && window.console) console.error("[carte]", erreur);
    attendre(false, message || MESSAGE_PANNE);
  }

  // ------------------------------------------------------------- Icones
  //
  // Un rond ne dit pas ce qu'il montre : une gare, une station de metro, une
  // maison et un appartement s'y ressembleraient tous. Chaque point porte donc
  // un dessin — mais ce dessin doit rester **colorable**, puisque c'est la
  // couleur qui porte le prix.
  //
  // D'ou des icones SDF (champ de distance signe) : la couche alpha de
  // l'image ne porte pas des pixels mais la distance au bord du trace. Une
  // seule image par forme suffit alors pour toutes les teintes — MapLibre la
  // colore par `icon-color`, la cerne de blanc par `icon-halo-width`, et elle
  // reste nette a toutes les tailles. L'alternative aurait ete de
  // precalculer une image par forme *et par couleur* de l'echelle.
  //
  // Les six champs de distance sont calcules une fois au demarrage, en
  // quelques millisecondes, et ne coutent plus rien ensuite.

  //: Cote de l'image, et cote du dessin a l'interieur. La difference est la
  //: marge ou vit le champ de distance : sans elle, le halo serait coupe net.
  var ICONE_COTE = 48;
  var ICONE_DESSIN = 32;
  //: Constantes du nuanceur SDF de MapLibre (`#define SDF_PX 8.0`, bord a
  //: (256 - 64) / 256). Les respecter, c'est poser le bord exactement la ou
  //: MapLibre l'attend, et obtenir un halo dont la largeur se compte en
  //: pixels d'ecran.
  var SDF_RAYON = 8;
  var SDF_SEUIL = 0.25;

  /* Chaque icone est un trace dans une boite de 24 x 24, en deux parties : la
   * silhouette, puis ce qu'on y decoupe — fenetres, lettres. Les decoupes
   * sont de vrais trous : a douze pixels, une lettre evidee se lit quand un
   * aplat plein ne serait qu'une tache. */
  var TRACES = {
    train: {
      plein: "M6 2h12a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V5a3 3 0 0 1 3-3z"
        + "M5.6 18.4h3.4l-2.4 4H2.2zM15 18.4h3.4l4 4h-4.4z",
      creux: "M6.2 5.2h11.6v5.4H6.2zM8.2 12.9a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2z"
        + "M15.8 12.9a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2z",
    },
    metro: {
      plein: "M12 1.5a10.5 10.5 0 1 0 0 21 10.5 10.5 0 0 0 0-21z",
      creux: "M5.8 17.4V6.6h3.1L12 11.2l3.1-4.6h3.1v10.8h-2.9v-6.2L12 15.6 8.7 11.2v6.2z",
    },
    tram: {
      plein: "M4.5 2h15a2.5 2.5 0 0 1 2.5 2.5v15a2.5 2.5 0 0 1-2.5 2.5h-15"
        + "A2.5 2.5 0 0 1 2 19.5v-15A2.5 2.5 0 0 1 4.5 2z",
      creux: "M5.6 6.4h12.8v3H13.6v8.2h-3.2V9.4H5.6z",
    },
    maison: {
      plein: "M12 1.6 23 11.2h-3.4v11.2H4.4V11.2H1z",
      creux: "M9.8 14.2h4.4v8.2H9.8z",
    },
    immeuble: {
      plein: "M4 1.8h16v20.6H4z",
      creux: "M6.6 4.6h3.2v3.2H6.6zM14.2 4.6h3.2v3.2h-3.2z"
        + "M6.6 10.4h3.2v3.2H6.6zM14.2 10.4h3.2v3.2h-3.2z"
        + "M6.6 16.2h3.2v6.2H6.6zM14.2 16.2h3.2v3.2h-3.2z",
    },
    mixte: {
      plein: "M12 1.4 22.6 12 12 22.6 1.4 12z",
      creux: "M12 7.4 16.6 12 12 16.6 7.4 12z",
    },
  };

  /* Masque de l'icone : la silhouette, moins ses decoupes. `destination-out`
   * plutot qu'une regle de remplissage pair-impair, pour que deux morceaux de
   * silhouette puissent se chevaucher sans se trouer l'un l'autre. */
  function masqueIcone(trace) {
    var toile = document.createElement("canvas");
    toile.width = toile.height = ICONE_COTE;
    var ctx = toile.getContext("2d");
    var marge = (ICONE_COTE - ICONE_DESSIN) / 2;
    ctx.setTransform(ICONE_DESSIN / 24, 0, 0, ICONE_DESSIN / 24, marge, marge);
    ctx.fillStyle = "#fff";
    ctx.fill(new Path2D(trace.plein));
    if (trace.creux) {
      ctx.globalCompositeOperation = "destination-out";
      ctx.fill(new Path2D(trace.creux));
    }
    return ctx.getImageData(0, 0, ICONE_COTE, ICONE_COTE).data;
  }

  var INFINI = 1e20;

  /* Transformee de distance euclidienne exacte (Felzenszwalb) : une passe par
   * colonne, une par ligne, en distances au carre. C'est le calcul que fait
   * TinySDF pour les glyphes de MapLibre, et le format qui en sort est celui
   * que le nuanceur attend. */
  function distance1d(f, d, v, z, n) {
    v[0] = 0;
    z[0] = -INFINI;
    z[1] = INFINI;
    for (var q = 1, k = 0, s = 0; q < n; q++) {
      do {
        var r = v[k];
        s = (f[q] - f[r] + q * q - r * r) / (2 * q - 2 * r);
      } while (s <= z[k] && --k > -1);
      k++;
      v[k] = q;
      z[k] = s;
      z[k + 1] = INFINI;
    }
    for (var i = 0, j = 0; i < n; i++) {
      while (z[j + 1] < i) j++;
      var proche = v[j];
      d[i] = (i - proche) * (i - proche) + f[proche];
    }
  }

  function distance2d(grille, cote) {
    var f = new Float64Array(cote);
    var d = new Float64Array(cote);
    var v = new Int32Array(cote);
    var z = new Float64Array(cote + 1);
    var x, y;
    for (x = 0; x < cote; x++) {
      for (y = 0; y < cote; y++) f[y] = grille[y * cote + x];
      distance1d(f, d, v, z, cote);
      for (y = 0; y < cote; y++) grille[y * cote + x] = d[y];
    }
    for (y = 0; y < cote; y++) {
      for (x = 0; x < cote; x++) f[x] = grille[y * cote + x];
      distance1d(f, d, v, z, cote);
      for (x = 0; x < cote; x++) grille[y * cote + x] = d[x];
    }
  }

  /* L'image SDF elle-meme : distance au bord dans la couche alpha, encodee
   * comme l'attend le nuanceur — 192 sur le bord, un pixel valant un huitieme
   * de l'echelle. */
  function champDeDistance(trace) {
    var pixels = masqueIcone(trace);
    var n = ICONE_COTE * ICONE_COTE;
    var dehors = new Float64Array(n);
    var dedans = new Float64Array(n);
    for (var i = 0; i < n; i++) {
      var a = pixels[i * 4 + 3] / 255;
      dehors[i] = a === 1 ? 0 : a === 0 ? INFINI : Math.pow(Math.max(0, 0.5 - a), 2);
      dedans[i] = a === 1 ? INFINI : a === 0 ? 0 : Math.pow(Math.max(0, a - 0.5), 2);
    }
    distance2d(dehors, ICONE_COTE);
    distance2d(dedans, ICONE_COTE);

    var donnees = new Uint8Array(n * 4);
    for (var j = 0; j < n; j++) {
      var distance = Math.sqrt(dehors[j]) - Math.sqrt(dedans[j]);
      var alpha = Math.round(255 - 255 * (distance / SDF_RAYON + SDF_SEUIL));
      donnees[j * 4] = donnees[j * 4 + 1] = donnees[j * 4 + 2] = 255;
      donnees[j * 4 + 3] = alpha < 0 ? 0 : alpha > 255 ? 255 : alpha;
    }
    return { width: ICONE_COTE, height: ICONE_COTE, data: donnees };
  }

  function poserIcones() {
    for (var nom in TRACES) {
      if (!carte.hasImage("ico-" + nom)) {
        carte.addImage("ico-" + nom, champDeDistance(TRACES[nom]), { sdf: true });
      }
    }
  }

  /* Le meme trace, en SVG, pour que la legende montre exactement ce que
   * montre la carte. Les decoupes y sont remplies de blanc plutot que
   * percees : le fond de la legende est blanc, et c'est une balise de moins. */
  function symboleLegende(nom, couleur) {
    var trace = TRACES[nom];
    return '<svg class="ico" viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">'
      + '<path d="' + trace.plein + '" fill="' + couleur + '"></path>'
      + (trace.creux ? '<path d="' + trace.creux + '" fill="#ffffff"></path>' : "")
      + "</svg>";
  }

  // ------------------------------------------------------------- Carte

  /* Source raster d'un fond de carte. `maxzoom` n'est pose que si la source
   * annonce une limite : MapLibre agrandit alors ses dernieres tuiles au lieu
   * d'en demander qui n'existent pas. */
  function sourceFond(fond) {
    var source = {
      type: "raster", tiles: [fond.url], tileSize: 256, attribution: fond.attr || "",
    };
    if (fond.zoom_max) source.maxzoom = fond.zoom_max;
    return source;
  }

  /* Aucun fond au depart : c'est le manifeste qui designe le fond d'ouverture,
   * quelques centaines de millisecondes plus tard (`majFonds`). Aucune tuile
   * n'est donc demandee a un autre serveur que celui du fond par defaut. */
  function styleInitial() {
    return { version: 8, sources: {}, layers: [] };
  }

  function creer() {
    // Sans WebGL (vieux navigateur, acceleration graphique coupee, certains
    // postes d'entreprise), MapLibre ne peut rien dessiner du tout : on le
    // dit plutot que de laisser un cadre blanc.
    if (!window.maplibregl || (maplibregl.supported && !maplibregl.supported())) {
      signalerPanne(MESSAGE_WEBGL);
      return false;
    }
    try {
      construire();
    } catch (erreur) {
      carte = null;
      signalerPanne(MESSAGE_WEBGL, erreur);
      return false;
    }
    return true;
  }

  function construire() {
    // Une adresse partagee (`#zoom/lat/lon`) rouvre la vue qu'elle designe, et
    // la carte ne la recadre plus : c'est le visiteur qui l'a choisie.
    vueInitiale = vueDeLAdresse();
    if (vueInitiale) vueTouchee = true;
    carte = new maplibregl.Map({
      container: "carte",
      style: styleInitial(),
      center: vueInitiale ? vueInitiale.center : [2.3522, 48.8566],
      zoom: vueInitiale ? vueInitiale.zoom : 10,
      attributionControl: { compact: true },
      // Le rendu WebGL supporte sans peine les milliers de sections d'une
      // agglomeration ; c'est ce qui rend la maille cadastrale tenable.
      maxZoom: 19,
      minZoom: 7,
      // --- Carte strictement plane, toujours orientee au nord. ---
      // Une carte qu'on fait pivoter par megarde est penible a remettre
      // d'aplomb — et au doigt, la rotation se declenche a chaque pincement.
      dragRotate: false,
      pitchWithRotate: false,
      rollEnabled: false,
      touchPitch: false,
      maxPitch: 0,
      bearing: 0,
      pitch: 0,
      // Au doigt, on zoome a deux doigts ou par double-tap ; le glissement a
      // un doigt doit toujours deplacer la carte.
      cooperativeGestures: false,
    });
    carte.touchZoomRotate.disableRotation();
    carte.keyboard.disableRotation();
    // Poignee pour les tests de bout en bout (`tests/test_navigateur.py`) :
    // ils interrogent la carte elle-meme plutot que le texte de ce fichier.
    window.carteDvf = carte;

    carte.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    ajouterBoutonVueInitiale();
    carte.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    /* `style.load`, et surtout pas `load` : MapLibre n'emet `load` qu'une fois
     * les **tuiles** du fond effectivement chargees : un serveur de tuiles lent
     * ou injoignable laisserait la carte indefiniment vide. */
    carte.on("style.load", function () {
      try {
        poserCouches();
      } catch (erreur) {
        signalerPanne(MESSAGE_PANNE, erreur);
        return;
      }
      pret = true;
      if (enAttente) { appliquer(enAttente); enAttente = null; }
    });
    carte.on("move", surMouvement);
    carte.on("zoom", surMouvement);
    carte.on("moveend", planifierPoints);
    carte.on("moveend", ecrireAdresse);
    carte.on("zoomend", majIndice);
    // Les contours sont lus dans le worker de MapLibre, pas ici : c'est lui
    // qui dit quand il a fini, et quand il n'y arrive pas.
    carte.on("sourcedata", function (evenement) {
      if (!contoursEnVol[evenement.sourceId]) return;
      if (!carte.isSourceLoaded(evenement.sourceId)) return;
      delete contoursEnVol[evenement.sourceId];
      attendre(false);
    });
    carte.on("error", function (evenement) {
      if (!evenement.sourceId || !contoursEnVol[evenement.sourceId]) return;
      delete contoursEnVol[evenement.sourceId];
      signalerPanne(MESSAGE_PANNE, evenement.error);
    });
    // Le GPU peut retirer son contexte a la carte (onglet en arriere-plan sur
    // un telephone a court de memoire). MapLibre le restaure seul au retour ;
    // la trace sert seulement a diagnostiquer une carte restee blanche.
    carte.getCanvas().addEventListener("webglcontextlost", function (evenement) {
      if (window.console) console.warn("[carte] contexte WebGL perdu", evenement);
    });
    // Tout geste du visiteur fige la vue : on ne recadre plus sous ses doigts.
    carte.on("movestart", function (evenement) {
      if (evenement && evenement.originalEvent) vueTouchee = true;
    });
  }

  function surMouvement() {
    planifierEtiquettes();
    majMailleLue();
    suivreAncre();
    suivreTrajet();
  }

  /* `setTiles` changerait les tuiles mais garderait l'attribution du fond
   * precedent — on afficherait « OpenStreetMap » sur des tuiles IGN. La source
   * est donc refaite, et la couche remise sous toutes les autres. */
  function changerFond(fond) {
    if (carte.getLayer("fond")) carte.removeLayer("fond");
    if (carte.getSource("fond")) carte.removeSource("fond");
    carte.addSource("fond", sourceFond(fond));
    var dessous = carte.getStyle().layers[0];
    carte.addLayer({ id: "fond", type: "raster", source: "fond" }, dessous && dessous.id);
  }

  /* ---------------------------------------------------------------- Opacites
   *
   * Toute la descente continue tient dans ces quatre expressions. Elles
   * sont evaluees par le GPU a partir du seul zoom : aucun evenement, aucun
   * recalcul, aucun aller-retour.
   *
   * `interpolate` sur le zoom doit rester **au sommet** de l'expression : la
   * specification de style n'autorise `["zoom"]` que la, jamais imbrique dans
   * un `case`. C'est donc le survol qui se glisse dans les sorties.
   */

  function appuye(pleine, survolee) { return ["case", SURVOLEE, survolee, pleine]; }

  var APLAT = appuye(0.72, 0.92);
  var APLAT_EFFACE = appuye(0, 0.20);

  //: Aplat des communes : plein jusqu'au debut du fondu, eteint ensuite. En
  //: mode sections, la couche est masquee, pas eteinte — autant ne rien
  //: dessiner du tout.
  function opaciteCommunes() {
    return [
      "interpolate", ["linear"], ["zoom"],
      Z_SECTIONS_DEBUT, APLAT,
      Z_SECTIONS_PLEIN, ["case", SURVOLEE, 0.92, 0],
    ];
  }

  //: Aplat des sections : il monte quand celui des communes descend, tient le
  //: temps qu'on compare des sections, puis s'efface pour laisser voir les
  //: rues sous les ventes.
  function opaciteSections(avecCommunes) {
    var paliers = ["interpolate", ["linear"], ["zoom"]];
    if (avecCommunes) paliers = paliers.concat([Z_SECTIONS_DEBUT, ["case", SURVOLEE, 0.92, 0]]);
    return paliers.concat([
      Z_SECTIONS_PLEIN, APLAT,
      Z_EFFACEMENT_DEBUT, APLAT,
      Z_EFFACEMENT_FIN, APLAT_EFFACE,
    ]);
  }

  //: Les traits suivent leur aplat, en un peu plus tenace : un contour de
  //: section reste lisible la ou la couleur a deja disparu.
  function opaciteTraitSections(avecCommunes) {
    var paliers = ["interpolate", ["linear"], ["zoom"]];
    // En mode sections, la vue d'ensemble pose huit mille contours sur un
    // ecran : a pleine opacite, ils font un grillage noir la ou on veut lire
    // des couleurs. Le trait ne reprend son poids qu'une fois la section
    // assez grande pour qu'il la borde au lieu de la remplir.
    paliers = paliers.concat(avecCommunes ? [Z_SECTIONS_DEBUT, 0] : [8, 0.15, 10.5, 0.45]);
    return paliers.concat([
      Z_SECTIONS_PLEIN, 0.85,
      Z_EFFACEMENT_DEBUT, 0.85,
      Z_EFFACEMENT_FIN, 0.35,
    ]);
  }

  function opaciteTraitCommunes() {
    return [
      "interpolate", ["linear"], ["zoom"],
      Z_SECTIONS_DEBUT, 0.85,
      Z_SECTIONS_PLEIN, 0,
    ];
  }

  //: Limites communales rappelees par-dessus la maille cadastrale : sans
  //: elles, on ne sait plus de quelle commune releve la section regardee.
  function opaciteLimites(avecCommunes) {
    var paliers = ["interpolate", ["linear"], ["zoom"]];
    paliers = paliers.concat(avecCommunes ? [Z_SECTIONS_DEBUT, 0] : [8, 0.2, 10.5, 0.5]);
    return paliers.concat([
      Z_SECTIONS_PLEIN, 0.8,
      Z_EFFACEMENT_DEBUT, 0.8,
      Z_EFFACEMENT_FIN, 0.3,
    ]);
  }

  /* Une valeur pour le metro et le tram, une autre pour les gares lourdes.
   * `rang` est pose par Python (`geo.rang_gare`). */
  function parRang(leger, lourd) {
    return ["match", ["get", "rang"], 2, lourd, leger];
  }

  var EN_SERVICE = ["==", ["get", "statut"], "service"];
  var A_VENIR = ["!=", ["get", "statut"], "service"];
  var VIOLET_PROJET = "#7b4fa8";
  var BLEU_GARE = "#1b6ca8";
  //: Cerne des ventes. Presque noir plutot que blanc : a ce zoom, le fond est
  //: un plan de rues clair ou une orthophoto, et c'est le trait sombre qui
  //: decoupe l'icone — quelle que soit la teinte, pale ou soutenue, que lui
  //: donne l'echelle des prix.
  var CERNE_VENTE = "#1c1c1c";

  //: Dessin d'un arret, selon ce qu'on y prend. `genre` est pose par Python
  //: (`geo.genre_gare`) ; le rang, lui, ne decide que de la taille.
  var ICONE_GARE = [
    "match", ["get", "genre"],
    "metro", "ico-metro",
    "tram", "ico-tram",
    "ico-train",
  ];

  //: Dessin d'un emplacement vendu. `k` est pose par le navigateur au moment
  //: de grouper les ventes de l'emplacement (`construirePoints`) : un
  //: emplacement ou se melangent une maison et un appartement n'est ni l'un
  //: ni l'autre.
  var TYPE_MIXTE = 0;
  var TYPE_MAISON = 1;
  var TYPE_APPARTEMENT = 2;
  var ICONE_VENTE = [
    "match", ["get", "k"],
    TYPE_MAISON, "ico-maison",
    TYPE_APPARTEMENT, "ico-immeuble",
    "ico-mixte",
  ];
  //: Taille et cerne des icones de ventes (paliers de zoom).
  var TAILLE_VENTE = [Z_POINTS_DEBUT, 0.38, Z_POINTS_PLEIN, 0.58, 18, 0.76];
  var CERNE_VENTE_LARGEUR = [Z_POINTS_DEBUT, 0.6, Z_POINTS_PLEIN, 1.0, 18, 1.3];

  //: Couleur d'une zone : elle vit dans l'**etat** de l'entite, pas dans ses
  //: proprietes. Un changement de filtre ne reecrit alors ni geometrie ni
  //: source — MapLibre ne met a jour qu'un tampon de couleurs sur des
  //: contours deja decoupes. C'est la difference entre repeindre et
  //: redessiner, et a dix mille sections elle se compte en centaines de
  //: millisecondes par reglage touche.
  var COULEUR_ZONE = ["to-color", ["feature-state", "couleur"], GRIS];

  //: Taille des icones de gares, du plus loin au plus pres (paliers de zoom
  //: et tailles). Les gares lourdes se voient de plus loin et plus gros que
  //: les stations ; les gares a venir ont la taille des gares lourdes.
  var TAILLE_GARE_LEGERE = [9.5, 0.13, 11, 0.19, 12, 0.25, 13.5, 0.34, 16, 0.40];
  var TAILLE_GARE_LOURDE = [9.5, 0.22, 11, 0.32, 12, 0.42, 13.5, 0.50, 16, 0.58];
  //: Le cerne blanc detache l'icone du fond de carte. Il ne s'epaissit qu'une
  //: fois l'icone assez grande pour le porter : a quatre pixels, un cerne
  //: d'un pixel et demi mange le dessin.
  var CERNE_GARE = [10.5, 0, 12, 0.9, 14, 1.4];

  /* `interpolate` sur le zoom, a partir d'une liste de paliers. */
  function selonZoom(paliers) {
    return ["interpolate", ["linear"], ["zoom"]].concat(paliers);
  }

  /* La meme interpolation, calculee ici : la taille d'une icone a l'ecran. */
  function valeurAuZoom(paliers, zoom) {
    if (zoom <= paliers[0]) return paliers[1];
    for (var i = 2; i < paliers.length; i += 2) {
      if (zoom <= paliers[i]) {
        var part = (zoom - paliers[i - 2]) / (paliers[i] - paliers[i - 2]);
        return paliers[i - 1] + part * (paliers[i + 1] - paliers[i - 1]);
      }
    }
    return paliers[paliers.length - 1];
  }

  function tailleGare() {
    var paliers = ["interpolate", ["linear"], ["zoom"]];
    for (var i = 0; i < TAILLE_GARE_LEGERE.length; i += 2) {
      paliers.push(TAILLE_GARE_LEGERE[i], parRang(TAILLE_GARE_LEGERE[i + 1], TAILLE_GARE_LOURDE[i + 1]));
    }
    return paliers;
  }

  function haloGare() { return selonZoom(CERNE_GARE); }

  var COUCHE_MAILLE = { "zones-communes": CLE_COMMUNE, "zones-sections": CLE_SECTION };

  /* Les contours ne sont pas decoupes indefiniment : passe ce zoom, MapLibre
   * reutilise les tuiles deja taillees au lieu d'en tailler de plus fines. Le
   * cadastre ne gagne rien a etre redecoupe a chaque cran — et son aplat s'est
   * de toute facon efface bien avant. */
  var ZOOM_MAX_DECOUPE = 15;
  /* Debordement de chaque tuile, en unites de tuile. Le defaut (128) fait
   * deborder chaque polygone tres au-dela de sa tuile : a dix mille sections,
   * c'est autant de sommets decoupes et tesselles pour rien. Trente-deux
   * suffisent largement aux traits les plus epais de la carte. */
  var DEBORD_TUILE = 32;

  //: `promoteId` fait du code de la zone l'identifiant de l'entite : c'est lui
  //: qui permet de lui poser une couleur sans toucher a sa geometrie.
  function sourceContours() {
    return {
      type: "geojson", data: VIDE, promoteId: "c",
      maxzoom: ZOOM_MAX_DECOUPE, buffer: DEBORD_TUILE,
    };
  }

  function poserCouches() {
    poserIcones();
    carte.addSource("communes", sourceContours());
    carte.addSource("sections", sourceContours());
    carte.addSource("points", { type: "geojson", data: VIDE });
    carte.addSource("gares", { type: "geojson", data: VIDE });
    carte.addSource("trajet", { type: "geojson", data: VIDE });

    // Ordre : communes dessous, sections dessus. Pendant le fondu, c'est la
    // maille vers laquelle on descend qui doit se decouvrir, pas l'inverse.
    carte.addLayer({
      id: "zones-communes", type: "fill", source: "communes",
      paint: { "fill-color": COULEUR_ZONE, "fill-opacity": opaciteCommunes() },
    });
    carte.addLayer({
      id: "zones-communes-contour", type: "line", source: "communes",
      paint: { "line-color": "#5a5a5a", "line-width": 0.6, "line-opacity": opaciteTraitCommunes() },
    });
    carte.addLayer({
      id: "zones-sections", type: "fill", source: "sections",
      paint: { "fill-color": COULEUR_ZONE, "fill-opacity": opaciteSections(true) },
    });
    carte.addLayer({
      id: "zones-sections-contour", type: "line", source: "sections",
      paint: { "line-color": "#5a5a5a", "line-width": 0.6, "line-opacity": opaciteTraitSections(true) },
    });
    // Le tirete distingue au premier coup d'oeil la limite de commune du
    // trait plein d'une section.
    carte.addLayer({
      id: "limites-communes", type: "line", source: "communes",
      layout: { "line-join": "round" },
      paint: {
        "line-color": "#23313f",
        "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.9, 12, 1.5, 15, 2],
        "line-opacity": opaciteLimites(true),
        "line-dasharray": [4, 2],
      },
    });
    // Le trait vers la gare la plus proche (`tracerTrajet`) : sous les ventes
    // et les gares, qu'il ne couvre jamais, au-dessus des zones. Fin, en
    // pointille leger : il indique une direction, il ne doit pas se lire
    // comme un chemin. Un liseré blanc, a peine marque, le detache des aplats
    // les plus soutenus.
    carte.addLayer({
      id: "trajet-lisere", type: "line", source: "trajet",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#ffffff", "line-width": 3.2, "line-opacity": 0.55 },
    });
    carte.addLayer({
      id: "trajet", type: "line", source: "trajet",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": ["case", ["get", "a_venir"], VIOLET_PROJET, BLEU_GARE],
        "line-width": 1.4,
        "line-opacity": 0.8,
        "line-dasharray": [2, 2.5],
      },
    });
    // Ventes : une maison se dessine en maison, un appartement en immeuble, et
    // ce qui melange les deux en losange. La forme dit le bien, la couleur dit
    // le prix — sur la meme echelle que les sections, si bien qu'un point plus
    // rouge que sa section s'y est vendu plus cher qu'elle.
    carte.addLayer({
      id: "transactions", type: "symbol", source: "points",
      minzoom: Z_POINTS_DEBUT,
      layout: {
        "icon-image": ICONE_VENTE,
        // Assez grandes : c'est ce qu'on est venu regarder a ce zoom-la, et
        // une icone de douze pixels coloree dans le pale de l'echelle se
        // confondrait avec le batiment sous elle.
        "icon-size": selonZoom(TAILLE_VENTE),
        // Deux ventes voisines sont deux informations, pas un encombrement :
        // rien ne doit etre ecarte. Ces deux options evitent en prime a
        // MapLibre de calculer, image par image, quelles icones se
        // chevauchent — a quelques milliers de points, c'est ce calcul qui
        // couterait le plus cher.
        "icon-allow-overlap": true,
        "icon-ignore-placement": true,
      },
      paint: {
        "icon-color": GRIS,
        // Cerne **sombre** : une icone jaune pale cernee de blanc sur un plan
        // clair ne se verrait pas. Le trait sombre donne un contour franc sur
        // tous les fonds, ortho comprise, et remplit au passage les decoupes —
        // la maison garde sa porte, l'immeuble ses fenetres, en negatif.
        "icon-halo-color": CERNE_VENTE,
        // Etroit, et proportionne a l'icone : un cerne fixe engorgerait les
        // decoupes quand l'icone retrecit.
        "icon-halo-width": selonZoom(CERNE_VENTE_LARGEUR),
        "icon-halo-blur": 0,
        // Pleine opacite une fois arrivee : c'est le contraste qui compte.
        "icon-opacity": [
          "interpolate", ["linear"], ["zoom"], Z_POINTS_DEBUT, 0, Z_POINTS_PLEIN, 1,
        ],
      },
    });
    // Les gares s'effacent en vue d'ensemble, mais pas de la meme facon selon
    // ce qu'elles desservent : ce sont les stations de metro qui satureraient
    // la carte, pas les gares RER, dont on veut au contraire pouvoir se reperer.
    // Une gare a venir porte le dessin de ce qu'elle sera — un « M » pour une
    // station du Grand Paris Express — mais dans la couleur des projets : elle
    // ne dessert encore personne, et se paie pourtant deja dans les prix.
    carte.addLayer({
      id: "gares-projet", type: "symbol", source: "gares",
      minzoom: 9.5,
      filter: A_VENIR,
      layout: {
        "icon-image": ICONE_GARE,
        // Les projets ne se disputent pas la place entre eux : ils sont trop
        // peu nombreux pour se masquer, et en cacher un serait le pire des
        // services a rendre a qui lit la carte pour cela.
        "icon-size": selonZoom(TAILLE_GARE_LOURDE),
        "icon-allow-overlap": true,
        "icon-ignore-placement": true,
      },
      paint: {
        "icon-color": VIOLET_PROJET,
        "icon-halo-color": "#ffffff",
        "icon-halo-width": haloGare(),
        "icon-opacity": ["interpolate", ["linear"], ["zoom"], 9.5, 0, 11, 0.6, 12, 0.9],
      },
    });
    carte.addLayer({
      id: "gares", type: "symbol", source: "gares",
      minzoom: 9.5,
      filter: EN_SERVICE,
      layout: {
        "icon-image": ICONE_GARE,
        "icon-size": tailleGare(),
        "icon-allow-overlap": true,
        "icon-ignore-placement": true,
      },
      paint: {
        "icon-color": BLEU_GARE,
        "icon-halo-color": "#ffffff",
        "icon-halo-width": haloGare(),
        "icon-opacity": [
          "interpolate", ["linear"], ["zoom"],
          9.5, 0,
          11, parRang(0.15, 0.55),
          12, parRang(0.30, 0.85),
          13.5, parRang(0.85, 0.95),
        ],
      },
    });

    if (SURVOL_POSSIBLE) carte.on("mousemove", planifierSurvol);
    carte.on("mouseout", quitterSurvol);
    carte.on("click", surClic);
    // Un glissement ferme l'infobulle : elle commenterait une position qu'on
    // a quittee, et sur telephone elle masque le bas de la carte.
    carte.on("dragstart", fermerInfobulle);
    // Un double-clic zoome — il ne demande pas les chiffres de ce qu'il
    // survole. MapLibre emet pourtant un `click` avant chaque `dblclick` :
    // sans cela, zoomer ouvrirait une infobulle a chaque cran.
    carte.on("dblclick", function () {
      clearTimeout(minuteurZone);
      fermerInfobulle();
    });
  }

  /* Couches interrogeables au pointeur, par ordre de priorite : une vente
   * l'emporte sur la zone qui la porte. Les gares passent avant les deux,
   * par leur propre requete (`gareDesignee`). */
  function couchesPointables() {
    var liste = [];
    if (carte.getZoom() >= Z_POINTS_POINTABLES) liste.push("transactions");
    liste.push(mailleLue === CLE_SECTION ? "zones-sections" : "zones-communes");
    return liste.filter(function (couche) { return carte.getLayer(couche); });
  }

  /* Les gares se designent a part, et a deux conditions : etre descendu assez
   * pour qu'on les cherche, et les toucher franchement. C'est une requete de
   * plus, mais sur deux couches de quelques centaines de symboles — sans
   * commune mesure avec celle qui interroge huit mille sections, et le prix a
   * payer pour que l'infobulle d'un secteur dense reponde enfin au doigt. */
  function gareDesignee(point, auDoigt) {
    var zoom = carte.getZoom();
    if (zoom < Z_GARES_LOURDES_POINTABLES) return null;
    var couches = ["gares", "gares-projet"].filter(function (couche) {
      return carte.getLayer(couche);
    });
    if (!couches.length) return null;
    var marge = auDoigt ? MARGE_GARE_DOIGT : MARGE_GARE_SOURIS;
    var trouvees = carte.queryRenderedFeatures(
      [[point.x - marge, point.y - marge], [point.x + marge, point.y + marge]],
      { layers: couches }
    );
    // Entre les deux planchers, seules les gares lourdes et celles a venir
    // repondent : les stations de metro restent des reperes.
    if (zoom < Z_GARES_POINTABLES) {
      trouvees = trouvees.filter(function (entite) {
        return entite.layer.id === "gares-projet" || entite.properties.rang === 2;
      });
    }
    // Deux stations voisines peuvent tomber dans la marge : c'est la plus
    // proche du pointeur qui repond, pas la premiere que MapLibre enumere.
    var meilleure = null, distance = Infinity;
    for (var i = 0; i < trouvees.length; i++) {
      var ecran = carte.project(trouvees[i].geometry.coordinates);
      var d = (ecran.x - point.x) * (ecran.x - point.x) + (ecran.y - point.y) * (ecran.y - point.y);
      if (d < distance) { distance = d; meilleure = trouvees[i]; }
    }
    return meilleure ? { couche: meilleure.layer.id, entite: meilleure } : null;
  }

  function premiereEntite(point, marge, auDoigt) {
    // La gare d'abord : elle est dessinee **au-dessus** des ventes, et c'est
    // ce qu'on voit sous le pointeur qui doit repondre, meme entouree de
    // dizaines de ventes.
    var gare = gareDesignee(point, auDoigt);
    if (gare) return gare;
    var couches = couchesPointables();
    if (!couches.length) return null;
    // Une seule interrogation pour toutes ces couches : a dix mille sections,
    // en faire deux d'affilee se sent au deplacement de la souris. MapLibre
    // rend les resultats dans l'ordre de dessin, qui n'est pas celui de la
    // priorite ; c'est donc le rang dans `couches` qui tranche.
    var trouvees = carte.queryRenderedFeatures(
      [[point.x - marge, point.y - marge], [point.x + marge, point.y + marge]],
      { layers: couches }
    );
    var meilleure = null, rang = couches.length;
    for (var i = 0; i < trouvees.length; i++) {
      var position = couches.indexOf(trouvees[i].layer.id);
      if (position >= 0 && position < rang) { rang = position; meilleure = trouvees[i]; }
    }
    return meilleure ? { couche: couches[rang], entite: meilleure } : null;
  }

  // ------------------------------------------------------------- Infobulles

  function fermerInfobulle() {
    var boite = document.getElementById("infobulle");
    boite.hidden = true;
    boite.classList.remove("ancree", "dessous");
    infobulleFixee = false;
    infobulleCle = null;
    infobulleAncre = null;
    effacerSurvol();
    effacerTrajet();
  }

  function effacerSurvol() {
    if (!survol) return;
    carte.setFeatureState({ source: survol.source, id: survol.id }, { survol: false });
    survol = null;
  }

  function marquerSurvol(source, id) {
    if (survol && survol.source === source && survol.id === id) return;
    effacerSurvol();
    survol = { source: source, id: id };
    carte.setFeatureState({ source: source, id: id }, { survol: true });
  }

  /* L'infobulle suit le pointeur sur grand ecran, et se pose au bas de
   * l'ecran sur telephone (c'est la feuille de style qui en decide) : un doigt
   * masque exactement ce qu'il designe. */
  function positionnerInfobulle(point) {
    var boite = document.getElementById("infobulle");
    var large = boite.offsetWidth, haut = boite.offsetHeight;
    var x = point.x + 14, y = point.y + 14;
    if (x + large > window.innerWidth) x = point.x - large - 14;
    if (y + haut > window.innerHeight) y = point.y - haut - 14;
    boite.style.left = Math.max(0, x) + "px";
    boite.style.top = Math.max(0, y) + "px";
  }

  function placerInfobulle(point, html, fixee, ancre) {
    var boite = document.getElementById("infobulle");
    boite.innerHTML =
      '<button type="button" class="fermer" aria-label="Fermer">×</button>' + html
      + '<div class="gare-proche" hidden></div>';
    boite.hidden = false;
    boite.classList.toggle("fixee", !!fixee);
    infobulleFixee = !!fixee;
    // Une ancre, c'est une position sur la carte, pas sur l'ecran : la bulle
    // la suit quand la carte bouge (`suivreAncre`).
    infobulleAncre = ancre || null;
    boite.classList.toggle("ancree", !!infobulleAncre);
    if (infobulleAncre) ancrerInfobulle();
    else positionnerInfobulle(point);
  }

  /* Sur telephone, l'infobulle d'une zone ou d'une vente est un bandeau au
   * bas de l'ecran — elle a beaucoup a dire, et le doigt masque ce qu'il
   * designe. Celle d'une gare tient en deux lignes : elle se pose **juste
   * au-dessus de la gare**, pointe vers elle, et suit la carte : on sait a
   * laquelle des stations voisines elle se rapporte. */
  var infobulleAncre = null;   // [longitude, latitude] de la gare commentee
  var ECART_ANCRE = 16;        // pixels entre la gare et la pointe de la bulle
  var MARGE_ECRAN = 8;

  function ancrerInfobulle() {
    var boite = document.getElementById("infobulle");
    if (!infobulleAncre || boite.hidden) return;
    var point = carte.project(infobulleAncre);
    var large = boite.offsetWidth, haut = boite.offsetHeight;
    var largeur = window.innerWidth, hauteur = window.innerHeight;
    var x = Math.min(Math.max(MARGE_ECRAN, point.x - large / 2), largeur - large - MARGE_ECRAN);
    var y = point.y - ECART_ANCRE - haut;
    // Pas la place au-dessus (gare au ras du haut de la carte) : dessous.
    var dessous = y < MARGE_ECRAN;
    if (dessous) y = Math.min(point.y + ECART_ANCRE, hauteur - haut - MARGE_ECRAN);
    boite.classList.toggle("dessous", dessous);
    boite.style.left = Math.round(x) + "px";
    boite.style.top = Math.round(y) + "px";
    // La pointe reste sur la gare meme quand la bulle bute contre un bord.
    var pointe = Math.min(Math.max(14, point.x - x), large - 14);
    boite.style.setProperty("--pointe", Math.round(pointe) + "px");
  }

  function suivreAncre() {
    if (infobulleAncre) ancrerInfobulle();
  }

  /* De quoi reconnaitre l'entite deja commentee. Tant que le pointeur reste
   * sur la meme, l'infobulle n'est que **deplacee** : la reecrire a chaque
   * image reconstruirait son contenu des dizaines de fois par seconde, et un
   * emplacement peut porter quelques centaines de ventes depuis qu'elles n'y
   * sont plus plafonnees. */
  function cleEntite(trouvee) {
    var proprietes = trouvee.entite.properties || {};
    if (trouvee.couche === "transactions") return "v:" + proprietes.t + "/" + proprietes.e;
    if (trouvee.couche === "gares" || trouvee.couche === "gares-projet") {
      return "g:" + proprietes.nom + "/" + proprietes.statut;
    }
    return trouvee.couche + ":" + proprietes.c;
  }

  document.getElementById("infobulle").addEventListener("click", function (evenement) {
    if (evenement.target.closest(".fermer")) fermerInfobulle();
  });

  function contenuEntite(couche, entite) {
    if (couche === "transactions") return infobulleEmplacement(entite.properties);
    if (couche === "gares" || couche === "gares-projet") return infobulleGare(entite.properties);
    return infobulleZone(COUCHE_MAILLE[couche], entite.properties);
  }

  /* Le survol est resolu au plus une fois par image. Interroger la carte a
   * chaque `mousemove` — il en arrive plus de cent par seconde sur une souris
   * rapide — couterait plus cher, a dix mille sections, que tout le rendu. */
  var rafSurvol = 0;
  var dernierPointeur = null;

  function planifierSurvol(evenement) {
    dernierPointeur = evenement.point;
    if (rafSurvol) return;
    rafSurvol = requestAnimationFrame(function () {
      rafSurvol = 0;
      if (dernierPointeur) surSurvol(dernierPointeur);
    });
  }

  function surSurvol(point) {
    if (infobulleFixee) return;
    var trouvee = premiereEntite(point, MARGE_CLIC_SOURIS, false);
    var boite = document.getElementById("infobulle");
    var gares = tracerTrajet(origineTrajet(trouvee, point));
    if (!trouvee) {
      boite.hidden = true;
      infobulleCle = null;
      carte.getCanvas().style.cursor = "";
      effacerSurvol();
      return;
    }
    carte.getCanvas().style.cursor = "pointer";
    var maille = COUCHE_MAILLE[trouvee.couche];
    if (maille) marquerSurvol(maille === CLE_SECTION ? "sections" : "communes", trouvee.entite.properties.c);
    else effacerSurvol();
    var cle = cleEntite(trouvee);
    if (!boite.hidden && cle === infobulleCle) {
      ecrireGares(gares);
      positionnerInfobulle(point);
      return;
    }
    infobulleCle = cle;
    placerInfobulle(point, contenuEntite(trouvee.couche, trouvee.entite), false);
    ecrireGares(gares);
    positionnerInfobulle(point);
  }

  function quitterSurvol() {
    if (infobulleFixee) return;
    document.getElementById("infobulle").hidden = true;
    infobulleCle = null;
    carte.getCanvas().style.cursor = "";
    effacerSurvol();
    effacerTrajet();
  }

  /* Le clic ne cadre pas, ne selectionne pas, ne recalcule rien : il
   * ouvre l'infobulle, et elle reste ouverte jusqu'a ce qu'on la ferme. C'est
   * le seul geste disponible au doigt, ou le survol n'existe pas — et sur
   * grand ecran, c'est ce qui permet de lire des chiffres sans garder la
   * souris parfaitement immobile. */
  function surClic(evenement) {
    var marge = SURVOL_POSSIBLE ? MARGE_CLIC_SOURIS : MARGE_CLIC_DOIGT;
    var trouvee = premiereEntite(evenement.point, marge, !SURVOL_POSSIBLE);
    if (!trouvee) { fermerInfobulle(); return; }
    var maille = COUCHE_MAILLE[trouvee.couche];
    if (maille && !PETIT_ECRAN.matches) {
      fermerInfobulle();
      zoomerSurZone(maille, trouvee.entite);
      return;
    }
    if (maille) marquerSurvol(maille === CLE_SECTION ? "sections" : "communes", trouvee.entite.properties.c);
    else effacerSurvol();
    infobulleCle = cleEntite(trouvee);
    var gare = trouvee.couche === "gares" || trouvee.couche === "gares-projet";
    var ancre = gare && PETIT_ECRAN.matches ? trouvee.entite.geometry.coordinates.slice() : null;
    var gares = tracerTrajet(origineTrajet(trouvee, evenement.point));
    placerInfobulle(evenement.point, contenuEntite(trouvee.couche, trouvee.entite), true, ancre);
    ecrireGares(gares);
    if (!ancre) positionnerInfobulle(evenement.point);
    degagerDuBandeau();
    suivreTrajet();
  }

  // --------------------------------------------------- Zoom sur une zone
  //
  // Sur grand ecran, cliquer une commune ou une section y descend, comme
  // « Aller à » : la zone entiere a l'ecran, avec une marge. On ne recule
  // jamais — cliquer une grande zone qu'on regarde deja de pres ne fait que
  // la centrer —, et on s'arrete au zoom ou chaque vente se lit.

  var ZOOM_MAX_ZONE = 16;
  var MARGE_ZONE = 48;
  //: MapLibre emet un `click` avant chaque `dblclick`. Le zoom attend ce
  //: delai : un double-clic l'annule, et garde son propre zoom d'un cran.
  var DELAI_DOUBLE_CLIC = 260;
  var minuteurZone = 0;

  /* Emprise d'une zone : ses morceaux dans les tuiles deja decoupees, et
   * l'entite touchee. Une zone plus grande que ce qu'on voit n'est connue
   * que de ses morceaux proches — le zoom la cadre alors par la, sans
   * reculer. */
  function bornesZone(nomSource, code, entite) {
    var bornes = new maplibregl.LngLatBounds();
    var morceaux = carte.querySourceFeatures(nomSource, { filter: ["==", ["get", "c"], code] });
    morceaux.concat([entite]).forEach(function (morceau) {
      var geometrie = morceau.geometry || {};
      var polygones = geometrie.type === "MultiPolygon" ? geometrie.coordinates
        : geometrie.type === "Polygon" ? [geometrie.coordinates] : [];
      polygones.forEach(function (polygone) {
        (polygone[0] || []).forEach(function (point) { bornes.extend(point); });
      });
    });
    return bornes.isEmpty() ? null : bornes;
  }

  function zoomerSurZone(maille, entite) {
    var nomSource = maille === CLE_SECTION ? "sections" : "communes";
    var code = entite.properties.c;
    clearTimeout(minuteurZone);
    minuteurZone = setTimeout(function () {
      var bornes = bornesZone(nomSource, code, entite);
      var camera = bornes && carte.cameraForBounds(bornes, { padding: MARGE_ZONE });
      if (!camera) return;
      vueTouchee = true;
      carte.flyTo({
        center: camera.center,
        zoom: Math.min(Math.max(camera.zoom, carte.getZoom()), ZOOM_MAX_ZONE),
        essential: true,
      });
    }, DELAI_DOUBLE_CLIC);
  }

  function infobulleZone(maille, proprietes) {
    var couche = (args.couches || {})[maille];
    var indice = (rangs[maille] || {})[proprietes.c];
    if (!couche || indice === undefined || indice === null) {
      return '<div class="entete">Aucune vente avec les filtres actifs</div>';
    }
    var lignes = ['<div class="entete">' + echapper(couche.entetes[indice]) + "</div>"];
    // Le cadastre ne nomme pas ses sections : le sous-titre donne les voies ou
    // se concentrent ses ventes, seul repere parlant disponible partout.
    var repere = couche.reperes ? couche.reperes[indice] : null;
    if (repere) lignes.push('<div class="repere">' + echapper(repere) + "</div>");
    var note = couche.notes ? couche.notes[indice] : null;
    if (note) lignes.push("<div><i>" + echapper(note) + "</i></div>");
    var valeurs = couche.valeurs[indice];
    for (var i = 0; i < couche.champs.length; i++) {
      var champ = couche.champs[i];
      lignes.push("<div><b>" + echapper(champ[0]) + "</b> " + echapper(formater(valeurs[i], champ[1])) + "</div>");
    }
    return lignes.join("");
  }

  /* Les lignes desservies disent mieux que le reseau ce qu'on a devant soi :
   * « RER B, Métro 4 » plutot que « RATP ». */
  var LIBELLES_STATUT = { travaux: "en travaux", projet: "en projet" };
  //: Le meme dessin que sur la carte et dans la legende : l'infobulle
  //: rappelle ce qu'on vient de toucher.
  function infobulleGare(proprietes) {
    var statut = LIBELLES_STATUT[proprietes.statut];
    var desserte = proprietes.lignes || proprietes.reseau;
    var genre = TRACES[proprietes.genre] ? proprietes.genre : "train";
    var marque = symboleLegende(genre, statut ? VIOLET_PROJET : BLEU_GARE);
    return '<div class="entete">' + marque + echapper(proprietes.nom) + "</div>"
      + (desserte ? "<div>" + echapper(desserte) + "</div>" : "")
      + (statut ? '<div class="projet">Gare ' + statut + "</div>" : "")
      + (proprietes.a_venir ? '<div class="projet">À venir : ' + echapper(proprietes.a_venir) + "</div>" : "");
  }

  // ------------------------------------------------------- Distance a pied
  //
  // A quelle distance de la gare ? La carte le dit en minutes, et seulement
  // la ou l'on regarde : aucun cercle, aucune couche de plus a lire.
  //
  // Des qu'on regarde un quartier, le pointeur — ou la vente qu'il designe —
  // est relie a la gare en service la plus proche, en pointille, avec le
  // temps de marche. Si une gare a venir est plus proche encore, un second
  // trait, violet, y mene : c'est elle qui change le quartier. Au doigt, un
  // appui suffit. Au-dela d'une demi-heure de marche, rien n'est trace.
  //
  // Le calcul se fait ici, sur les gares deja chargees : quelques centaines
  // de distances par image, rien a telecharger.

  //: Une minute de marche, a vol d'oiseau : 4,8 km/h, soit 80 m par minute le
  //: long des rues, qui font en moyenne 1,25 fois la ligne droite.
  var METRES_PAR_MINUTE = 64;
  //: Zoom a partir duquel le trait suit le pointeur : on regarde un quartier,
  //: plus une agglomeration.
  var Z_TRAJET = 13;
  //: En deca, le pointeur est sur la gare : pas de trait.
  var TRAJET_MIN_METRES = 30;
  //: Au-dela d'une demi-heure de marche, on ne va plus a la gare a pied : ni
  //: trait ni temps, et l'infobulle le dit.
  var MINUTES_MAX = 30;
  var METRES_PAR_DEGRE = 111195;

  var trajet = null;          // { origine, icone, gares, traits, segments, zoom }

  function metresEntre(a, b) {
    var y = (a[1] - b[1]) * METRES_PAR_DEGRE;
    var x = (a[0] - b[0]) * METRES_PAR_DEGRE * Math.cos((a[1] + b[1]) / 360 * Math.PI);
    return Math.sqrt(x * x + y * y);
  }

  function minutesAPied(metres) {
    return Math.max(1, Math.ceil(metres / METRES_PAR_MINUTE));
  }

  /* La gare la plus proche d'une position, parmi celles que retient `garder`
   * (ses proprietes, posees par `charge.points_gares`). */
  function gareLaPlusProche(position, garder, projet) {
    var entites = (args && args.gares && args.gares.features) || [];
    var meilleure = null, distance = Infinity;
    for (var i = 0; i < entites.length; i++) {
      var p = entites[i].properties || {};
      if (!garder(p)) continue;
      var d = metresEntre(position, entites[i].geometry.coordinates);
      if (d < distance) { distance = d; meilleure = entites[i]; }
    }
    if (!meilleure || minutesAPied(distance) > MINUTES_MAX) return null;
    return { gare: meilleure, metres: distance, minutes: minutesAPied(distance), a_venir: projet };
  }

  var ICONE_DU_TYPE = {};
  ICONE_DU_TYPE[TYPE_MAISON] = "maison";
  ICONE_DU_TYPE[TYPE_APPARTEMENT] = "immeuble";
  ICONE_DU_TYPE[TYPE_MIXTE] = "mixte";

  /* D'ou part le trait : de la vente designee — et de son icone —, sinon du
   * pointeur. Une gare designee n'en demande pas. */
  function origineTrajet(trouvee, point) {
    if (trouvee && (trouvee.couche === "gares" || trouvee.couche === "gares-projet")) return null;
    if (trouvee && trouvee.couche === "transactions") {
      return {
        position: trouvee.entite.geometry.coordinates.slice(),
        icone: ICONE_DU_TYPE[trouvee.entite.properties.k] || "mixte",
      };
    }
    var lieu = carte.unproject(point);
    return { position: [lieu.lng, lieu.lat], icone: null };
  }

  function enService(p) { return p.statut === "service"; }
  function trainOuRer(p) { return p.statut === "service" && p.rang === 2; }
  //: A venir : un chantier, un projet, ou un pole desservi ou une ligne est
  //: annoncee.
  function aVenir(p) { return p.statut !== "service" || !!p.a_venir; }

  /* Pose le trait vers la gare la plus proche — et vers la gare a venir si
   * elle l'est plus encore — et rend les gares a citer dans l'infobulle :
   * les memes, plus la gare de train ou de RER la plus proche quand la plus
   * proche n'est qu'un metro ou un tram. Rien en deca du zoom des quartiers
   * (`null`), et une liste vide quand aucune gare n'est a moins d'une
   * demi-heure de marche. */
  function tracerTrajet(depart) {
    if (!depart || carte.getZoom() < Z_TRAJET) { effacerTrajet(); return null; }
    var origine = depart.position;
    var service = gareLaPlusProche(origine, enService, false);
    var projet = gareLaPlusProche(origine, aVenir, true);
    var lourde = service && service.gare.properties.rang !== 2
      ? gareLaPlusProche(origine, trainOuRer, false) : null;
    var retenues = [];
    if (service) retenues.push(service);
    if (projet && (!service || projet.metres < service.metres)) retenues.push(projet);
    if (!retenues.length) { effacerTrajet(); return []; }
    var traits = retenues.filter(function (r) { return r.metres >= TRAJET_MIN_METRES; });
    var citees = retenues.concat(lourde ? [lourde] : []);
    trajet = {
      origine: origine, icone: depart.icone, gares: citees, traits: traits, zoom: null, segments: [],
    };
    poserTraits();
    poserPastilles();
    return citees;
  }

  /* Silhouettes des icones, dans la boite de 24 x 24 des `TRACES`, pour
   * savoir ou le trait en touche le bord. Le metro est un disque. */
  var SILHOUETTES = {
    maison: [[12, 1.6], [23, 11.2], [19.6, 11.2], [19.6, 22.4], [4.4, 22.4], [4.4, 11.2], [1, 11.2]],
    immeuble: [[4, 1.8], [20, 1.8], [20, 22.4], [4, 22.4]],
    mixte: [[12, 1.4], [22.6, 12], [12, 22.6], [1.4, 12]],
    train: [[3, 2], [21, 2], [21, 18], [22.4, 22.4], [2.2, 22.4], [3, 18]],
    tram: [[2, 2], [22, 2], [22, 22], [2, 22]],
    metro: (function () {
      var cercle = [];
      for (var i = 0; i < 32; i++) {
        cercle.push([12 + 10.5 * Math.cos(i * Math.PI / 16), 12 + 10.5 * Math.sin(i * Math.PI / 16)]);
      }
      return cercle;
    })(),
  };

  /* Distance du centre de l'icone a son bord, dans la direction `(ux, uy)`,
   * en unites de la boite de 24 : la plus lointaine traversee d'un cote. */
  function bordSilhouette(nom, ux, uy) {
    var contour = SILHOUETTES[nom] || SILHOUETTES.mixte;
    var loin = 0;
    for (var i = 0; i < contour.length; i++) {
      var p = contour[i], q = contour[(i + 1) % contour.length];
      var ex = q[0] - p[0], ey = q[1] - p[1];
      var d = ux * ey - uy * ex;
      if (Math.abs(d) < 1e-9) continue;
      var wx = p[0] - 12, wy = p[1] - 12;
      var t = (wx * ey - wy * ex) / d;
      var s = (wx * uy - wy * ux) / d;
      if (t > 0 && s >= 0 && s <= 1 && t > loin) loin = t;
    }
    return loin;
  }

  //: Une unite de la boite de 24, en pixels, pour une icone de taille 1 : le
  //: dessin occupe 32 pixels de l'image (`ICONE_DESSIN`).
  var PIXELS_PAR_UNITE = ICONE_DESSIN / 24;

  /* Ou le trait touche l'icone, en pixels depuis son centre : le bord du
   * dessin, plus son cerne. Il la touche sans jamais la couvrir. */
  function ecartIcone(nom, taille, cerne, ux, uy) {
    return bordSilhouette(nom, ux, uy) * PIXELS_PAR_UNITE * taille + cerne;
  }

  function ecartGare(gare, zoom, ux, uy) {
    var p = gare.properties || {};
    var paliers = p.statut !== "service" || p.rang === 2 ? TAILLE_GARE_LOURDE : TAILLE_GARE_LEGERE;
    return ecartIcone(TRACES[p.genre] ? p.genre : "train", valeurAuZoom(paliers, zoom),
      valeurAuZoom(CERNE_GARE, zoom), ux, uy);
  }

  /* Les traits, du bord de l'icone du logement a celui de la gare. Les
   * ecarts se comptent en pixels : ils sont recalcules quand le zoom change,
   * pas quand la carte glisse. */
  function poserTraits() {
    var zoom = carte.getZoom();
    trajet.zoom = zoom;
    trajet.ecarts = [];
    trajet.segments = trajet.traits.map(function (trait) {
      var a = carte.project(trajet.origine), b = carte.project(trait.gare.geometry.coordinates);
      var dx = b.x - a.x, dy = b.y - a.y, longueur = Math.sqrt(dx * dx + dy * dy);
      if (!longueur) return null;
      var ux = dx / longueur, uy = dy / longueur;
      var depart = trajet.icone ? ecartIcone(trajet.icone, valeurAuZoom(TAILLE_VENTE, zoom),
        valeurAuZoom(CERNE_VENTE_LARGEUR, zoom), ux, uy) : 0;
      var arrivee = ecartGare(trait.gare, zoom, -ux, -uy);
      trajet.ecarts.push(depart);
      if (longueur <= depart + arrivee + 4) return null;
      var debut = carte.unproject([a.x + ux * depart, a.y + uy * depart]);
      var fin = carte.unproject([b.x - ux * arrivee, b.y - uy * arrivee]);
      return [[debut.lng, debut.lat], [fin.lng, fin.lat]];
    });
    carte.getSource("trajet").setData({
      type: "FeatureCollection",
      features: trajet.traits.map(function (trait, i) {
        return trajet.segments[i] && {
          type: "Feature", properties: { a_venir: trait.a_venir },
          geometry: { type: "LineString", coordinates: trajet.segments[i] },
        };
      }).filter(Boolean),
    });
  }

  function effacerTrajet() {
    if (!trajet) return;
    trajet = null;
    var source = carte && carte.getSource("trajet");
    if (source) source.setData(VIDE);
    poserPastilles();
  }

  /* Le temps de marche, au milieu de chaque trait. Des `div`, comme les noms
   * de communes : MapLibre n'ecrit pas de texte sans serveur de polices. */
  function poserPastilles() {
    var conteneur = document.getElementById("trajets");
    var traits = trajet ? trajet.traits : [];
    while (conteneur.children.length < traits.length) {
      var pastille = document.createElement("div");
      pastille.className = "pastille-trajet";
      conteneur.appendChild(pastille);
    }
    for (var i = 0; i < conteneur.children.length; i++) {
      var boite = conteneur.children[i];
      boite.hidden = i >= traits.length;
      if (boite.hidden) continue;
      boite.textContent = traits[i].minutes + " min";
      boite.classList.toggle("a-venir", traits[i].a_venir);
    }
    suivreTrajet();
  }

  //: Marge, en pixels, entre une pastille et le bord de la carte.
  var MARGE_PASTILLE = 24;
  //: Distance maximale, en pixels, entre la pastille et le depart du trait.
  var PASTILLE_MAX_PX = 90;

  /* Sur telephone, l'infobulle d'une vente est un bandeau au bas de l'ecran :
   * la carte visible s'arrete a son bord superieur. */
  function bandeauOuvert() {
    var boite = document.getElementById("infobulle");
    return PETIT_ECRAN.matches && !boite.hidden && !boite.classList.contains("ancree");
  }

  function hautVisible() {
    var hauteur = carte.getCanvas().clientHeight;
    if (!bandeauOuvert()) return hauteur;
    var haut = document.getElementById("infobulle").getBoundingClientRect().top
      - carte.getContainer().getBoundingClientRect().top;
    return Math.max(0, Math.min(hauteur, haut));
  }

  //: Marge minimale entre le logement touche et le bandeau, en pixels.
  var MARGE_BANDEAU = 56;

  /* Le logement touche ne doit pas disparaitre sous le bandeau qu'il ouvre :
   * s'il y tomberait, la carte glisse juste assez pour le garder au-dessus —
   * sans changer de zoom —, et le trait vers la gare avec lui. */
  function degagerDuBandeau() {
    if (!trajet || !bandeauOuvert()) return;
    var visible = hautVisible();
    var y = carte.project(trajet.origine).y;
    if (y <= visible - MARGE_BANDEAU) return;
    carte.panBy([0, y - visible * 0.55], { duration: 300 });
  }

  /* La pastille se pose sur la partie **visible** du trait, pres du
   * logement : quand la gare est hors de l'ecran, elle reste lisible, et le
   * trait dit dans quelle direction marcher. */
  function suivreTrajet() {
    if (!trajet) return;
    if (carte.getZoom() < Z_TRAJET) { effacerTrajet(); return; }
    if (carte.getZoom() !== trajet.zoom) poserTraits();
    var conteneur = document.getElementById("trajets");
    var canevas = carte.getCanvas();
    var cadre = [MARGE_PASTILLE, MARGE_PASTILLE,
      canevas.clientWidth - MARGE_PASTILLE, hautVisible() - MARGE_PASTILLE];
    for (var i = 0; i < trajet.traits.length; i++) {
      var segment = trajet.segments[i];
      var visible = segment && decouper(carte.project(segment[0]), carte.project(segment[1]), cadre);
      var boite = conteneur.children[i];
      boite.hidden = !visible;
      if (!visible) continue;
      // Au milieu de la partie visible, sans s'eloigner du logement : c'est
      // pres de lui qu'on regarde, loin des reglages poses en haut de l'ecran.
      var vx = visible[2] - visible[0], vy = visible[3] - visible[1];
      var longueur = Math.sqrt(vx * vx + vy * vy);
      var t = longueur ? Math.min(longueur / 2, PASTILLE_MAX_PX) / longueur : 0;
      boite.style.transform = "translate(" + Math.round(visible[0] + vx * t) + "px,"
        + Math.round(visible[1] + vy * t) + "px) translate(-50%, -50%)";
    }
  }

  /* Le segment `a`-`b` coupe au rectangle `[x0, y0, x1, y1]` (Liang-Barsky) :
   * `[xa, ya, xb, yb]`, ou `null` s'il est tout entier dehors. */
  function decouper(a, b, cadre) {
    var dx = b.x - a.x, dy = b.y - a.y, t0 = 0, t1 = 1;
    var bords = [[-dx, a.x - cadre[0]], [dx, cadre[2] - a.x], [-dy, a.y - cadre[1]], [dy, cadre[3] - a.y]];
    for (var i = 0; i < 4; i++) {
      var p = bords[i][0], q = bords[i][1];
      if (p === 0) { if (q < 0) return null; continue; }
      var t = q / p;
      if (p < 0) { if (t > t1) return null; if (t > t0) t0 = t; }
      else { if (t < t0) return null; if (t < t1) t1 = t; }
    }
    return [a.x + t0 * dx, a.y + t0 * dy, a.x + t1 * dx, a.y + t1 * dy];
  }

  /* Dans l'infobulle : la gare, ses lignes et le temps de marche. */
  function ecrireGares(gares) {
    var zone = document.querySelector("#infobulle .gare-proche");
    if (!zone) return;
    zone.hidden = !gares;
    if (!gares) { zone.innerHTML = ""; return; }
    if (!gares.length) {
      zone.innerHTML = '<div class="loin">Aucune gare à moins de ' + MINUTES_MAX + " min à pied</div>";
      return;
    }
    zone.innerHTML = gares.map(function (r) {
      var p = r.gare.properties || {};
      var genre = TRACES[p.genre] ? p.genre : "train";
      var lignes = r.a_venir ? (p.statut === "service" ? p.a_venir : p.lignes) : p.lignes;
      return '<div class="' + (r.a_venir ? "projet" : "") + '">'
        + symboleLegende(genre, r.a_venir ? VIOLET_PROJET : BLEU_GARE)
        + '<span class="nom">' + echapper(p.nom || "Gare") + "</span>"
        + (lignes ? " (" + echapper(lignes) + ")" : "")
        + " · ≈ " + r.minutes + " min à pied" + (r.a_venir ? ", à venir" : "") + "</div>";
    }).join("");
  }

  // ------------------------------------------------------- Noms de communes
  //
  // MapLibre sait ecrire du texte, mais uniquement a partir de glyphes servis
  // par un serveur de polices : une dependance reseau de plus, et un point de
  // panne hors ligne. Les noms de communes sont donc de simples `div`.
  //
  // Le cout est borne par construction : les `div` sont crees une fois, leur
  // largeur mesuree une fois, et chaque frame ne fait que les deplacer ou les
  // masquer.

  function preparerEtiquettes(liste) {
    var conteneur = document.getElementById("etiquettes");
    conteneur.textContent = "";
    etiquettes = (liste || []).map(function (donnee) {
      var boite = document.createElement("div");
      boite.className = "etiquette";
      boite.textContent = donnee.n;
      conteneur.appendChild(boite);
      return { lon: donnee.x, lat: donnee.y, poids: donnee.p || 0, boite: boite, large: 0, visible: false };
    });
    // Les plus peuplees d'abord : ce sont elles qu'on s'attend a lire en
    // premier quand la place manque.
    etiquettes.sort(function (a, b) { return b.poids - a.poids; });
    // Une seule lecture de mise en page pour toutes les etiquettes : mesurer
    // au fil de l'eau declencherait un reflow par commune.
    for (var i = 0; i < etiquettes.length; i++) {
      etiquettes[i].large = etiquettes[i].boite.offsetWidth;
      etiquettes[i].haut = etiquettes[i].boite.offsetHeight;
      etiquettes[i].boite.style.display = "none";
    }
  }

  function planifierEtiquettes() {
    if (rafEtiquettes) return;
    rafEtiquettes = requestAnimationFrame(function () {
      rafEtiquettes = 0;
      majEtiquettes();
    });
  }

  function plafondEtiquettes(zoom) {
    var paliers = PLAFOND_ETIQUETTES;
    if (zoom <= paliers[0][0]) return paliers[0][1];
    for (var i = 1; i < paliers.length; i++) {
      if (zoom <= paliers[i][0]) {
        var part = (zoom - paliers[i - 1][0]) / (paliers[i][0] - paliers[i - 1][0]);
        return Math.round(paliers[i - 1][1] + part * (paliers[i][1] - paliers[i - 1][1]));
      }
    }
    return paliers[paliers.length - 1][1];
  }

  function majEtiquettes() {
    if (!etiquettes.length) return;
    var zoom = carte.getZoom();
    var montrer = zoom >= ZOOM_MIN_ETIQUETTES;
    var plafond = plafondEtiquettes(zoom);
    var canevas = carte.getCanvas();
    var largeur = canevas.clientWidth, hauteur = canevas.clientHeight;
    var posees = [];

    for (var i = 0; i < etiquettes.length; i++) {
      var etiquette = etiquettes[i];
      var afficher = false;
      if (montrer && posees.length < plafond) {
        var point = carte.project([etiquette.lon, etiquette.lat]);
        var demi = etiquette.large / 2;
        var gauche = point.x - demi - MARGE_ETIQUETTE;
        var haut = point.y - etiquette.haut / 2 - MARGE_ETIQUETTE;
        var droite = gauche + etiquette.large + 2 * MARGE_ETIQUETTE;
        var bas = haut + etiquette.haut + 2 * MARGE_ETIQUETTE;
        if (droite > 0 && gauche < largeur && bas > 0 && haut < hauteur) {
          afficher = true;
          for (var j = 0; j < posees.length; j++) {
            var autre = posees[j];
            if (gauche < autre[2] && droite > autre[0] && haut < autre[3] && bas > autre[1]) {
              afficher = false;
              break;
            }
          }
          if (afficher) {
            posees.push([gauche, haut, droite, bas]);
            etiquette.boite.style.transform =
              "translate(" + Math.round(point.x - demi) + "px," + Math.round(point.y - etiquette.haut / 2) + "px)";
          }
        }
      }
      if (afficher !== etiquette.visible) {
        etiquette.boite.style.display = afficher ? "block" : "none";
        etiquette.visible = afficher;
      }
    }
  }

  // ------------------------------------------------------------- Rendu

  function appliquer(recus) {
    var premier = args === null;
    args = recus;

    if (premier && args.mode_initial) mode = args.mode_initial;
    // Sans contours communaux, le mode « Communes » n'a pas de vue d'ensemble
    // a proposer : ses sections ne s'allumeraient qu'a partir du zoom 11,2 et
    // la carte s'ouvrirait vide. On passe alors aux sections d'office, et le
    // choix disparait plutot que de ne rien faire.
    if (!(args.geometries || {})[CLE_COMMUNE]) mode = "sections";
    majBoutonsMode();

    majFonds();
    majMetriques();

    if (args.etiquettes_jeton !== etiquettesJeton) {
      etiquettesJeton = args.etiquettes_jeton;
      preparerEtiquettes(args.etiquettes);
      preparerRecherche(args.etiquettes);
    }

    // Les gares font partie de la carte, projets compris : la ligne qui
    // n'existe pas encore se paie deja. C'est le zoom, et lui seul, qui decide
    // de leur presence a l'ecran.
    carte.getSource("gares").setData(args.gares || VIDE);

    carte.setPaintProperty("transactions", "icon-color", couleurDesPoints());

    appliquerMode();
    indexer(CLE_COMMUNE);
    indexer(CLE_SECTION);
    peindre(CLE_COMMUNE);
    peindre(CLE_SECTION);
    majMailleLue(true);
    chargerPoints();

    if (args.limites) {
      var l = args.limites; // [ouest, sud, est, nord]
      carte.setMaxBounds([[l[0], l[1]], [l[2], l[3]]]);
    }
    if (args.cadrage && args.cadrage_jeton !== dernierCadrage) {
      dernierCadrage = args.cadrage_jeton;
      if (vueInitiale) {
        vueInitiale = null;
      } else {
        vueTouchee = false;
        cadrer(!premier);
      }
    }
    planifierEtiquettes();
    majIndice();
  }

  /* Cadre la vue d'ouverture : la ou se vendent les logements, a une marge
   * pres. Elle est calculee cote Python (`cadrage_ventes`) sur les ventes et
   * non sur les contours, dont les confins reduiraient l'agglomeration a une
   * vignette. */
  var vueTouchee = false;
  var vueInitiale = null;

  /* Revenir a la vue d'ouverture : un troisieme bouton sous « + » et « − »,
   * dans le meme bloc, pour qu'il se lise comme l'un d'eux. La carte redevient
   * celle qu'on a ouverte : cadree sur l'agglomeration, sans vue dans
   * l'adresse, et de nouveau recadree si la fenetre change de taille. */
  function ajouterBoutonVueInitiale() {
    var moins = carte.getContainer().querySelector(".maplibregl-ctrl-zoom-out");
    if (!moins) return;
    var bouton = document.createElement("button");
    bouton.type = "button";
    bouton.className = "vue-initiale";
    bouton.title = "Revenir à la vue d'ensemble";
    bouton.setAttribute("aria-label", "Revenir à la vue d'ensemble");
    // Quatre coins de cadre : « tout voir », sans maison ni fleche.
    bouton.innerHTML = '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" focusable="false">'
      + '<path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
      + 'd="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4"/></svg>';
    bouton.addEventListener("click", function () {
      vueTouchee = false;
      if (window.location.hash && window.history && window.history.replaceState) {
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      }
      fermerInfobulle();
      cadrer(true);
    });
    moins.parentNode.appendChild(bouton);
  }

  function cadrer(animer) {
    if (!carte || !args || !args.cadrage) return;
    var b = args.cadrage; // [ouest, sud, est, nord]
    var canevas = carte.getCanvas();
    // Tant que le cadre n'a pas sa taille, cadrer ne donnerait rien de juste :
    // le redimensionnement qui suit s'en chargera.
    if (!canevas.clientWidth || canevas.clientHeight < 120) return;
    var marge = Math.round(Math.min(canevas.clientWidth, canevas.clientHeight) * 0.03);
    carte.fitBounds([[b[0], b[1]], [b[2], b[3]]], {
      padding: Math.max(8, Math.min(marge, 28)), duration: animer ? 600 : 0,
    });
  }

  /* Pose les contours d'une maille, puis sa couleur. Les deux sont
   * independants, et c'est tout l'interet :
   *
   * - **Le contour est une URL.** MapLibre telecharge et decoupe le fichier
   *   dans son worker ; le fil principal n'ouvre jamais les quatre megaoctets
   *   du territoire. Il n'est redemande que s'il change vraiment, sans quoi
   *   deux rendus rapproches le telechargeraient deux fois.
   * - **La couleur est un etat.** Changer un filtre ne renvoie pas une
   *   geometrie : il reecrit un etat par zone, et MapLibre ne met a jour que
   *   le tampon de couleurs des tuiles deja taillees. */
  function peindre(maille) {
    var fichier = (args.geometries || {})[maille];
    var nomSource = maille === CLE_SECTION ? "sections" : "communes";
    var source = carte.getSource(nomSource);
    if (!source) return;
    if (!fichier) {
      if (contoursPoses[nomSource]) {
        source.setData(VIDE);
        contoursPoses[nomSource] = null;
      }
      return;
    }
    if (contoursPoses[nomSource] !== fichier) {
      contoursPoses[nomSource] = fichier;
      // Le bandeau d'attente se compte : deux rendus rapproches changeant
      // deux fois de fichier ne doivent pas le laisser allume pour toujours.
      if (!contoursEnVol[nomSource]) {
        contoursEnVol[nomSource] = true;
        attendre(true);
      }
      source.setData(urlStatique(fichier));
    }
    colorer(maille, nomSource);
  }

  /* Une couleur par zone, posee dans l'etat de l'entite. L'etat precedent est
   * efface d'abord : une zone qui sort des filtres doit redevenir grise, pas
   * garder la couleur du rendu precedent. */
  function colorer(maille, nomSource) {
    effacerSurvol();
    carte.removeFeatureState({ source: nomSource });
    var couche = (args.couches || {})[maille];
    if (couche) {
      for (var i = 0; i < couche.codes.length; i++) {
        carte.setFeatureState(
          { source: nomSource, id: couche.codes[i] }, { couleur: couche.couleurs[i] }
        );
      }
    }
    dessinerLegende();
  }

  /* Rang de chaque zone dans les tableaux de `couches` : l'infobulle y lit
   * les chiffres de la zone touchee sans que la geometrie ait a les porter. */
  function indexer(maille) {
    var couche = (args.couches || {})[maille];
    var table = {};
    if (couche) for (var i = 0; i < couche.codes.length; i++) table[couche.codes[i]] = i;
    rangs[maille] = table;
  }

  // ------------------------------------------------------------- Mode et maille

  function appliquerMode() {
    var avecCommunes = mode !== "sections";
    carte.setLayoutProperty("zones-communes", "visibility", avecCommunes ? "visible" : "none");
    carte.setLayoutProperty(
      "zones-communes-contour", "visibility", avecCommunes ? "visible" : "none"
    );
    carte.setPaintProperty("zones-sections", "fill-opacity", opaciteSections(avecCommunes));
    carte.setPaintProperty(
      "zones-sections-contour", "line-opacity", opaciteTraitSections(avecCommunes)
    );
    carte.setPaintProperty("limites-communes", "line-opacity", opaciteLimites(avecCommunes));

    // Une couche hors de sa plage de zoom n'est pas seulement transparente :
    // elle n'est plus dessinee du tout. A huit mille sections, c'est ce qui
    // separe une vue d'ensemble fluide d'une vue d'ensemble qui peine —
    // MapLibre redessinerait sinon, a chaque image, un cadastre entier reduit a
    // une opacite nulle. Les bornes coincident avec celles du fondu : rien ne
    // disparait avant d'etre deja invisible.
    var bas = avecCommunes ? Z_SECTIONS_DEBUT : 0;
    carte.setLayerZoomRange("zones-sections", bas, 24);
    carte.setLayerZoomRange("zones-sections-contour", bas, 24);
    carte.setLayerZoomRange("limites-communes", bas, 24);
    if (avecCommunes) {
      carte.setLayerZoomRange("zones-communes", 0, Z_SECTIONS_PLEIN);
      carte.setLayerZoomRange("zones-communes-contour", 0, Z_SECTIONS_PLEIN);
    }
  }

  /* Quelle maille l'oeil lit-il ? La reponse ne sert qu'a deux choses : savoir
   * quelle couche interroger au pointeur, et quelle legende ecrire. Le rendu,
   * lui, n'attend personne — il suit le zoom tout seul. */
  function majMailleLue(forcer) {
    var lue = (mode === "sections" || carte.getZoom() >= Z_BASCULE)
      ? CLE_SECTION : CLE_COMMUNE;
    if (!forcer && lue === mailleLue) return;
    mailleLue = lue;
    dessinerLegende();
  }

  function majBoutonsMode() {
    var boutons = document.querySelectorAll("#mode button");
    for (var i = 0; i < boutons.length; i++) {
      boutons[i].setAttribute(
        "aria-pressed", boutons[i].dataset.mode === mode ? "true" : "false"
      );
    }
    // Sans contours communaux, le mode « Communes » n'a pas de vue d'ensemble
    // a proposer : le choix disparait plutot que de ne rien faire.
    document.getElementById("mode").hidden =
      !(args && args.geometries && args.geometries[CLE_COMMUNE]);
  }

  // --------------------------------------------------- Metrique et fond
  //
  // Les deux listes posees sur la carte. Elles ne se ressemblent que de loin :
  //
  // - **le fond** ne regarde que la carte. Ce sont des adresses de tuiles,
  //   recues une fois pour toutes ; en changer ne coute qu'une source raster,
  //   et aucun calcul. Le choix vit donc ici, et il survit aux rendus
  //   declenches par ailleurs — sans quoi toucher un filtre rendrait a
  //   l'utilisateur le fond qu'il vient de quitter ;
  // - **la metrique** decide des couleurs, des chiffres de l'infobulle et de
  //   la legende, calcules par la page. Elle remonte donc a la page
  //   (`CarteDvfHote.metrique`), et c'est le seul geste de la carte qui le
  //   fasse.

  function remplirListe(champ, options, valeur) {
    // Le `select` n'est reecrit que si ses options ont change : le
    // reconstruire a chaque rendu fermerait le menu qu'on vient d'ouvrir.
    var signature = options.map(function (o) { return o[0] + "\u0000" + o[1]; }).join("\u0001");
    if (champ.dataset.signature !== signature) {
      champ.dataset.signature = signature;
      champ.textContent = "";
      for (var i = 0; i < options.length; i++) {
        var item = document.createElement("option");
        item.value = options[i][0];
        item.textContent = options[i][1];
        champ.appendChild(item);
      }
    }
    if (valeur && champ.value !== valeur) champ.value = valeur;
  }

  function poserFond(nom) {
    var fond = fonds[nom];
    if (!fond || !fond.url || fond.url === dernierFond) return;
    changerFond(fond);
    dernierFond = fond.url;
  }

  function majFonds() {
    fonds = args.fonds || {};
    var noms = Object.keys(fonds);
    if (!noms.length) return;
    remplirListe(
      document.getElementById("choix-fond"),
      noms.map(function (nom) { return [nom, nom]; }),
      null
    );
    // Le choix de l'utilisateur l'emporte sur le fond propose par la page :
    // c'est lui qui a regarde la carte en dernier.
    var nom = fondChoisi && fonds[fondChoisi] ? fondChoisi : args.fond_initial;
    if (!fonds[nom]) nom = noms[0];
    document.getElementById("choix-fond").value = nom;
    poserFond(nom);
  }

  function majMetriques() {
    remplirListe(
      document.getElementById("choix-metrique"), args.metriques || [], args.metrique
    );
  }

  document.getElementById("choix-fond").addEventListener("change", function (evenement) {
    fondChoisi = evenement.target.value;
    poserFond(fondChoisi);
  });

  document.getElementById("choix-metrique").addEventListener("change", function (evenement) {
    // L'infobulle ouverte commente des chiffres qui vont changer.
    fermerInfobulle();
    if (hote.metrique) hote.metrique(evenement.target.value);
  });

  document.getElementById("mode").addEventListener("click", function (evenement) {
    var bouton = evenement.target.closest("button");
    if (!bouton || bouton.dataset.mode === mode) return;
    // Les deux mailles sont deja posees : changer de mode ne coute qu'un jeu
    // d'expressions d'opacite. La carte ne bouge pas, rien n'est retelecharge.
    mode = bouton.dataset.mode;
    majBoutonsMode();
    appliquerMode();
    fermerInfobulle();
    majMailleLue(true);
  });

  // ------------------------------------------------------------- Legende

  //: L'etat replie de la legende suit la taille de l'ecran, reevaluee a
  //: chaque redimensionnement tant que le visiteur n'a pas tranche.
  var PETIT_ECRAN = window.matchMedia("(max-width: 640px), (pointer: coarse)");
  var legendeChoisie = false;

  function ouvrirLegende(ouverte) {
    document.getElementById("legende-bascule")
      .setAttribute("aria-expanded", ouverte ? "true" : "false");
    document.getElementById("legende").hidden = !ouverte;
  }

  //: Tant que l'utilisateur n'a pas tranche lui-meme, la legende suit la
  //: taille de l'ecran : deployee sur grand ecran, repliee sur telephone, ou
  //: elle mangerait le quart de la carte.
  function reglerLegende() {
    if (!legendeChoisie) ouvrirLegende(!PETIT_ECRAN.matches);
  }

  function dessinerLegende() {
    if (!args) return;
    var couche = (args.couches || {})[mailleLue] || {};
    var legende = couche.legende || args.legende;
    var boite = document.getElementById("legende");
    var bascule = document.getElementById("legende-bascule");
    if (!legende) { boite.hidden = true; bascule.hidden = true; return; }
    bascule.hidden = false;
    reglerLegende();

    var graduations = (legende.graduations || [])
      .map(function (valeur) { return "<span>" + echapper(valeur) + "</span>"; }).join("");

    // L'echelle de couleur, puis une ligne pour les transports : les gares
    // sont la seconde moitie de la question « ou acheter ? », et leurs
    // dessins ne se devinent pas tous (le violet des gares a venir). Ce que
    // signifie le gris, et la mise en garde propre a l'evolution annuelle,
    // restent au survol de la legende.
    boite.title = [legende.note_gris, legende.note].filter(Boolean).join("\n\n");
    boite.innerHTML =
      '<div class="titre">' + echapper(legende.titre) + "</div>"
      + '<div class="degrade"></div>'
      + '<div class="graduations">' + graduations + "</div>"
      + '<div class="transports">'
      + "<span>" + symboleLegende("train", BLEU_GARE) + "Train, RER</span>"
      + "<span>" + symboleLegende("metro", BLEU_GARE) + "Métro</span>"
      + "<span>" + symboleLegende("tram", BLEU_GARE) + "Tram</span>"
      + "<span>" + symboleLegende("metro", VIOLET_PROJET) + "À venir</span>"
      + "</div>";
    // Le degrade se pose par le CSSOM, pas par un attribut `style` : la
    // politique de securite du site refuse les styles ecrits dans le HTML.
    boite.querySelector(".degrade").style.background =
      "linear-gradient(to right, " + (legende.palette || []).join(", ") + ")";
  }

  document.getElementById("legende-bascule").addEventListener("click", function () {
    legendeChoisie = true;
    ouvrirLegende(
      document.getElementById("legende-bascule").getAttribute("aria-expanded") === "false"
    );
  });

  // ------------------------------------------------------------- Ventes
  //
  // Les ventes arrivent par tuiles statiques et restent en memoire sous leur
  // forme brute : des colonnes de nombres. Filtrer, grouper et colorer se fait
  // ici, sur les seules tuiles a l'ecran — quelques milliers de ventes, soit
  // une poignee de millisecondes. C'est ce qui permet a un changement de
  // filtre de ne rien retelecharger.

  function lonVersX(longitude, zoom) {
    return Math.floor((longitude + 180) / 360 * Math.pow(2, zoom));
  }

  function latVersY(latitude, zoom) {
    var phi = Math.max(-85.05112878, Math.min(85.05112878, latitude)) * Math.PI / 180;
    return Math.floor(
      (1 - Math.asinh(Math.tan(phi)) / Math.PI) / 2 * Math.pow(2, zoom)
    );
  }

  function chargerManifeste() {
    var nom = args && args.points_manifeste;
    if (!nom) { manifeste = null; manifesteCharge = null; return Promise.resolve(null); }
    if (manifesteCharge === nom) return Promise.resolve(manifeste);
    manifesteCharge = nom;
    manifeste = null;
    tuiles.clear();
    return lireJson(nom)
      .then(function (charge) {
        if (manifesteCharge !== nom) return null;
        manifeste = {
          zoom: charge.zoom, cle: charge.cle, tuiles: new Set(charge.tuiles || []),
        };
        return manifeste;
      })
      .catch(function (erreur) {
        // Rien de fige : le prochain rendu redemandera ce manifeste.
        if (window.console) console.warn("[carte] manifeste indisponible", erreur);
        if (manifesteCharge === nom) manifesteCharge = null;
        manifeste = null;
        return null;
      });
  }

  function tuilesVisibles() {
    if (!manifeste) return [];
    var zoom = manifeste.zoom;
    var bornes = carte.getBounds();
    var x0 = lonVersX(bornes.getWest(), zoom), x1 = lonVersX(bornes.getEast(), zoom);
    var y0 = latVersY(bornes.getNorth(), zoom), y1 = latVersY(bornes.getSouth(), zoom);
    if ((x1 - x0 + 1) * (y1 - y0 + 1) > PLAFOND_TUILES_VISIBLES) return [];
    var liste = [];
    for (var x = x0; x <= x1; x++) {
      for (var y = y0; y <= y1; y++) {
        var cle = x + "/" + y;
        if (manifeste.tuiles.has(cle)) liste.push(cle);
      }
    }
    return liste;
  }

  function oublierTuilesEnEchec() {
    tuiles.forEach(function (charge, cle) { if (charge === null) tuiles.delete(cle); });
  }

  function chargerPoints() {
    oublierTuilesEnEchec();
    chargerManifeste().then(function () {
      planifierPoints();
    });
  }

  function planifierPoints() {
    if (rafPoints) return;
    rafPoints = requestAnimationFrame(function () {
      rafPoints = 0;
      majPoints();
    });
  }

  function majPoints() {
    var source = carte.getSource("points");
    if (!source) return;
    if (!manifeste || carte.getZoom() < Z_POINTS_CHARGE) {
      if (pointsPoses) {
        source.setData(VIDE);
        pointsPoses = false;
      }
      return;
    }
    var visibles = tuilesVisibles();
    var manquantes = visibles.filter(function (cle) {
      return !tuiles.has(cle) && !tuilesEnVol.has(cle);
    });
    manquantes.forEach(telechargerTuile);
    construirePoints(visibles);
  }

  function telechargerTuile(cle) {
    var morceaux = cle.split("/");
    var origine = manifeste.cle;
    tuilesEnVol.add(cle);
    lireJson("dvf-pts-" + origine + "-" + morceaux[0] + "-" + morceaux[1] + ".json")
      .then(function (charge) {
        tuilesEnVol.delete(cle);
        // Un changement de reglage a pu changer de jeu de tuiles pendant le
        // telechargement : celle-ci ne decrit plus les ventes affichees.
        if (!manifeste || manifeste.cle !== origine) return;
        tuiles.set(cle, charge);
        oublierTuilesAnciennes();
        planifierPoints();
      })
      .catch(function () {
        tuilesEnVol.delete(cle);
        // Une tuile absente n'est pas redemandee a chaque deplacement — mais
        // seulement jusqu'au prochain rendu (`oublierTuilesEnEchec`) : un
        // incident passager ne doit pas priver un quartier de ses ventes
        // pour toute la visite.
        if (manifeste && manifeste.cle === origine) tuiles.set(cle, null);
      });
  }

  /* `Map` conserve l'ordre d'insertion : la plus ancienne cle est la premiere,
   * et c'est exactement la politique qu'on veut. */
  function oublierTuilesAnciennes() {
    while (tuiles.size > PLAFOND_TUILES_MEMOIRE) {
      var premiere = tuiles.keys().next();
      if (premiere.done) return;
      tuiles.delete(premiere.value);
    }
  }

  /* Les filtres du panneau, traduits une fois par rendu en un objet
   * que la boucle des ventes consulte sans rien allouer. */
  function preparerFiltre() {
    var brut = (args && args.points_filtres) || {};
    return {
      annees: brut.an ? new Set(brut.an) : null,
      types: brut.ty ? new Set(brut.ty) : null,
      suMin: brut.su ? brut.su[0] : null,
      suMax: brut.su ? brut.su[1] : null,
      suOuvert: brut.su ? !!brut.su[2] : true,
      colonne: (args && args.points_echelle && args.points_echelle.colonne) || "m2",
    };
  }

  function mediane(valeurs) {
    valeurs.sort(function (a, b) { return a - b; });
    var milieu = valeurs.length >> 1;
    return valeurs.length % 2
      ? valeurs[milieu]
      : (valeurs[milieu - 1] + valeurs[milieu]) / 2;
  }

  /* Quelle icone pour un libelle de type de bien. Les DVF n'en connaissent
   * que trois — maison, appartement, et le melange des deux. */
  function iconeDuType(libelle) {
    var mot = String(libelle || "").toLowerCase();
    if (mot.indexOf("maison") === 0) return TYPE_MAISON;
    if (mot.indexOf("appart") === 0) return TYPE_APPARTEMENT;
    return TYPE_MIXTE;
  }

  function construirePoints(visibles) {
    var filtre = preparerFiltre();
    var entites = [];
    var valeurs = [];
    for (var t = 0; t < visibles.length; t++) {
      var cle = visibles[t];
      var tuile = tuiles.get(cle);
      if (!tuile) continue;
      // Types autorises et icones sont resolus une fois par tuile : dans la
      // boucle des ventes, il ne reste qu'une lecture de tableau.
      var typesOk = (tuile.ty_l || []).map(function (libelle) {
        return !filtre.types || filtre.types.has(libelle);
      });
      var icones = (tuile.ty_l || []).map(iconeDuType);
      var metrique = tuile[filtre.colonne] || tuile.m2;
      for (var e = 0; e < tuile.lon.length; e++) {
        // `valeurs` est reutilise d'un emplacement a l'autre : a quelques
        // milliers de ventes a l'ecran, allouer un tableau par emplacement
        // ferait travailler le ramasse-miettes en plein deplacement.
        valeurs.length = 0;
        var gardees = 0;
        // Un emplacement porte le dessin de ce qu'on y a vendu — et le
        // losange des lors que deux natures s'y superposent, ce qui arrive
        // des qu'une maison et un appartement partagent une parcelle.
        var icone = -1;
        for (var v = tuile.off[e]; v < tuile.off[e + 1]; v++) {
          if (!retenue(tuile, v, filtre, typesOk)) continue;
          gardees += 1;
          var sienne = icones[tuile.ty[v]];
          icone = icone < 0 || icone === sienne ? sienne : TYPE_MIXTE;
          if (metrique[v] >= 0) valeurs.push(metrique[v]);
        }
        if (!gardees) continue;
        entites.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: [tuile.lon[e], tuile.lat[e]] },
          properties: {
            v: valeurs.length ? mediane(valeurs) : -1,
            k: icone < 0 ? TYPE_MIXTE : icone,
            t: cle,
            e: e,
          },
        });
      }
    }
    carte.getSource("points").setData({ type: "FeatureCollection", features: entites });
    pointsPoses = true;
  }

  /* Un filtre du panneau, applique vente par vente. Les regles sont
   * celles de `stats.filtrer`, y compris pour les trous : une vente sans
   * surface declaree ne passe aucune borne, ici comme en Python. */
  function retenue(tuile, v, filtre, typesOk) {
    if (filtre.annees && !filtre.annees.has(tuile.an[v])) return false;
    if (filtre.types && !typesOk[tuile.ty[v]]) return false;
    if (filtre.suMin !== null) {
      var surface = tuile.su[v];
      if (surface < 0 || surface < filtre.suMin) return false;
      if (!filtre.suOuvert && surface > filtre.suMax) return false;
    }
    return true;
  }

  /* La couleur d'une vente se lit sur la meme echelle que celle des sections :
   * un point plus rouge que sa section s'y est vendu plus cher qu'elle. Elle
   * est calculee par le GPU a partir du prix porte par le point, ce qui evite
   * de reecrire une couleur par vente a chaque changement de filtre. */
  function couleurDesPoints() {
    var echelle = args && args.points_echelle;
    if (!echelle || echelle.vide || !echelle.palette || echelle.palette.length < 2) return GRIS;
    if (!(echelle.haut > echelle.bas)) return echelle.palette[echelle.palette.length >> 1];
    var paliers = ["interpolate", ["linear"], ["get", "v"]];
    for (var i = 0; i < echelle.palette.length; i++) {
      var part = i / (echelle.palette.length - 1);
      paliers.push(echelle.bas + part * (echelle.haut - echelle.bas), echelle.palette[i]);
    }
    // `-1` marque une vente dont le prix au m² n'est pas calculable : elle est
    // grise, comme une section sans donnee exploitable.
    return ["case", ["<", ["get", "v"], 0], GRIS, paliers];
  }

  function infobulleEmplacement(proprietes) {
    var tuile = tuiles.get(proprietes.t);
    if (!tuile) return '<div class="entete">Vente</div>';
    var filtre = preparerFiltre();
    var typesOk = (tuile.ty_l || []).map(function (libelle) {
      return !filtre.types || filtre.types.has(libelle);
    });
    var e = proprietes.e;
    var ventes = [];
    for (var v = tuile.off[e]; v < tuile.off[e + 1]; v++) {
      if (retenue(tuile, v, filtre, typesOk)) ventes.push(v);
    }
    if (!ventes.length) return '<div class="entete">Aucune vente avec les filtres actifs</div>';
    ventes.sort(function (a, b) { return tuile.dt[b] - tuile.dt[a]; });

    // Les DVF geolocalisent a la parcelle, pas au logement : deux tiers des
    // ventes partagent leur position avec une autre. L'adresse n'est reprise
    // en sous-titre que lorsqu'elle est commune a tout l'emplacement.
    var adresses = {};
    for (var i = 0; i < ventes.length; i++) adresses[tuile.ad[ventes[i]]] = true;
    var cles = Object.keys(adresses);
    var adresse = cles.length === 1 ? (tuile.ad_l[cles[0]] || "") : "";

    if (ventes.length === 1) return venteDetaillee(tuile, ventes[0], adresse);

    var lignes = ['<div class="entete">' + ventes.length + " ventes "
      + (adresse ? "à cette adresse" : "à cet emplacement") + "</div>"];
    if (adresse) lignes.push('<div class="repere">' + echapper(adresse) + "</div>");
    // Toutes les ventes, sans plafond : sur un immeuble, les quelques
    // dizaines de mutations *sont* l'information. L'infobulle epinglee
    // defile ; ce qui couterait cher, ce serait de construire ces lignes a
    // chaque image, or elles ne le sont qu'au clic, pour un emplacement.
    lignes.push('<div class="ventes">');
    for (var j = 0; j < ventes.length; j++) {
      lignes.push(ligneVente(tuile, ventes[j], !adresse));
    }
    lignes.push("</div>");
    return lignes.join("");
  }

  function venteDetaillee(tuile, v, adresse) {
    var pieces = tuile.pi[v] >= 0 ? Number(tuile.pi[v]) : "?";
    return '<div class="entete">' + echapper(tuile.ty_l[tuile.ty[v]] || "Bien")
      + " — " + formater(positif(tuile.su[v]), "surface") + "</div>"
      + "<div>" + formaterDate(tuile.dt[v]) + "</div>"
      + "<div>" + formater(positif(tuile.va[v]), "euros")
      + " (" + formater(positif(tuile.m2[v]), "euros_m2") + ")</div>"
      + "<div>" + pieces + " pièce(s)</div>"
      + "<div><i>" + echapper(adresse || tuile.ad_l[tuile.ad[v]] || "adresse non renseignée")
      + "</i></div>";
  }

  function ligneVente(tuile, v, avecAdresse) {
    var pieces = tuile.pi[v] >= 0 ? Number(tuile.pi[v]) + " p." : "? p.";
    var adresse = avecAdresse
      ? " · <i>" + echapper(tuile.ad_l[tuile.ad[v]] || "adresse non renseignée") + "</i>"
      : "";
    return '<div class="vente">' + formaterDate(tuile.dt[v]) + " · "
      + echapper(tuile.ty_l[tuile.ty[v]] || "Bien") + " "
      + formater(positif(tuile.su[v]), "surface") + ", " + pieces + " · "
      + formater(positif(tuile.va[v]), "euros")
      + " (" + formater(positif(tuile.m2[v]), "euros_m2") + ")" + adresse + "</div>";
  }

  //: Les colonnes d'une tuile marquent l'absence par `-1` : c'est ici qu'on le
  //: retraduit en « n/d ».
  function positif(valeur) {
    return valeur === undefined || valeur === null || valeur < 0 ? null : valeur;
  }

  // ----------------------------------------------------- Adresse partageable
  //
  // La vue courante s'ecrit dans l'adresse de la page (`#zoom/lat/lon`), des
  // que le visiteur a bouge la carte : copier l'adresse suffit a partager un
  // quartier. `replaceState` et non `pushState` : chaque deplacement n'a pas a
  // devenir une page de l'historique.

  function vueDeLAdresse() {
    var m = /^#(\d{1,2}(?:\.\d+)?)\/(-?\d{1,2}(?:\.\d+)?)\/(-?\d{1,3}(?:\.\d+)?)$/
      .exec(window.location.hash || "");
    if (!m) return null;
    var zoom = Number(m[1]), latitude = Number(m[2]), longitude = Number(m[3]);
    if (!(zoom >= 7 && zoom <= 19 && Math.abs(latitude) <= 85 && Math.abs(longitude) <= 180)) {
      return null;
    }
    return { zoom: zoom, center: [longitude, latitude] };
  }

  function ecrireAdresse() {
    if (!vueTouchee || !window.history || !window.history.replaceState) return;
    var centre = carte.getCenter();
    window.history.replaceState(null, "", "#" + carte.getZoom().toFixed(2) + "/"
      + centre.lat.toFixed(5) + "/" + centre.lng.toFixed(5));
  }

  // --------------------------------------------------- Aller a une commune
  //
  // Les noms sont ceux des etiquettes de la carte : la recherche se fait ici,
  // sans service exterieur, et tolere accents, tirets et majuscules
  // (« st maur » trouve Saint-Maur-des-Fossés).

  //: Zoom d'arrivee : les quartiers sont la, les gares se designent.
  var ZOOM_COMMUNE = 13;
  var communes = [];

  function cleRecherche(texte) {
    return String(texte).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
      .replace(/\bst\b/g, "saint").replace(/\bste\b/g, "sainte")
      .replace(/[^a-z0-9]+/g, " ").trim();
  }

  function preparerRecherche(liste) {
    communes = (liste || []).map(function (e) {
      return { nom: e.n, cle: cleRecherche(e.n), x: e.x, y: e.y };
    }).sort(function (a, b) { return a.nom.localeCompare(b.nom, "fr"); });
    var choix = document.getElementById("liste-communes");
    choix.textContent = "";
    for (var i = 0; i < communes.length; i++) {
      var option = document.createElement("option");
      option.value = communes[i].nom;
      choix.appendChild(option);
    }
  }

  function trouverCommune(texte) {
    var cle = cleRecherche(texte);
    if (!cle) return null;
    var debut = null, dedans = null;
    for (var i = 0; i < communes.length; i++) {
      var c = communes[i];
      if (c.cle === cle) return c;
      if (!debut && c.cle.indexOf(cle) === 0) debut = c;
      if (!dedans && c.cle.indexOf(cle) >= 0) dedans = c;
    }
    return debut || dedans;
  }

  function allerA(texte, exacte) {
    var champ = document.getElementById("aller-a");
    var commune = trouverCommune(texte);
    if (exacte && (!commune || commune.nom !== texte)) return;
    champ.classList.toggle("introuvable", !commune);
    if (!commune || !carte) return;
    champ.value = "";
    champ.blur();
    vueTouchee = true;
    carte.flyTo({
      center: [commune.x, commune.y], zoom: Math.max(carte.getZoom(), ZOOM_COMMUNE),
      essential: true,
    });
  }

  (function brancherRecherche() {
    var champ = document.getElementById("aller-a");
    // Un nom choisi dans la liste part tout de suite ; un nom tape, a Entree.
    champ.addEventListener("input", function () {
      champ.classList.remove("introuvable");
      allerA(champ.value, true);
    });
    champ.addEventListener("keydown", function (evenement) {
      if (evenement.key === "Enter") { evenement.preventDefault(); allerA(champ.value, false); }
    });
  })();

  // ------------------------------------------------------- Indice de zoom
  //
  // Ce que la carte reserve a qui zoome — les gares, les quartiers, puis
  // chaque vente — ne se devine pas depuis la vue d'ensemble. Un indice
  // discret le dit, cliquable pour zoomer, et s'efface pour la session des
  // que les ventes ont ete atteintes une fois.

  var CLE_INDICE = "ventes-atteintes";
  var ventesAtteintes = false;
  try { ventesAtteintes = !!window.sessionStorage.getItem(CLE_INDICE); } catch (erreur) { /* sans stockage */ }

  function majIndice() {
    var bouton = document.getElementById("indice-zoom");
    if (!carte || !args) return;
    var zoom = carte.getZoom();
    if (!ventesAtteintes && zoom >= Z_POINTS_PLEIN) {
      ventesAtteintes = true;
      try { window.sessionStorage.setItem(CLE_INDICE, "1"); } catch (erreur) { /* sans stockage */ }
    }
    var montrer = !ventesAtteintes && zoom < Z_POINTS_DEBUT;
    bouton.hidden = !montrer;
    if (!montrer) return;
    var texte = zoom < Z_SECTIONS_PLEIN
      ? "Zoomez : gares, quartiers, ventes"
      : "Zoomez : chaque vente, rue par rue";
    var libelle = bouton.querySelector("span");
    if (libelle.textContent !== texte) libelle.textContent = texte;
  }

  document.getElementById("indice-zoom").addEventListener("click", function (evenement) {
    if (!carte) return;
    carte.easeTo(
      { zoom: Math.min(carte.getZoom() + 2, Z_POINTS_PLEIN + 0.2), duration: 700 },
      { originalEvent: evenement }
    );
  });

  creer();

  // La porte d'entree de la page. Des arguments recus avant que la carte soit
  // prete attendent leur tour.
  window.CarteDvf = {
    appliquer: function (recus) {
      if (!pret) { enAttente = recus; return; }
      appliquer(recus);
    },
    panne: function (message) { signalerPanne(message); },
    //: Pour les tests : le trait pose, et les gares qu'il relie.
    trajet: function () {
      return trajet ? {
        origine: trajet.origine,
        segments: trajet.segments.slice(),
        ecarts: (trajet.ecarts || []).slice(),
        gares: trajet.gares.map(function (r) {
          return { nom: r.gare.properties.nom, minutes: r.minutes, a_venir: r.a_venir,
                   position: r.gare.geometry.coordinates.slice() };
        }),
      } : null;
    },
  };
})();
