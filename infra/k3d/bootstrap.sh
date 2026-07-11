#!/usr/bin/env bash
# One-command local NimbleShip: k3d cluster + images + helm install.
# Prerequisites: docker (or colima), k3d, helm.
set -euo pipefail

cd "$(dirname "$0")/../.."

CLUSTER=nimbleship

if ! k3d cluster list | grep -q "^${CLUSTER}"; then
  k3d cluster create "${CLUSTER}" --port "8080:80@loadbalancer"
fi

docker build -t nimbleship-api:dev api
docker build -t nimbleship-web:dev web
k3d image import -c "${CLUSTER}" nimbleship-api:dev nimbleship-web:dev

helm upgrade --install nimbleship infra/chart/nimbleship

echo
echo "NimbleShip is starting: http://nimbleship.localhost:8080"
echo "API docs:               http://nimbleship.localhost:8080/api/docs"
kubectl get pods
