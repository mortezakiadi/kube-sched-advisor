# Demo transcript (real output, redacted)

# The cluster

_4 real A10G GPUs, plus 3 stand-in nodes. Every node is labelled._

## 1. Seven GPU nodes. Four have one real A10G each. Three are stand-ins: CPU machines that advertise 8 GPUs and have none.

```console
$ kubectl get nodes -l nvidia.com/gpu.product -o custom-columns='NODE:.metadata.name,KIND:.metadata.labels.ksa/gpu-kind,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,GPUS:.status.allocatable.nvidia\.com/gpu,GPU-PRODUCT:.metadata.labels.nvidia\.com/gpu\.product'
NODE                                           KIND       TYPE        GPUS   GPU-PRODUCT
gpu-node-1                                     real       g5.xlarge   1      NVIDIA-A10G
standin-node-1                                 stand-in   t3.medium   8      NVIDIA-H100-80GB-HBM3
standin-node-2                                 stand-in   t3.medium   8      NVIDIA-H100-80GB-HBM3
gpu-node-2                                     real       g5.xlarge   1      NVIDIA-A10G
gpu-node-3                                     real       g5.xlarge   1      NVIDIA-A10G
standin-node-3                                 stand-in   t3.medium   8      NVIDIA-H100-80GB-HBM3
gpu-node-4                                     real       g5.xlarge   1      NVIDIA-A10G
```

## 2. Other teams run jobs on both kinds of node: 1 GPU on two real nodes, and 2 of the 8 stand-in GPUs on each stand-in node.

```console
$ kubectl get pods -n other-teams -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,GPUS-HELD:.spec.containers[0].resources.requests.nvidia\.com/gpu'
POD                  NODE                                           GPUS-HELD
other-team-a         gpu-node-1                                     1
other-team-b         gpu-node-2                                     1
standin-team-job-0   standin-node-1                                 2
standin-team-job-1   standin-node-2                                 2
standin-team-job-2   standin-node-3                                 2
```

## 3. The advisor reads the live cluster through kubectl. Nothing is changed.

```console
$ python -m sched_advisor status
Kubernetes 1.37 on EKS
Installed add-ons: none
Network topology label: network.topology/block
Node pools:
- 2x NVIDIA-A10G (NVIDIA-A10G) in us-east-2b: 1 GPU/node, 0 GPUs free, 0 fully-free nodes, 7.3 CPU free, taints=['nvidia.com/gpu']
- 1x NVIDIA-H100-80GB-HBM3 (NVIDIA-H100-80GB-HBM3) in us-east-2b: 8 GPU/node, 6 GPUs free, 0 fully-free nodes, 1.765 CPU free, taints=none
- 1x NVIDIA-H100-80GB-HBM3 (NVIDIA-H100-80GB-HBM3) in us-east-2a: 8 GPU/node, 6 GPUs free, 0 fully-free nodes, 1.765 CPU free, taints=none
- 2x NVIDIA-A10G (NVIDIA-A10G) in us-east-2c: 1 GPU/node, 2 GPUs free, 2 fully-free nodes, 7.23 CPU free, taints=['nvidia.com/gpu']
- 1x NVIDIA-H100-80GB-HBM3 (NVIDIA-H100-80GB-HBM3) in us-east-2c: 8 GPU/node, 6 GPUs free, 0 fully-free nodes, 1.765 CPU free, taints=none
```

# Scenario 1: a training job that must start all at once (real GPUs)

_Real GPUs. 3 workers, 1 GPU each. Only 2 real GPUs are free._

## 4. The ask, in plain English. The advisor warns that starting 2 of 3 workers would deadlock the job.

```console
$ python -m sched_advisor suggest --brief --name gang-train --image nvidia/cuda:12.4.1-base-ubuntu22.04 --command "nvidia-smi -L" "train on 3 workers, 1 A10G GPU each, all-or-nothing, 1 cpu and 1 GiB per pod"
Recommended: Secondary scheduler gang-scheduler (scheduler-plugins Coscheduling + GPU bin-packing)

Why:
  - Pin to nvidia.com/gpu.product=NVIDIA-A10G so the pod only lands on A10G nodes.
  - Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only allows
    placement, it does not attract the pod - the nodeSelector/requests do that).
  - No batch system is installed, so the lightest gang option is the Coscheduling plugin from
    kubernetes-sigs/scheduler-plugins.
  - It runs as a second scheduler: the default scheduler is untouched (required on EKS, where the
    default scheduler's config cannot be edited).

Findings:
  BLOCKER: Not enough free GPUs right now.
  WARNING: The gang cannot start right now under any scoring strategy.
  WARNING: Partial placement would deadlock the job.
  NOTE: Coscheduling does not wake up when GPUs free up.
  NOTE: EKS: default scheduler config is not editable.

Simulation: default scheduler places 2/3 pods; 2 GPUs free, 2 whole nodes free

Manifests and the full report: out/gang-train/
```

## 5. The same job on the default scheduler, for real: two workers start and hold GPUs. The third waits.

```console
$ kubectl apply -f demo/plain-job.yaml && sleep 15 && kubectl get pods -l job-name=plain-train -o custom-columns='POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName'
job.batch/plain-train created
POD                 STATUS    NODE
plain-train-bcmn2   Running   gpu-node-4                                  
plain-train-tmj5n   Running   gpu-node-3                                  
plain-train-tp9qp   Pending   <none>
```

## 6. Delete it. Those two GPUs were held for a job that could never finish.

```console
$ kubectl delete job plain-train --wait=true
job.batch "plain-train" deleted from default namespace
```

> Omitted from the video: Installed the PodGroup API from scheduler-plugins v0.34.7 (output omitted from the video).

## 7. Apply what the advisor generated: a second scheduler with the Coscheduling plugin, and a PodGroup for the job.

```console
$ kubectl create namespace research && kubectl apply -f out/gang-train/00-priorityclass.yaml -f out/gang-train/10-scheduler.yaml -f out/gang-train/20-podgroup.yaml && kubectl rollout status -n kube-system deploy/gang-scheduler --timeout=180s
namespace/research created
priorityclass.scheduling.k8s.io/research-normal created
serviceaccount/gang-scheduler created
clusterrolebinding.rbac.authorization.k8s.io/gang-scheduler-kube-scheduler created
clusterrolebinding.rbac.authorization.k8s.io/gang-scheduler-volume-scheduler created
rolebinding.rbac.authorization.k8s.io/gang-scheduler-auth-reader created
role.rbac.authorization.k8s.io/gang-scheduler-leader-election created
rolebinding.rbac.authorization.k8s.io/gang-scheduler-leader-election created
clusterrole.rbac.authorization.k8s.io/gang-scheduler-podgroups created
clusterrolebinding.rbac.authorization.k8s.io/gang-scheduler-podgroups created
configmap/gang-scheduler-config created
deployment.apps/gang-scheduler created
podgroup.scheduling.x-k8s.io/gang-train created
Waiting for deployment "gang-scheduler" rollout to finish: 0 of 2 updated replicas are available...
Waiting for deployment "gang-scheduler" rollout to finish: 1 of 2 updated replicas are available...
deployment "gang-scheduler" successfully rolled out
```

## 8. Submit the job. All three workers wait, and none holds a GPU. Two are free, three are needed.

```console
$ kubectl apply -f out/gang-train/30-workload.yaml && sleep 25 && kubectl get pods -n research -o custom-columns='POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName'
service/gang-train created
job.batch/gang-train created
POD                  STATUS    NODE
gang-train-0-xfjlp   Pending   <none>
gang-train-1-nwhnf   Pending   <none>
gang-train-2-cgqmf   Pending   <none>
```

## 9. Another team's job finishes and frees one GPU.

```console
$ kubectl delete pod -n other-teams other-team-b
pod "other-team-b" deleted from other-teams namespace
```

## 10. The gang scheduler retries on new pods and on a 5-minute timer, not when a GPU frees up. Still waiting.

```console
$ sleep 20 && kubectl get pods -n research -o custom-columns='POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName'
POD                  STATUS    NODE
gang-train-0-xfjlp   Pending   <none>
gang-train-1-nwhnf   Pending   <none>
gang-train-2-cgqmf   Pending   <none>
```

## 11. As the advisor's note says: touch the PodGroup and the scheduler retries at once.

```console
$ kubectl annotate podgroup -n research gang-train retry=$(date +%s) --overwrite
podgroup.scheduling.x-k8s.io/gang-train annotated
```

## 12. All three workers start together, each on its own real GPU.

```console
$ kubectl wait --for=condition=complete job/gang-train -n research --timeout=300s && kubectl logs -n research -l job-name=gang-train --prefix
job.batch/gang-train condition met
[pod/gang-train-0-xfjlp/main] GPU 0: NVIDIA A10G (UUID: GPU-<id>)
[pod/gang-train-1-nwhnf/main] GPU 0: NVIDIA A10G (UUID: GPU-<id>)
[pod/gang-train-2-cgqmf/main] GPU 0: NVIDIA A10G (UUID: GPU-<id>)
```

# Scenario 2: a highly available inference service (real GPUs)

_2 replicas, 1 GPU each. A different ask gets a different plan, applied for real._

## 13. A serving ask. No gang scheduler this time: the default scheduler, zone spread and a disruption budget.

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

Simulation: default scheduler places 2/2 pods; 3 GPUs free, 3 whole nodes free

Manifests and the full report: out/gpu-serve/
```

## 14. Apply the generated manifests for real. The only edits are the image and command given on the command line.

```console
$ kubectl apply -f out/gpu-serve/00-priorityclass.yaml -f out/gpu-serve/30-workload.yaml && kubectl rollout status -n research deploy/gpu-serve --timeout=300s
priorityclass.scheduling.k8s.io/research-normal configured
deployment.apps/gpu-serve created
poddisruptionbudget.policy/gpu-serve created
deployment "gpu-serve" successfully rolled out
```

## 15. Both replicas are running, each on its own node.

```console
$ kubectl get pods -n research -l app=gpu-serve -o custom-columns='POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName'
POD                          STATUS    NODE
gpu-serve-6bb6f49bfb-6fqk2   Running   gpu-node-2                                
gpu-serve-6bb6f49bfb-p4g4w   Running   gpu-node-4                                  
```

## 16. Inside a pod: the real GPU the scheduler gave it.

```console
$ kubectl exec -n research deploy/gpu-serve -- nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
name, memory.total [MiB], driver_version
NVIDIA A10G, 23028 MiB, 580.178.04
```

# Scenario 3: a big job on the stand-in H100 nodes

_2 nodes x 8 GPUs in one network block. Stand-in nodes show the scheduling decision, not a GPU workload._

## 17. The ask: 2 nodes with 8 H100 GPUs each. Other teams hold 2 stand-in GPUs on every node, so 18 are free but no node has 8.

```console
$ python -m sched_advisor suggest --brief --name gang-big "2 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing fine-tune, 1 cpu and 1 GiB per pod"
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
  NOTE: Coscheduling does not wake up when GPUs free up.
  NOTE: EKS: default scheduler config is not editable.

Simulation: default scheduler places 0/2 pods; no single network.topology/block has room for the
            whole job; 18 GPUs free, 0 whole nodes free

Manifests and the full report: out/gang-big/
```

## 18. The real kube-scheduler agrees: a pod that wants a whole 8-GPU node stays Pending.

```console
$ kubectl apply -f demo/probe-pod.yaml && sleep 8 && kubectl get pod train-probe && kubectl get events --field-selector involvedObject.name=train-probe,reason=FailedScheduling -o custom-columns=MESSAGE:.message --no-headers
pod/train-probe created
NAME          READY   STATUS    RESTARTS   AGE
train-probe   0/1     Pending   0          10s
0/7 nodes are available: 7 Insufficient nvidia.com/gpu. preemption: 0/7 nodes are available: 3 No preemption victims found for incoming pod, 4 Preemption is not helpful for scheduling.
```

## 19. Two stand-in nodes are freed.

```console
$ kubectl delete pod train-probe && kubectl delete pod -n other-teams standin-team-job-0 standin-team-job-1
pod "train-probe" deleted from default namespace
pod "standin-team-job-0" deleted from other-teams namespace
pod "standin-team-job-1" deleted from other-teams namespace
```

## 20. Same ask, new cluster state: both workers fit, inside one network block.

```console
$ python -m sched_advisor suggest --brief --name gang-big "2 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing fine-tune, 1 cpu and 1 GiB per pod"
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
  NOTE: Coscheduling does not wake up when GPUs free up.
  NOTE: EKS: default scheduler config is not editable.

Simulation: default scheduler places 2/2 pods; whole job fits in one network.topology/block:
            block-a; 22 GPUs free, 2 whole nodes free

Manifests and the full report: out/gang-big/
```

## 21. The real API server validates the generated manifests, including the PodGroup. Nothing is created.

```console
$ kubectl apply --dry-run=server -f out/gang-big/00-priorityclass.yaml -f out/gang-big/20-podgroup.yaml -f out/gang-big/30-workload.yaml
priorityclass.scheduling.k8s.io/research-normal configured (server dry run)
podgroup.scheduling.x-k8s.io/gang-big created (server dry run)
service/gang-big created (server dry run)
job.batch/gang-big created (server dry run)
```

# Scenario 4: asking for hardware the cluster does not have

_The advisor should say so instead of generating a job that never starts._

## 22. An ask for B200 GPUs. This cluster has A10G and H100-class capacity only.

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
  NOTE: Coscheduling does not wake up when GPUs free up.
  NOTE: EKS: default scheduler config is not editable.

Simulation: default scheduler places 0/2 pods; 23 GPUs free, 3 whole nodes free

Manifests and the full report: out/train-b200/
```

> Omitted from the video: Installed Kueue v0.20.7 (output omitted from the video).

# Scenario 5: the platform team installs Kueue

_The advisor notices and changes its plan._

## 23. Kueue is installed on the cluster. The advisor detects it from the cluster's API extensions.

```console
$ kubectl get pods -n kueue-system && kubectl get crd clusterqueues.kueue.x-k8s.io
NAME                                        READY   STATUS    RESTARTS   AGE
kueue-controller-manager-5f58576dcf-km4sv   1/1     Running   0          17s
NAME                           SCOPE     VERSIONS           CREATED AT
clusterqueues.kueue.x-k8s.io   Cluster   v1beta2(storage)   2026-11-24T00:12:02Z
```

## 24. A shared-pool ask on the same cluster. The plan now uses Kueue queues.

```console
$ python -m sched_advisor suggest --brief --name gang-kueue "train on 3 workers, 1 A10G GPU each, all-or-nothing, shared pool with other teams, 1 cpu and 1 GiB per pod"
Recommended: Kueue admission (all-or-nothing quota) + gpu-binpack bin-packing profile

Why:
  - Pin to nvidia.com/gpu.product=NVIDIA-A10G so the pod only lands on A10G nodes.
  - Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only allows
    placement, it does not attract the pod - the nodeSelector/requests do that).
  - Kueue keeps the Job suspended until quota for all workers is available, then admits it as a
    unit.
  - A ClusterQueue per team inside a shared cohort gives fair sharing: idle quota is borrowable and
    reclaimed with preemption when its owner needs it back.
  - gpu-binpack uses MostAllocated with nvidia.com/gpu weight 5: fill GPU nodes before touching
    empty ones, so whole nodes stay free for big jobs and idle nodes can be scaled down.

Findings:
  BLOCKER: Not enough free GPUs right now.
  WARNING: The gang cannot start right now under any scoring strategy.
  WARNING: Partial placement would deadlock the job.
  NOTE: EKS: default scheduler config is not editable.

Simulation: default scheduler places 1/3 pods; 1 GPUs free, 1 whole nodes free

Manifests and the full report: out/gang-kueue/
```

## 25. Kueue is really installed, so its API server can validate the generated queue objects. Nothing is created.

```console
$ kubectl apply --dry-run=server -f out/gang-kueue/20-kueue.yaml -f out/gang-kueue/30-workload.yaml
resourceflavor.kueue.x-k8s.io/a10g created (server dry run)
clusterqueue.kueue.x-k8s.io/research-cq created (server dry run)
localqueue.kueue.x-k8s.io/research-queue created (server dry run)
service/gang-kueue created (server dry run)
job.batch/gang-kueue created (server dry run)
```

