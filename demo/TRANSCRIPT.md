# Demo transcript (real output, redacted)

## 1. A real Amazon EKS cluster with 3 nodes. Each node advertises 8 GPUs, in two network blocks.

```console
$ kubectl get nodes -o custom-columns='NODE:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu,NETWORK-BLOCK:.metadata.labels.network\.topology/block'
NODE                                           GPUS   NETWORK-BLOCK
node-1                                         8      block-a
node-2                                         8      block-a
node-3                                         8      block-b
```

## 2. Other teams already hold 2 GPUs on every node. 18 GPUs are free, but no node has 8 free.

```console
$ kubectl get pods -n other-teams -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,GPUS-HELD:.spec.containers[0].resources.requests.nvidia\.com/gpu'
POD                NODE                                           GPUS-HELD
other-team-job-0   node-1                                         2
other-team-job-1   node-2                                         2
other-team-job-2   node-3                                         2
```

## 3. The advisor reads the live cluster through kubectl. No snapshot file, nothing is changed.

```console
$ python -m sched_advisor status
Kubernetes 1.37 on EKS
Installed add-ons: none
Network topology label: network.topology/block
Node pools:
- 1x NVIDIA-H100-80GB-HBM3 (NVIDIA-H100-80GB-HBM3) in us-east-2b: 8 GPU/node, 6 GPUs free, 0 fully-free nodes, 1.67 CPU free, taints=none
- 1x NVIDIA-H100-80GB-HBM3 (NVIDIA-H100-80GB-HBM3) in us-east-2a: 8 GPU/node, 6 GPUs free, 0 fully-free nodes, 1.67 CPU free, taints=none
- 1x NVIDIA-H100-80GB-HBM3 (NVIDIA-H100-80GB-HBM3) in us-east-2c: 8 GPU/node, 6 GPUs free, 0 fully-free nodes, 1.57 CPU free, taints=none
```

## 4. The ask, in plain English: a 2-node, 8-GPU fine-tune that must start all at once.

```console
$ python -m sched_advisor suggest --brief --name gang-finetune "2 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing fine-tune, 1 cpu and 1 GiB per pod"
Recommended: Secondary scheduler gang-scheduler (scheduler-plugins Coscheduling + GPU bin-packing)

Why:
  - Pin to nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 so the pod only lands on H100 nodes.
  - No batch system is installed, so the lightest gang option is the Coscheduling plugin from
    kubernetes-sigs/scheduler-plugins.
  - It runs as a second scheduler: the default scheduler is untouched (required on EKS, where the
    default scheduler's config cannot be edited).

Findings:
  BLOCKER: No node can run even one pod.
  WARNING: Enough GPUs in total, but they are fragmented.
  WARNING: The gang cannot start right now under any scoring strategy.
  WARNING: No single network domain can host the whole job.
  NOTE: EKS: default scheduler config is not editable.

Simulation: default scheduler places 0/2 pods; no single network.topology/block has room for the
            whole job; 18 GPUs free, 0 whole nodes free

Manifests and the full report: out/gang-finetune/
```

## 5. Check against the real kube-scheduler: one pod that wants a whole node. Same verdict as the advisor.

```console
$ kubectl apply -f demo/probe-pod.yaml && sleep 8 && kubectl get pod train-probe && kubectl get events --field-selector involvedObject.name=train-probe,reason=FailedScheduling -o custom-columns=MESSAGE:.message --no-headers
pod/train-probe created
NAME          READY   STATUS    RESTARTS   AGE
train-probe   0/1     Pending   0          9s
0/3 nodes are available: 3 Insufficient nvidia.com/gpu. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod.
```

## 6. Now the other teams' jobs finish and two nodes become completely free.

```console
$ kubectl delete pod train-probe && kubectl delete pod -n other-teams other-team-job-0 other-team-job-1
pod "train-probe" deleted from default namespace
pod "other-team-job-0" deleted from other-teams namespace
pod "other-team-job-1" deleted from other-teams namespace
```

## 7. Same ask, new cluster state: both workers place, and the whole job fits inside one network block.

```console
$ python -m sched_advisor suggest --brief --name gang-finetune "2 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing fine-tune, 1 cpu and 1 GiB per pod"
Recommended: Secondary scheduler gang-scheduler (scheduler-plugins Coscheduling + GPU bin-packing)

Why:
  - Pin to nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 so the pod only lands on H100 nodes.
  - No batch system is installed, so the lightest gang option is the Coscheduling plugin from
    kubernetes-sigs/scheduler-plugins.
  - It runs as a second scheduler: the default scheduler is untouched (required on EKS, where the
    default scheduler's config cannot be edited).

Findings:
  NOTE: EKS: default scheduler config is not editable.

Simulation: default scheduler places 2/2 pods; whole job fits in one network.topology/block:
            block-a; 22 GPUs free, 2 whole nodes free

Manifests and the full report: out/gang-finetune/
```

## 8. It wrote manifests. Ask the real API server to validate them. Nothing is created (server dry run).

```console
$ ls out/gang-finetune && kubectl create namespace research && kubectl apply --dry-run=server -f out/gang-finetune/00-priorityclass.yaml -f out/gang-finetune/30-workload.yaml
00-priorityclass.yaml
10-scheduler.yaml
20-podgroup.yaml
30-workload.yaml
ask.json
REPORT.md
namespace/research created
priorityclass.scheduling.k8s.io/research-normal created (server dry run)
service/gang-finetune created (server dry run)
job.batch/gang-finetune created (server dry run)
```

## 9. The plan also writes a PodGroup so both pods start together, and points the Job at the second scheduler.

```console
$ cat out/gang-finetune/20-podgroup.yaml && grep -n schedulerName out/gang-finetune/30-workload.yaml
apiVersion: scheduling.x-k8s.io/v1alpha1
kind: PodGroup
metadata:
  name: gang-finetune
  namespace: research
spec:
  minMember: 2
  scheduleTimeoutSeconds: 600
66:      schedulerName: gang-scheduler
```

## 10. A different ask, different answer: inference should not use a gang scheduler.

```console
$ python -m sched_advisor suggest --brief --name chat-serve "serve a chat model, 3 replicas, 1 H100 GPU each, highly available, 1 cpu and 1 GiB per pod"
Recommended: Default kube-scheduler + spreading (Deployment, topology spread, PDB)

Why:
  - Pin to nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 so the pod only lands on H100 nodes.
  - Serving needs availability, not packing: keep the default LeastAllocated scoring so replicas
    spread out and a single node failure costs one replica.
  - Topology spread across topology.kubernetes.io/zone is hard (DoNotSchedule) because this is
    critical.
  - A PodDisruptionBudget keeps node drains/Karpenter consolidation from taking out several replicas
    at once.

Simulation: default scheduler places 3/3 pods; 22 GPUs free, 2 whole nodes free

Manifests and the full report: out/chat-serve/
```

