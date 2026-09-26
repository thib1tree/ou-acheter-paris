/* La carte lit ses donnees dans `donnees/`, et previent la page quand on
 * choisit une autre metrique (`app.js` remplace ce rappel).
 *
 * Un fichier plutot qu'un script en ligne : la politique de securite du site
 * (`Content-Security-Policy`, voir `scripts/construire_site.py`) n'autorise
 * que les scripts servis par le site lui-meme. */
window.CarteDvfHote = { base: "donnees/" };
