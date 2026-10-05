/* MapLibre GL JS n'est plus distribue qu'en modules depuis sa version 6 :
 * ce module l'importe et l'expose sous le nom qu'attend `carte.js`
 * (`window.maplibregl`).
 *
 * L'ordre d'execution est garanti par la page : ce module et les scripts
 * `defer` qui le suivent (`carte.js`, `app.js`) s'executent dans l'ordre ou
 * ils sont ecrits, une fois la page lue. */
import * as maplibregl from "./vendor/maplibre-gl.mjs";

window.maplibregl = maplibregl;
