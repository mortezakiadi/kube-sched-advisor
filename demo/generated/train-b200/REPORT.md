# Scheduling plan: `train-b200`

> train on 2 nodes with 8 B200 GPUs per node, all-or-nothing

**Recommended strategy:** Secondary scheduler `gang-scheduler` (scheduler-plugins Coscheduling + GPU bin-packing)

| field | value |
|---|---|
| kind | training |
| workers / replicas | 2 |
| per pod | 8 GPU, 96 CPU, 800 GiB |
| GPU model | B200 |
| gang (all-or-nothing) | True |
| fast interconnect | False |
| priority | normal |

## Why
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- No batch system is installed, so the lightest gang option is the Coscheduling plugin from kubernetes-sigs/scheduler-plugins. Pods wait at the Permit stage until `minMember` of the PodGroup can all be placed, then bind together.
- It runs as a **second** scheduler: the default scheduler is untouched (required on EKS, where the default scheduler's config cannot be edited).

## Findings
- **BLOCKER - No B200 nodes found.** GPU models in the cluster: NVIDIA-A10G, NVIDIA-H100-80GB-HBM3, Tesla-T4. Either relax the model or add a node pool (Karpenter NodePool / managed node group).
- **BLOCKER - No node can run even one pod.** `0/8 nodes are available: 8 Insufficient cpu.` Likely causes: requests larger than any node's free allocatable (requests, not real usage, are what count; right-size with VPA/Goldilocks).
- **WARNING - The gang cannot start right now under any scoring strategy.** With gang scheduling the job simply waits (holding nothing) instead of half-starting. To start it sooner: add capacity (Karpenter NodePool / node group for this GPU type), drain stragglers off partially-used nodes (descheduler), or preempt lower-priority work via PriorityClass.
- **NOTE - Coscheduling does not wake up when GPUs free up.** The plugin retries a waiting gang when a Pod or PodGroup is added or updated, or when the scheduler's periodic retry runs (every 5 minutes). Freed GPUs alone do not trigger a retry. To retry at once, touch the PodGroup, for example with `kubectl annotate podgroup <name> retry=$(date +%s) --overwrite`.
- **NOTE - EKS: default scheduler config is not editable.** That is why the weights live in a second scheduler selected via `schedulerName`.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "0/8 nodes are available: 8 Insufficient cpu.",
  "default_scheduler": {
    "placed": "0/2",
    "first_failure": "0/8 nodes are available: 8 Insufficient cpu."
  },
  "binpack_profile": {
    "placed": "0/2",
    "first_failure": "0/8 nodes are available: 8 Insufficient cpu."
  },
  "gpu_fragmentation": {
    "free_gpus": 25.0,
    "stranded_gpus": 6.0,
    "fully_free_nodes": 5,
    "stranded_ratio": 0.24
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
