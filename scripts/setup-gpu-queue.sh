#!/usr/bin/env bash
# Pueue is a cooperative scheduler; external GPU tasks are not locked.
set -euo pipefail
command -v pueue >/dev/null || { echo "Install Pueue first" >&2; exit 2; }
pueue status >/dev/null || { echo "Start pueued first" >&2; exit 2; }
GROUP="${GPU_GROUP:-gpu0}"
[[ "$GROUP" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "Invalid group" >&2; exit 2; }
if ! pueue group | grep -Eq "(^|[[:space:]])${GROUP}([[:space:]]|$)"; then
  pueue group add "$GROUP"
fi
pueue parallel -g "$GROUP" 1
echo "Ready: max 1 task in $GROUP, shared by all projects."
