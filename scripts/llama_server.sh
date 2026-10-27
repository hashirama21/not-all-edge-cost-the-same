#!/usr/bin/env bash
# Usage : scripts/llama_server.sh
# Lance llama-server (llama.cpp) en Docker et attend qu'il soit prêt (/health = 200).
# Variables :
#   LLAMA_MODEL       dépôt GGUF Hugging Face au format utilisateur/depot[:quant] (votre modèle)
#   LLAMA_IMAGE       image Docker (ghcr.io/ggml-org/llama.cpp:server ; :server-cuda sur runner GPU)
#   LLAMA_PORT        port local (8080)
#   LLAMA_CTX         taille de contexte (32768 : les prompts browser-use sont longs)
#   LLAMA_CACHE_DIR   cache des modèles téléchargés, à mettre en cache entre deux lancements
#   LLAMA_THINKING    true pour activer le raisonnement de Qwen (false : plus rapide sur CPU)
#   LLAMA_VISION      true pour charger le projecteur d'images (mmproj) du modèle
#   LLAMA_EXTRA_ARGS  options supplémentaires de llama-server
#   LLAMA_WAIT_S      délai maximal d'attente (1800 s : premier téléchargement compris)
#
# Modèle par défaut : Qwen3.5-9B (Qwen, 2026), le plus récent des petits Qwen ; 5,7 Go en
# Q4_K_M, il tient sur un runner GitHub standard (16 Go, CPU). Qwen3.8-27B
# (unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M, 16,5 Go) demande un runner GPU d'au moins 24 Go.
set -euo pipefail

model="${LLAMA_MODEL:-unsloth/Qwen3.5-9B-GGUF:Q4_K_M}"
image="${LLAMA_IMAGE:-ghcr.io/ggml-org/llama.cpp:server}"
port="${LLAMA_PORT:-8080}"
ctx="${LLAMA_CTX:-32768}"
cache="${LLAMA_CACHE_DIR:-$HOME/.cache/llama.cpp}"
wait_s="${LLAMA_WAIT_S:-1800}"
name="cmrmp-llama"

mkdir -p "$cache"
opts=()
if [[ "$image" == *cuda* ]]; then opts+=(--gpus all); fi
# Jeton transmis au conteneur pour un dépôt de modèle privé ou à accès restreint.
if [ -n "${HF_TOKEN:-}" ]; then opts+=(-e HF_TOKEN); fi

args=()
if [ "${LLAMA_THINKING:-false}" = "true" ]; then thinking=true; else thinking=false; fi
args+=(--chat-template-kwargs "{\"enable_thinking\":${thinking}}")
# -hf télécharge aussi le mmproj du dépôt ; sans vision, inutile de le charger en mémoire.
if [ "${LLAMA_VISION:-false}" != "true" ]; then args+=(--no-mmproj); fi
docker rm -f "$name" >/dev/null 2>&1 || true

# -np 1 : un seul slot, qui reçoit tout le contexte (sinon il est partagé entre slots).
# -n 4096 : plafond de génération côté serveur, quel que soit le paramètre envoyé par le client.
# shellcheck disable=SC2086
docker run -d --name "$name" "${opts[@]}" -p "127.0.0.1:${port}:8080" \
  -v "$cache:/root/.cache/llama.cpp" -e LLAMA_CACHE=/root/.cache/llama.cpp \
  "$image" -hf "$model" --alias local --host 0.0.0.0 --port 8080 \
  -c "$ctx" -np 1 -n 4096 --jinja "${args[@]}" ${LLAMA_EXTRA_ARGS:-} >/dev/null

echo "llama-server : $model ($image), attente du chargement…"
for _ in $(seq 1 $((wait_s / 5))); do
  if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    echo "llama-server prêt sur http://127.0.0.1:${port}/v1"
    exit 0
  fi
  if [ -z "$(docker ps -q -f name="^${name}$")" ]; then
    echo "llama-server s'est arrêté :" >&2
    docker logs "$name" 2>&1 | tail -50 >&2
    exit 1
  fi
  sleep 5
done
echo "llama-server pas prêt après ${wait_s} s" >&2
docker logs "$name" 2>&1 | tail -50 >&2
exit 1
