#!/usr/bin/env bash
# Make a small EKS cluster look like a GPU cluster, then create "other teams" load.
#
#   * each node advertises 8 nvidia.com/gpu (extended resource, set on the node status)
#   * nodes carry the GPU product label and a network-block label that a GPU
#     operator / topology discovery would normally set
#   * background pods hold 2 GPUs on every node, so 18 GPUs are free but no node has 8
#
# No real GPUs are used. The scheduler treats the advertised count exactly like real
# capacity, which is all the advisor (and kube-scheduler) look at.
set -euo pipefail

NODES=($(kubectl get nodes -o name | sed 's#node/##'))   # bash 3.2 (macOS) safe
BLOCKS=(block-a block-a block-b)

for i in "${!NODES[@]}"; do
  n="${NODES[$i]}"
  kubectl label node "$n" --overwrite \
    nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 \
    "network.topology/block=${BLOCKS[$i]}" >/dev/null
  kubectl patch node "$n" --subresource=status --type=json -p '[
    {"op":"add","path":"/status/capacity/nvidia.com~1gpu","value":"8"},
    {"op":"add","path":"/status/allocatable/nvidia.com~1gpu","value":"8"}]' >/dev/null
done

kubectl create namespace other-teams --dry-run=client -o yaml | kubectl apply -f - >/dev/null
for i in "${!NODES[@]}"; do
  cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: other-team-job-$i
  namespace: other-teams
spec:
  nodeSelector:
    kubernetes.io/hostname: ${NODES[$i]}
  containers:
  - name: hold
    image: registry.k8s.io/pause:3.10
    resources:
      requests: {cpu: 10m, memory: 8Mi, nvidia.com/gpu: "2"}
      limits: {nvidia.com/gpu: "2"}
EOF
done
kubectl -n other-teams wait --for=condition=Ready pod --all --timeout=180s >/dev/null
echo "cluster ready: ${#NODES[@]} nodes x 8 advertised GPUs, 2 held on each by other-teams"
