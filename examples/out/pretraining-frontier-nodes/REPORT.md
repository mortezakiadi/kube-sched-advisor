# Scheduling plan: `pretraining-frontier-nodes`

> Pretraining run for the frontier team: 4 nodes with 8 H100 GPUs per node, FSDP with NCCL allreduce, needs NVLink/InfiniBand, deadline Friday

**Recommended strategy:** Secondary scheduler `gang-scheduler` (scheduler-plugins Coscheduling + GPU bin-packing)

| field | value |
|---|---|
| kind | training |
| workers / replicas | 4 |
| per pod | 8 GPU, 96 CPU, 800 GiB |
| GPU model | H100 |
| gang (all-or-nothing) | True |
| fast interconnect | True |
| priority | high |

## Why
- Pin to `nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3` so the pod only lands on H100 nodes.
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- No batch system is installed, so the lightest gang option is the Coscheduling plugin from kubernetes-sigs/scheduler-plugins. Pods wait at the Permit stage until `minMember` of the PodGroup can all be placed, then bind together.
- It runs as a **second** scheduler: the default scheduler is untouched (required on EKS, where the default scheduler's config cannot be edited).

## Findings
- **WARNING - Enough GPUs in total, but they are fragmented.** 43 GPUs are free, but 27 of them sit on partially-used nodes and each pod needs 8. This is the classic '0/N nodes are available: Insufficient nvidia.com/gpu' with idle GPUs on the dashboard. Bin-packing (MostAllocated with a high GPU weight) and the descheduler fix this over time.
- **WARNING - The gang cannot start right now under any scoring strategy.** With gang scheduling the job simply waits (holding nothing) instead of half-starting. To start it sooner: add capacity (Karpenter NodePool / node group for this GPU type), drain stragglers off partially-used nodes (descheduler), or preempt lower-priority work via PriorityClass.
- **WARNING - Partial placement would deadlock the job.** The default scheduler places pods one at a time: 2/4 workers would start and hold GPUs while waiting for the rest, which may never fit. Gang scheduling is required.
- **WARNING - No single network domain can host the whole job.** Capacity per `network.topology/block`: {'block-a': 1, 'block-b': 1}. Use a *preferred* topology (best effort) or reduce the number of workers.
- **NOTE - EKS: default scheduler config is not editable.** That is why the weights live in a second scheduler selected via `schedulerName`.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "2/12 nodes are available.",
  "default_scheduler": {
    "placed": "2/4",
    "nodes": [
      "h100-a-2",
      "h100-b-1"
    ],
    "network_domains": [
      "block-a",
      "block-b"
    ],
    "first_failure": "0/12 nodes are available: 8 Insufficient nvidia.com/gpu, 4 node(s) didn't match Pod's node affinity/selector."
  },
  "binpack_profile": {
    "placed": "2/4",
    "nodes": [
      "h100-a-2",
      "h100-b-1"
    ],
    "network_domains": [
      "block-a",
      "block-b"
    ],
    "first_failure": "0/12 nodes are available: 8 Insufficient nvidia.com/gpu, 4 node(s) didn't match Pod's node affinity/selector."
  },
  "gpu_fragmentation": {
    "free_gpus": 43.0,
    "stranded_gpus": 27.0,
    "fully_free_nodes": 2,
    "stranded_ratio": 0.63
  },
  "interconnect_domains": {
    "label": "network.topology/block",
    "pods_that_fit_per_domain": {
      "block-a": 1,
      "block-b": 1
    },
    "fits_entirely_in": null
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
- Run the descheduler (e.g. HighNodeUtilization / RemovePodsViolatingNodeAffinity) to repack drifted pods; pair with Karpenter consolidation.
