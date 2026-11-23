# Scheduling plan: `gang-train`

> train on 3 workers, 1 A10G GPU each, all-or-nothing, 1 cpu and 1 GiB per pod

**Recommended strategy:** Secondary scheduler `gang-scheduler` (scheduler-plugins Coscheduling + GPU bin-packing)

| field | value |
|---|---|
| kind | training |
| workers / replicas | 3 |
| per pod | 1 GPU, 1 CPU, 1 GiB |
| GPU model | A10G |
| gang (all-or-nothing) | True |
| fast interconnect | False |
| priority | normal |

## Why
- Pin to `nvidia.com/gpu.product=NVIDIA-A10G` so the pod only lands on A10G nodes.
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- No batch system is installed, so the lightest gang option is the Coscheduling plugin from kubernetes-sigs/scheduler-plugins. Pods wait at the Permit stage until `minMember` of the PodGroup can all be placed, then bind together.
- It runs as a **second** scheduler: the default scheduler is untouched (required on EKS, where the default scheduler's config cannot be edited).

## Findings
- **BLOCKER - Not enough free GPUs right now.** The ask needs 3 GPUs; 2 are free. Without a queue the pods sit Pending and hold partial capacity. Queue it (Kueue/Volcano) so it is admitted only when capacity exists, or let Karpenter/cluster-autoscaler add nodes.
- **WARNING - The gang cannot start right now under any scoring strategy.** With gang scheduling the job simply waits (holding nothing) instead of half-starting. To start it sooner: add capacity (Karpenter NodePool / node group for this GPU type), drain stragglers off partially-used nodes (descheduler), or preempt lower-priority work via PriorityClass.
- **WARNING - Partial placement would deadlock the job.** The default scheduler places pods one at a time: 2/3 workers would start and hold GPUs while waiting for the rest, which may never fit. Gang scheduling is required.
- **NOTE - Coscheduling does not wake up when GPUs free up.** The plugin retries a waiting gang when a Pod or PodGroup is added or updated, or when the scheduler's periodic retry runs (every 5 minutes). Freed GPUs alone do not trigger a retry. To retry at once, touch the PodGroup, for example with `kubectl annotate podgroup <name> retry=$(date +%s) --overwrite`.
- **NOTE - EKS: default scheduler config is not editable.** That is why the weights live in a second scheduler selected via `schedulerName`.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "2/7 nodes are available.",
  "default_scheduler": {
    "placed": "2/3",
    "nodes": [
      "node-x",
      "node-x"
    ],
    "first_failure": "0/7 nodes are available: 4 Insufficient nvidia.com/gpu, 3 node(s) didn't match Pod's node affinity/selector."
  },
  "binpack_profile": {
    "placed": "2/3",
    "nodes": [
      "node-x",
      "node-x"
    ],
    "first_failure": "0/7 nodes are available: 4 Insufficient nvidia.com/gpu, 3 node(s) didn't match Pod's node affinity/selector."
  },
  "gpu_fragmentation": {
    "free_gpus": 2.0,
    "stranded_gpus": 0.0,
    "fully_free_nodes": 2,
    "stranded_ratio": 0.0
  }
}
```

## Apply
```bash
kubectl apply -f 00-priorityclass.yaml
kubectl apply -f 10-scheduler.yaml
kubectl apply -f 20-podgroup.yaml
kubectl apply -f 30-workload.yaml
```

## Alternatives considered
- Kueue: queue + quota + all-or-nothing admission; best when many teams share GPUs.
- Volcano / KAI / YuniKorn: full batch schedulers replacing the scheduling path for batch pods (gang, queues, fair share, GPU sharing); heavier to operate.
- Kubernetes 1.35 alpha Workload API (native gang scheduling in kube-scheduler); behind a feature gate, not for production yet.
- Run the descheduler (e.g. HighNodeUtilization / RemovePodsViolatingNodeAffinity) to repack drifted pods; pair with Karpenter consolidation.
