# Demo transcript (real output, redacted)

# The cluster

_A real EKS cluster. All 4 GPUs are real NVIDIA A10G._

## 1. Four GPU nodes with one real NVIDIA A10G GPU each.

```console
$ kubectl get nodes -l ksa/real-gpu=true -o custom-columns='NODE:.metadata.name,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,GPUS:.status.allocatable.nvidia\.com/gpu,GPU-PRODUCT:.metadata.labels.nvidia\.com/gpu\.product'
NODE                                           TYPE        GPUS   GPU-PRODUCT
gpu-node-1                                     g5.xlarge   1      NVIDIA-A10G
gpu-node-2                                     g5.xlarge   1      NVIDIA-A10G
gpu-node-3                                     g5.xlarge   1      NVIDIA-A10G
gpu-node-4                                     g5.xlarge   1      NVIDIA-A10G
```

## 2. Other teams already run jobs on 2 of the 4 GPUs. Two are free, on two different nodes.

```console
$ kubectl get pods -n other-teams -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,GPUS-HELD:.spec.containers[0].resources.requests.nvidia\.com/gpu'
POD            NODE                                           GPUS-HELD
other-team-a   gpu-node-1                                     1
other-team-b   gpu-node-2                                     1
```

## 3. The advisor reads the live cluster through kubectl. Nothing is changed.

```console
$ python -m sched_advisor status
Kubernetes 1.37 on EKS
Installed add-ons: none
Network topology label: none
Node pools:
- 1x cpu-only in us-east-2c: 0 GPU/node, 0 GPUs free, 0 fully-free nodes, 1.575 CPU free, taints=none
- 1x NVIDIA-A10G (NVIDIA-A10G) in us-east-2c: 1 GPU/node, 0 GPUs free, 0 fully-free nodes, 3.545 CPU free, taints=['nvidia.com/gpu']
- 1x cpu-only in us-east-2a: 0 GPU/node, 0 GPUs free, 0 fully-free nodes, 1.775 CPU free, taints=none
- 1x NVIDIA-A10G (NVIDIA-A10G) in us-east-2a: 1 GPU/node, 0 GPUs free, 0 fully-free nodes, 3.755 CPU free, taints=['nvidia.com/gpu']
- 2x NVIDIA-A10G (NVIDIA-A10G) in us-east-2b: 1 GPU/node, 2 GPUs free, 2 fully-free nodes, 7.43 CPU free, taints=['nvidia.com/gpu']
- 1x cpu-only in us-east-2b: 0 GPU/node, 0 GPUs free, 0 fully-free nodes, 1.775 CPU free, taints=none
```

# Scenario 1: a training job that must start all at once

_3 workers, 1 GPU each. Only 2 GPUs are free._

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
plain-train-m2ffr   Running   gpu-node-4                                 
plain-train-tdb6r   Pending   <none>
plain-train-vbx45   Running   gpu-node-3                                  
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
deployment "gang-scheduler" successfully rolled out
```

## 8. Submit the job. All three workers wait, and none holds a GPU. Two are free, three are needed.

```console
$ kubectl apply -f out/gang-train/30-workload.yaml && sleep 25 && kubectl get pods -n research -o custom-columns='POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName'
service/gang-train created
job.batch/gang-train created
POD                  STATUS    NODE
gang-train-0-rpdkf   Pending   <none>
gang-train-1-zrkn5   Pending   <none>
gang-train-2-tvjng   Pending   <none>
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
gang-train-0-rpdkf   Pending   <none>
gang-train-1-zrkn5   Pending   <none>
gang-train-2-tvjng   Pending   <none>
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
[pod/gang-train-2-tvjng/main] GPU 0: NVIDIA A10G (UUID: GPU-<id>)
[pod/gang-train-0-rpdkf/main] GPU 0: NVIDIA A10G (UUID: GPU-<id>)
[pod/gang-train-1-zrkn5/main] GPU 0: NVIDIA A10G (UUID: GPU-<id>)
```

# Scenario 2: a highly available inference service

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
gpu-serve-6bb6f49bfb-btxss   Running   gpu-node-2                                  
gpu-serve-6bb6f49bfb-fsw8q   Running   gpu-node-3                                  
```

## 16. Inside a pod: the real GPU the scheduler gave it.

```console
$ kubectl exec -n research deploy/gpu-serve -- nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
name, memory.total [MiB], driver_version
NVIDIA A10G, 23028 MiB, 580.178.04
```

# Scenario 3: asking for hardware the cluster does not have

_The advisor should say so instead of generating a job that never starts._

## 17. An ask for B200 GPUs. This cluster has A10G GPUs only.

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

Simulation: default scheduler places 0/2 pods; 1 GPUs free, 1 whole nodes free

Manifests and the full report: out/train-b200/
```

> Omitted from the video: Installed Kueue v0.20.7 (output omitted from the video).

# Scenario 4: the platform team installs Kueue

_The advisor notices and changes its plan._

## 18. Kueue is installed on the cluster. The advisor detects it from the cluster's API extensions.

```console
$ kubectl get pods -n kueue-system && kubectl get crd clusterqueues.kueue.x-k8s.io
NAME                                        READY   STATUS    RESTARTS   AGE
kueue-controller-manager-5f58576dcf-7c2db   1/1     Running   0          13s
NAME                           SCOPE     VERSIONS           CREATED AT
clusterqueues.kueue.x-k8s.io   Cluster   v1beta2(storage)   2026-11-23T20:02:52Z
```

## 19. A shared-pool ask on the same cluster. The plan now uses Kueue queues.

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

## 20. Kueue is really installed, so its API server can validate the generated queue objects. Nothing is created.

```console
$ kubectl apply --dry-run=server -f out/gang-kueue/20-kueue.yaml -f out/gang-kueue/30-workload.yaml
resourceflavor.kueue.x-k8s.io/a10g created (server dry run)
clusterqueue.kueue.x-k8s.io/research-cq created (server dry run)
localqueue.kueue.x-k8s.io/research-queue created (server dry run)
service/gang-kueue created (server dry run)
job.batch/gang-kueue created (server dry run)
```

