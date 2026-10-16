#!/usr/bin/env bash
# 새 이미지를 만들어 Harbor 에 올리고 ns-branch-graph-dev 에 배포한다.
#   ./deploy/k8s/release.sh
# 미리 필요: docker login docker.altimedia.com, Secret teams-roulette/teams-vote,
#            사내망 또는 VPN (KUBECONFIG=~/.kube/ns-branch-graph-dev.yaml)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TAG="$(date +%Y%m%d-%H%M)"
IMAGE="docker.altimedia.com/branch-graph/teams-agents"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/ns-branch-graph-dev.yaml}"

cd "$ROOT"
for t in roulette-agent/tests/check.py vote-agent/tests/check.py; do
    .venv/bin/python "$t" >/dev/null 2>&1 || { echo "✖ 테스트 실패($t). 배포하지 않습니다."; exit 1; }
done

# 없는 Secret 으로 배포하면 컨테이너가 CreateContainerConfigError 로 멈춘다. 먼저 확인한다(값은 보지 않는다).
for s in teams-roulette teams-vote; do
    kubectl get secret "$s" -o name >/dev/null 2>&1 || { echo "✖ Secret $s 가 없습니다 (docs/K8S-HANDOFF.md 4.3)."; exit 1; }
done

docker buildx build --platform linux/amd64 -t "$IMAGE:$TAG" --push .
sed -i '' -E "s/newTag: \"[^\"]*\"/newTag: \"$TAG\"/" deploy/k8s/kustomization.yaml
kubectl apply -k deploy/k8s
kubectl rollout status deploy/teams-agents --timeout=300s
echo "✔ 배포 완료: $IMAGE:$TAG"
echo "  터널 로그: kubectl logs deploy/teams-agents -c tunnel"
