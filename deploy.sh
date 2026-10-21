#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy Copilot MVP to the local Kind cluster.
# Run this script from any directory; it locates the project root from its own path.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
CLUSTER_NAME="${CLUSTER_NAME:-copilot-test}"
NAMESPACE="${NAMESPACE:-copilot-mvp}"
DEPLOYMENT="${DEPLOYMENT:-copilot-api}"
CONTAINER_NAME="${CONTAINER_NAME:-api}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-copilot-api}"
IMAGE_TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${IMAGE_REPOSITORY}:${IMAGE_TAG}"
RUNNER_IMAGE_REPOSITORY="${RUNNER_IMAGE_REPOSITORY:-copilot-runner}"
RUNNER_IMAGE="${RUNNER_IMAGE_REPOSITORY}:${IMAGE_TAG}"

for cmd in docker kind kubectl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERRORE: comando richiesto non trovato: $cmd" >&2
    exit 1
  fi
done

if [[ ! -f "$PROJECT_ROOT/backend/Dockerfile" || ! -f "$PROJECT_ROOT/frontend/index.html" || ! -f "$PROJECT_ROOT/runner/Dockerfile" ]]; then
  echo "ERRORE: non trovo backend/Dockerfile e frontend/index.html in $PROJECT_ROOT" >&2
  echo "Metti deploy.sh nella directory principale copilot-k8s-mvp." >&2
  exit 1
fi

if ! kind get clusters | grep -Fxq "$CLUSTER_NAME"; then
  echo "ERRORE: cluster Kind '$CLUSTER_NAME' non trovato." >&2
  echo "Cluster disponibili:" >&2
  kind get clusters >&2
  exit 1
fi

if ! kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "ERRORE: Deployment '$DEPLOYMENT' non trovato nel namespace '$NAMESPACE'." >&2
  echo "Controlla il contesto con: kubectl config current-context" >&2
  exit 1
fi

echo "==> Build runner: $RUNNER_IMAGE"
docker build -t "$RUNNER_IMAGE" "$PROJECT_ROOT/runner"

echo "==> Build immagine: $IMAGE"
docker build -f "$PROJECT_ROOT/backend/Dockerfile" -t "$IMAGE" "$PROJECT_ROOT"

echo "==> Caricamento immagine nel cluster Kind: $CLUSTER_NAME"
kind load docker-image "$IMAGE" "$RUNNER_IMAGE" --name "$CLUSTER_NAME"

echo "==> Aggiornamento Deployment: $DEPLOYMENT (namespace: $NAMESPACE)"
kubectl set image "deployment/$DEPLOYMENT" "$CONTAINER_NAME=$IMAGE" -n "$NAMESPACE"
kubectl set env "deployment/$DEPLOYMENT" "RUNNER_IMAGE=$RUNNER_IMAGE" -n "$NAMESPACE"

echo "==> Attendo il rollout..."
if ! kubectl rollout status "deployment/$DEPLOYMENT" -n "$NAMESPACE" --timeout=180s; then
  echo "ERRORE: rollout non completato. Stato attuale:" >&2
  kubectl get pods -n "$NAMESPACE" -o wide >&2 || true
  exit 1
fi

echo
echo "==> Deployment aggiornato correttamente."
kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o wide
kubectl get pods -n "$NAMESPACE" -o wide
echo
echo "Frontend/API: se il port-forward non è già attivo, esegui in un altro terminale:"
echo "kubectl port-forward -n $NAMESPACE service/copilot-api 8080:8080"
echo "Poi apri http://localhost:8080"
