#!/usr/bin/env bash
# Prepare the demo cluster. It has two kinds of GPU node, labelled ksa/gpu-kind:
#
#   real      the four g5.xlarge nodes (label ksa/real-gpu=true) carry one real NVIDIA A10G each.
#             The NVIDIA device plugin and GPU feature discovery are installed with helm, so the
#             GPU count and the nvidia.com/gpu.product label come from real discovery. The nodes
#             then get the usual nvidia.com/gpu taint that GPU pools carry.
#   stand-in  the three t3.medium nodes have no GPU. Each is given 8 nvidia.com/gpu on its node
#             status, the H100 product label and a network-block label, so the demo can show
#             8-GPU, multi-node and network-block scheduling that the real nodes are too small
#             for. The scheduler treats the number like any other resource, but nothing here can
#             actually run a GPU workload: only no-op containers are ever placed on these nodes.
#
# "Other teams" run pods that hold GPUs on both kinds of node: one GPU on each of two real nodes,
# and 2 stand-in GPUs on each stand-in node. Their pods carry a priority class with the same value
# as the advisor's "normal" class (10000). Pods with no priority at all would be preempted by the
# advisor's workloads, which is the advisor's documented way to free GPUs, but it is not the story
# told here. They run a do-nothing image: they request GPUs, so the scheduler and the device
# plugin treat them like a team's running job.
set -euo pipefail

echo "waiting for nodes..."
kubectl wait --for=condition=Ready node --all --timeout=300s >/dev/null

# ------------------------------------------------------------------ real GPUs
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin >/dev/null 2>&1 || true
helm repo update nvdp >/dev/null
helm upgrade -i nvdp nvdp/nvidia-device-plugin --version 0.20.2 \
  --namespace nvidia-device-plugin --create-namespace --set gfd.enabled=true >/dev/null

echo "waiting for the real GPUs to be discovered..."
for i in $(seq 1 90); do
  total=$(kubectl get nodes -l ksa/real-gpu=true -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' | awk '{s+=$1} END {print s+0}')
  labelled=$(kubectl get nodes -l ksa/real-gpu=true,nvidia.com/gpu.product=NVIDIA-A10G -o name | wc -l | tr -d ' ')
  [ "$total" = "4" ] && [ "$labelled" = "4" ] && break
  sleep 10
done
[ "$total" = "4" ] && [ "$labelled" = "4" ] || { echo "GPUs not discovered in time (total=$total labelled=$labelled)" >&2; exit 1; }

REAL=($(kubectl get nodes -l ksa/real-gpu=true -o name | sed 's#node/##' | sort))   # bash 3.2 safe
for n in "${REAL[@]}"; do
  kubectl label node "$n" --overwrite ksa/gpu-kind=real >/dev/null
  kubectl taint node "$n" nvidia.com/gpu=present:NoSchedule --overwrite >/dev/null
done

# ------------------------------------------------------------------ stand-in GPUs
STANDIN=($(kubectl get nodes -l '!ksa/real-gpu' -o name | sed 's#node/##' | sort))
BLOCKS=(block-a block-a block-b)
for i in "${!STANDIN[@]}"; do
  n="${STANDIN[$i]}"
  kubectl label node "$n" --overwrite ksa/gpu-kind=stand-in \
    nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 "network.topology/block=${BLOCKS[$i]}" >/dev/null
  kubectl patch node "$n" --subresource=status --type=json -p '[
    {"op":"add","path":"/status/capacity/nvidia.com~1gpu","value":"8"},
    {"op":"add","path":"/status/allocatable/nvidia.com~1gpu","value":"8"}]' >/dev/null
done

# ------------------------------------------------------------------ other teams' jobs
kubectl create namespace other-teams --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl create priorityclass other-teams-normal --value=10000 \
  --description="normal priority for other teams' jobs (same value as the advisor's research-normal)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

hold() {  # name node gpus
  cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: other-teams
spec:
  priorityClassName: other-teams-normal
  nodeSelector:
    kubernetes.io/hostname: $2
  tolerations:
  - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
  containers:
  - name: hold
    image: registry.k8s.io/pause:3.10
    resources:
      requests: {cpu: 10m, memory: 8Mi, nvidia.com/gpu: "$3"}
      limits: {nvidia.com/gpu: "$3"}
EOF
}
hold other-team-a "${REAL[0]}" 1
hold other-team-b "${REAL[1]}" 1
for i in "${!STANDIN[@]}"; do hold "standin-team-job-$i" "${STANDIN[$i]}" 2; done
kubectl -n other-teams wait --for=condition=Ready pod --all --timeout=180s >/dev/null
echo "cluster ready: 4 real A10G GPUs (2 held) and 3 stand-in nodes x 8 GPUs (2 held on each)"
