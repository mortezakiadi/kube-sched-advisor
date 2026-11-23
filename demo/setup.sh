#!/usr/bin/env bash
# Prepare the demo cluster. It has two kinds of GPU node:
#
#   real      the two g5.xlarge nodes (label ksa/real-gpu=true) carry one real NVIDIA A10G each.
#             The NVIDIA device plugin and GPU feature discovery are installed with helm, so the
#             GPU count and the nvidia.com/gpu.product label come from real discovery. The nodes
#             then get the usual nvidia.com/gpu taint that GPU pools carry.
#   stand-in  the three t3.medium nodes advertise 8 nvidia.com/gpu each (set on the node status)
#             and carry the H100 product label and a network-block label. No real GPUs: they let
#             the demo show 8-GPU, multi-node and network-block scheduling on a small bill.
#
# Other teams' pods then hold 2 stand-in GPUs per node, so 18 are free but no node has 8 free.
set -euo pipefail

echo "waiting for nodes..."
kubectl wait --for=condition=Ready node --all --timeout=300s >/dev/null

# --- real GPUs: device plugin + GPU feature discovery (pinned chart version)
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin >/dev/null 2>&1 || true
helm repo update nvdp >/dev/null
helm upgrade -i nvdp nvdp/nvidia-device-plugin --version 0.20.2 \
  --namespace nvidia-device-plugin --create-namespace --set gfd.enabled=true >/dev/null
echo "waiting for the real GPUs to be discovered..."
for i in $(seq 1 60); do
  ready=$(kubectl get nodes -l ksa/real-gpu=true \
    -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{" "}{.metadata.labels.nvidia\.com/gpu\.product}{"\n"}{end}' | grep -c '^1 NVIDIA' || true)
  [ "$ready" = "2" ] && break
  sleep 10
done
[ "$ready" = "2" ] || { echo "real GPUs were not discovered in time" >&2; exit 1; }
for n in $(kubectl get nodes -l ksa/real-gpu=true -o name | sed 's#node/##'); do
  kubectl taint node "$n" nvidia.com/gpu=present:NoSchedule --overwrite >/dev/null
done

# --- stand-in GPUs on the CPU nodes
STANDIN=($(kubectl get nodes -l '!ksa/real-gpu' -o name | sed 's#node/##' | sort))   # bash 3.2 safe
BLOCKS=(block-a block-a block-b)
for i in "${!STANDIN[@]}"; do
  n="${STANDIN[$i]}"
  kubectl label node "$n" --overwrite \
    nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 \
    "network.topology/block=${BLOCKS[$i]}" >/dev/null
  kubectl patch node "$n" --subresource=status --type=json -p '[
    {"op":"add","path":"/status/capacity/nvidia.com~1gpu","value":"8"},
    {"op":"add","path":"/status/allocatable/nvidia.com~1gpu","value":"8"}]' >/dev/null
done

kubectl create namespace other-teams --dry-run=client -o yaml | kubectl apply -f - >/dev/null
for i in "${!STANDIN[@]}"; do
  cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: other-team-job-$i
  namespace: other-teams
spec:
  nodeSelector:
    kubernetes.io/hostname: ${STANDIN[$i]}
  containers:
  - name: hold
    image: registry.k8s.io/pause:3.10
    resources:
      requests: {cpu: 10m, memory: 8Mi, nvidia.com/gpu: "2"}
      limits: {nvidia.com/gpu: "2"}
EOF
done
kubectl -n other-teams wait --for=condition=Ready pod --all --timeout=180s >/dev/null
echo "cluster ready: 2 real A10G nodes, ${#STANDIN[@]} stand-in nodes x 8 GPUs, 2 held on each by other-teams"
