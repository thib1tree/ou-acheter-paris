#!/usr/bin/env bash
# Ouvre une issue, ou complète celle qui est déjà ouverte avec la même étiquette.
#
#     scripts/signaler.sh ETIQUETTE "Titre" FICHIER_CORPS
#
# Pour les workflows de maintenance : un même incident, répété de semaine en
# semaine, s'accumule dans une seule issue au lieu d'en ouvrir une par jour.
# Les issues s'ouvrent avec le jeton par défaut de GitHub Actions (GH_TOKEN) :
# ouvertes au nom de github-actions, elles notifient le propriétaire du dépôt.
set -euo pipefail

etiquette=$1
titre=$2
corps=$3

gh label create "$etiquette" --color B60205 \
  --description "Signalé par la maintenance automatique" 2>/dev/null || true
existante=$(gh issue list --label "$etiquette" --state open --json number --jq '.[0].number')
if [ -n "$existante" ]; then
  gh issue comment "$existante" --body-file "$corps"
else
  gh issue create --title "$titre" --label "$etiquette" --body-file "$corps"
fi
