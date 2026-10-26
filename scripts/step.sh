#!/usr/bin/env bash
# Usage : scripts/step.sh "Titre" commande args...
# Exécute la commande, garde sa sortie, et la remonte en annotation GitHub (lisible via l'API
# check-runs). Le code 124 (délai `timeout` atteint) n'est pas une erreur : la collecte reprendra.
title="$1"; shift
log="$(mktemp)"
"$@" 2>&1 | tee "$log"
code=${PIPESTATUS[0]}
tail_txt="$(tail -c 1500 "$log" | tr '\r\n' '  ' | sed 's/%/%25/g')"
if [ "$code" -eq 3 ]; then
  echo "::warning title=${title}::source indisponible | ${tail_txt}"
  exit 0
fi
if [ "$code" -eq 0 ] || [ "$code" -eq 124 ]; then
  echo "::notice title=${title}::code ${code} | ${tail_txt: -600}"
  exit 0
fi
echo "::error title=${title}::code ${code} | ${tail_txt}"
exit "$code"
