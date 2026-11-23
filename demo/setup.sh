#!/usr/bin/env bash
# Prepare the demo cluster. Every GPU in it is real:
#
#   * the NVIDIA device plugin and GPU feature discovery are installed with helm, so the GPU
#     counts and the nvidia.com/gpu.product label come from real discovery
#   * the GPU nodes then get the usual nvidia.com/gpu taint that GPU pools carry
#   * "other teams" start two real pods that each hold the GPU of one node. That leaves 2 of the
#     4 GPUs free, on two different nodes.
#
# The other teams' pods carry a priority class with the same value as the advisor's "normal"
# class (10000). Pods with no priority at all would be preempted by the advisor's workloads,
# which is the advisor's documented way to free GPUs, but it is not the story told here.
#
# The held pods run a do-nothing image. They request GPUs, so the scheduler and the device
# plugin treat them exactly like a team's running job.
set -euo pipefail

echo "waiting for nodes..."
kubectl wait --for=condition=Ready node --all --timeout=300s >/dev/null

helm repo add nvdp https://nvidia.github.io/k8s-device-plugin >/dev/null 2>&1 || true
helm repo update nvdp >/dev/null
helm upgrade -i nvdp nvdp/nvidia-device-plugin --version 0.20.2 \
  --namespace nvidia-device-plugin --create-namespace --set gfd.enabled=true >/dev/null

echo "waiting for the GPUs to be discovered..."
for i in $(seq 1 90); do
  total=$(kubectl get nodes -l ksa/real-gpu=true -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' | awk '{s+=$1} END {print s+0}')
  labelled=$(kubectl get nodes -l ksa/real-gpu=true,nvidia.com/gpu.product=NVIDIA-A10G -o name | wc -l | tr -d ' ')
  [ "$total" = "4" ] && [ "$labelled" = "4" ] && break
  sleep 10
done
[ "$total" = "4" ] && [ "$labelled" = "4" ] || { echo "GPUs not discovered in time (total=$total labelled=$labelled)" >&2; exit 1; }

for n in $(kubectl get nodes -l ksa/real-gpu=true -o name | sed 's#node/##'); do
  kubectl taint node "$n" nvidia.com/gpu=present:NoSchedule --overwrite >/dev/null
done

SMALL=($(kubectl get nodes -l node.kubernetes.io/instance-type=g5.xlarge -o name | sed 's#node/##' | sort))   # bash 3.2 safe

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
kubectl create namespace other-teams --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl create priorityclass other-teams-normal --value=10000 \
  --description="normal priority for other teams' jobs (same value as the advisor's research-normal)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
hold other-team-a "${SMALL[0]}" 1
hold other-team-b "${SMALL[1]}" 1
kubectl -n other-teams wait --for=condition=Ready pod --all --timeout=180s >/dev/null
echo "cluster ready: 4 real A10G GPUs on 4 nodes, 2 held by other-teams, 2 free"
