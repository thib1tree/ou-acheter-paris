#!/usr/bin/env bash
# Lance `git` authentifié par le jeton de `GH_TOKEN`, sans l'écrire nulle part.
#
#     GH_TOKEN=… scripts/git_jeton.sh push -u origin BRANCHE
#
# Les workflows extraient le dépôt sans y laisser de jeton
# (`persist-credentials: false`) : ni `pip install` ni les tests ne peuvent
# pousser dans le dépôt. Seules les étapes qui poussent passent par ce script,
# le jeton dans leur seul environnement. Git le demande à l'assistant
# d'identification ci-dessous, qui le lit dans l'environnement : il n'est écrit
# ni dans `.git/config`, ni sur la ligne de commande.
set -euo pipefail
: "${GH_TOKEN:?GH_TOKEN absent}"
exec git -c credential.helper= \
  -c 'credential.helper=!f() { test "$1" = get && printf "username=x-access-token\npassword=%s\n" "$GH_TOKEN"; }; f' \
  "$@"
