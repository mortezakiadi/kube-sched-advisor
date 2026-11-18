# Scheduling plan: `llm-sft`

> Fine-tune a 70B model for the alignment team: 2 nodes x 8 H100 GPUs per node, deepspeed, tensor parallel over NVLink, shared GPU pool with other teams

**Recommended strategy:** Kueue admission (all-or-nothing quota) + Topology-Aware Scheduling + `gpu-binpack` bin-packing profile

| field | value |
|---|---|
| kind | training |
| workers / replicas | 2 |
| per pod | 8 GPU, 96 CPU, 800 GiB |
| GPU model | H100 |
| gang (all-or-nothing) | True |
| fast interconnect | True |
| priority | normal |

## Why
- Pin to `nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3` so the pod only lands on H100 nodes.
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- Kueue keeps the Job suspended until quota for **all** workers is available, then admits it as a unit. No half-started jobs holding GPUs.
- A ClusterQueue per team inside a shared cohort gives fair sharing: idle quota is borrowable and reclaimed with preemption when its owner needs it back.
- Topology-Aware Scheduling: the pod-set annotation asks Kueue to place every worker inside one `network.topology/block` (preferred - no single domain fits today).
- `gpu-binpack` uses MostAllocated with `nvidia.com/gpu` weight 5: fill GPU nodes before touching empty ones, so whole nodes stay free for big jobs and idle nodes can be scaled down. Trade-off: larger blast radius per node, which is why it is a separate profile and services stay on the default scheduler.

## Findings
- **WARNING - Default placement splits the job across network domains.** LeastAllocated spread the workers over ['block-a', 'block-b']. NCCL all-reduce then crosses the slowest link. The scheduler sees GPU *counts*, not NVLink/IB topology, so this must be requested explicitly (Kueue Topology-Aware Scheduling or JobSet exclusive topology).
- **WARNING - No single network domain can host the whole job.** Capacity per `network.topology/block`: {'block-a': 1, 'block-b': 1}. Use a *preferred* topology (best effort) or reduce the number of workers.
- **NOTE - EKS: default scheduler config is not editable.** That is why the weights live in a second scheduler selected via `schedulerName`.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "2/12 nodes are available.",
  "default_scheduler": {
    "placed": "2/2",
    "nodes": [
      "h100-a-2",
      "h100-b-1"
    ],
    "network_domains": [
      "block-a",
      "block-b"
    ]
  },
  "binpack_profile": {
    "placed": "2/2",
    "nodes": [
      "h100-a-2",
      "h100-b-1"
    ],
    "network_domains": [
      "block-a",
      "block-b"
    ]
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
kubectl apply -f 20-kueue.yaml
kubectl apply -f 30-workload.yaml
```

## Alternatives considered
- Volcano / KAI / YuniKorn: full batch schedulers replacing the scheduling path for batch pods (gang, queues, fair share, GPU sharing); heavier to operate.
- scheduler-plugins Coscheduling (PodGroup + minMember) as a lightweight second scheduler.
- Run the descheduler (e.g. HighNodeUtilization / RemovePodsViolatingNodeAffinity) to repack drifted pods; pair with Karpenter consolidation.
