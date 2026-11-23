# Demo transcript (real output, redacted)

# The cluster

_Real EKS. Two real A10G GPU nodes, three nodes with stand-in H100 capacity._

## 1. Five nodes. Two g5.xlarge nodes carry a real NVIDIA A10G each. Three t3.medium nodes advertise 8 stand-in GPUs.

```console
$ kubectl get nodes -o custom-columns='NODE:.metadata.name,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,GPUS:.status.allocatable.nvidia\.com/gpu,GPU-PRODUCT:.metadata.labels.nvidia\.com/gpu\.product,BLOCK:.metadata.labels.network\.topology/block'
NODE                                           TYPE        GPUS   GPU-PRODUCT             BLOCK
gpu-node-1                                     g5.xlarge   1      NVIDIA-A10G             <none>
node-1                                         t3.medium   8      NVIDIA-H100-80GB-HBM3   block-a
gpu-node-2                                     g5.xlarge   1      NVIDIA-A10G             <none>
node-2                                         t3.medium   8      NVIDIA-H100-80GB-HBM3   block-a
node-3                                         t3.medium   8      NVIDIA-H100-80GB-HBM3   block-b
```

## 2. Other teams already hold 2 stand-in GPUs on every H100 node. 18 are free, but no node has 8 free.

```console
$ kubectl get pods -n other-teams -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,GPUS-HELD:.spec.containers[0].resources.requests.nvidia\.com/gpu'
POD                NODE                                          GPUS-HELD
other-team-job-0   node-1                                        2
other-team-job-1   node-2                                        2
other-team-job-2   node-3                                        2
```

## 3. The advisor reads the live cluster through kubectl. Nothing is changed.

```console
$ python -m sched_advisor status
Kubernetes 1.37 on EKS
Installed add-ons: none
Network topology label: network.topology/block
Node pools:
- 1x NVIDIA-A10G (NVIDIA-A10G) in us-east-2b: 1 GPU/node, 1 GPUs free, 1 fully-free nodes, 3.655 CPU free, taints=['nvidia.com/gpu']
- 1x NVIDIA-H100-80GB-HBM3 (NVIDIA-H100-80GB-HBM3) in us-east-2b: 8 GPU/node, 6 GPUs free, 0 fully-free nodes, 1.565 CPU free, taints=none
- 1x NVIDIA-A10G (NVIDIA-A10G) in us-east-2c: 1 GPU/node, 1 GPUs free, 1 fully-free nodes, 3.565 CPU free, taints=['nvidia.com/gpu']
- 1x NVIDIA-H100-80GB-HBM3 (NVIDIA-H100-80GB-HBM3) in us-east-2c: 8 GPU/node, 6 GPUs free, 0 fully-free nodes, 1.765 CPU free, taints=none
- 1x NVIDIA-H100-80GB-HBM3 (NVIDIA-H100-80GB-HBM3) in us-east-2a: 8 GPU/node, 6 GPUs free, 0 fully-free nodes, 1.765 CPU free, taints=none
```

# Scenario 1: an all-or-nothing training job

_2 nodes x 8 H100, on a pool that other teams have fragmented._

## 4. The ask, in plain English: a 2-node, 8-GPU fine-tune that must start all at once.

```console
$ python -m sched_advisor suggest --brief --name gang-finetune "2 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing fine-tune, 1 cpu and 1 GiB per pod"
Recommended: Secondary scheduler gang-scheduler (scheduler-plugins Coscheduling + GPU bin-packing)

Why:
  - Pin to nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 so the pod only lands on H100 nodes.
  - Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only allows
    placement, it does not attract the pod - the nodeSelector/requests do that).
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
0/5 nodes are available: 5 Insufficient nvidia.com/gpu. preemption: 0/5 nodes are available: 2 Preemption is not helpful for scheduling, 3 No preemption victims found for incoming pod.
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
  - Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only allows
    placement, it does not attract the pod - the nodeSelector/requests do that).
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
70:      schedulerName: gang-scheduler
```

# Scenario 2: inference on real GPUs

_The two real A10G nodes. The plan is applied for real._

## 10. A serving ask on the real GPUs. The advisor sees the GPU nodes are tainted and plans for it.

```console
$ python -m sched_advisor suggest --brief --name gpu-serve --image nvidia/cuda:12.4.1-base-ubuntu22.04 --command "sleep infinity" "serve a chat model, 2 replicas, 1 A10G GPU each, highly available, 1 cpu and 1 GiB per pod"
Recommended: Default kube-scheduler + spreading (Deployment, topology spread, PDB)

Why:
  - Pin to nvidia.com/gpu.product=NVIDIA-A10G so the pod only lands on A10G nodes.
  - Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only allows
    placement, it does not attract the pod - the nodeSelector/requests do that).
  - Serving needs availability, not packing: keep the default LeastAllocated scoring so replicas
    spread out and a single node failure costs one replica.
  - Topology spread across topology.kubernetes.io/zone is hard (DoNotSchedule) because this is
    critical.
  - A PodDisruptionBudget keeps node drains/Karpenter consolidation from taking out several replicas
    at once.

Simulation: default scheduler places 2/2 pods; 2 GPUs free, 2 whole nodes free

Manifests and the full report: out/gpu-serve/
```

## 11. Apply the generated manifests for real. The only edits are the image and command given on the command line.

```console
$ kubectl apply -f out/gpu-serve/00-priorityclass.yaml -f out/gpu-serve/30-workload.yaml && kubectl rollout status -n research deploy/gpu-serve --timeout=300s
priorityclass.scheduling.k8s.io/research-normal created
deployment.apps/gpu-serve created
poddisruptionbudget.policy/gpu-serve created
Waiting for deployment "gpu-serve" rollout to finish: 0 of 2 updated replicas are available...
Waiting for deployment "gpu-serve" rollout to finish: 1 of 2 updated replicas are available...
deployment "gpu-serve" successfully rolled out
```

## 12. Both replicas are running, one on each real GPU node.

```console
$ kubectl get pods -n research -o custom-columns='POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName'
POD                          STATUS    NODE
gpu-serve-6bb6f49bfb-6bzv5   Running   gpu-node-2                                  
gpu-serve-6bb6f49bfb-lfc94   Running   gpu-node-1                                 
```

## 13. Inside the pod, the real GPU the scheduler gave it.

```console
$ kubectl exec -n research deploy/gpu-serve -- nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
name, memory.total [MiB], driver_version
NVIDIA A10G, 23028 MiB, 580.178.04
```

## 14. Both real GPUs are now taken. A batch ask for 4 more GPU workers, and the advisor says so.

```console
$ python -m sched_advisor suggest --brief --name evals "batch eval job with 4 workers, 1 A10G GPU each, 1 cpu and 1 GiB per pod"
Recommended: Indexed Job

Why:
  - Pin to nvidia.com/gpu.product=NVIDIA-A10G so the pod only lands on A10G nodes.
  - Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only allows
    placement, it does not attract the pod - the nodeSelector/requests do that).
  - Pods are independent (no all-reduce), so gang semantics are unnecessary; each index can retry on
    its own (backoffLimitPerIndex).

Findings:
  BLOCKER: No node can run even one pod.

Simulation: default scheduler places 0/4 pods; 0 GPUs free, 0 whole nodes free

Manifests and the full report: out/evals/
```

# Scenario 3: asking for hardware the cluster does not have

_The advisor should say so instead of generating a job that never starts._

## 15. An ask for B200 GPUs. This cluster has H100-class and A10G capacity only.

```console
$ python -m sched_advisor suggest --brief --name train-b200 "train on 2 nodes with 8 B200 GPUs per node, all-or-nothing"
Recommended: Secondary scheduler gang-scheduler (scheduler-plugins Coscheduling + GPU bin-packing)

Why:
  - Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only allows
    placement, it does not attract the pod - the nodeSelector/requests do that).
  - No batch system is installed, so the lightest gang option is the Coscheduling plugin from
    kubernetes-sigs/scheduler-plugins.
  - It runs as a second scheduler: the default scheduler is untouched (required on EKS, where the
    default scheduler's config cannot be edited).

Findings:
  BLOCKER: No B200 nodes found.
  BLOCKER: No node can run even one pod.
  WARNING: The gang cannot start right now under any scoring strategy.
  NOTE: EKS: default scheduler config is not editable.

Simulation: default scheduler places 0/2 pods; 22 GPUs free, 2 whole nodes free

Manifests and the full report: out/train-b200/
```

> Omitted from the video: Installed Kueue v0.20.7 (output omitted from the video).

# Scenario 4: the platform team installs Kueue

_The advisor notices and changes its plan._

## 16. Kueue is installed on the cluster. The advisor detects it from the cluster's API extensions.

```console
$ kubectl get pods -n kueue-system && kubectl get crd clusterqueues.kueue.x-k8s.io
NAME                                        READY   STATUS    RESTARTS   AGE
kueue-controller-manager-5f58576dcf-lssx4   1/1     Running   0          18s
NAME                           SCOPE     VERSIONS           CREATED AT
clusterqueues.kueue.x-k8s.io   Cluster   v1beta2(storage)   2026-11-23T16:48:34Z
```

## 17. A shared-pool ask on the same cluster. The plan now uses Kueue queues instead of a second scheduler.

```console
$ python -m sched_advisor suggest --brief --name gang-kueue "2 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing fine-tune, shared pool with other teams, 1 cpu and 1 GiB per pod"
Recommended: Kueue admission (all-or-nothing quota) + Topology-Aware Scheduling + gpu-binpack bin-
             packing profile

Why:
  - Pin to nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 so the pod only lands on H100 nodes.
  - Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only allows
    placement, it does not attract the pod - the nodeSelector/requests do that).
  - Kueue keeps the Job suspended until quota for all workers is available, then admits it as a
    unit.
  - A ClusterQueue per team inside a shared cohort gives fair sharing: idle quota is borrowable and
    reclaimed with preemption when its owner needs it back.
  - Topology-Aware Scheduling: the pod-set annotation asks Kueue to place every worker inside one
    network.topology/block (required - simulation found block-a fits).
  - gpu-binpack uses MostAllocated with nvidia.com/gpu weight 5: fill GPU nodes before touching
    empty ones, so whole nodes stay free for big jobs and idle nodes can be scaled down.

Findings:
  NOTE: EKS: default scheduler config is not editable.

Simulation: default scheduler places 2/2 pods; whole job fits in one network.topology/block:
            block-a; 22 GPUs free, 2 whole nodes free

Manifests and the full report: out/gang-kueue/
```

## 18. Kueue is really installed, so its API server can validate the generated queue objects. Nothing is created.

```console
$ kubectl apply --dry-run=server -f out/gang-kueue/20-kueue.yaml -f out/gang-kueue/30-workload.yaml
topology.kueue.x-k8s.io/gpu-network created (server dry run)
resourceflavor.kueue.x-k8s.io/h100 created (server dry run)
clusterqueue.kueue.x-k8s.io/research-cq created (server dry run)
localqueue.kueue.x-k8s.io/research-queue created (server dry run)
service/gang-kueue created (server dry run)
job.batch/gang-kueue created (server dry run)
```

