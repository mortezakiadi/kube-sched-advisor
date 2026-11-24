# Scheduling plan: `gang-kueue`

> train on 3 workers, 1 A10G GPU each, all-or-nothing, shared pool with other teams, 1 cpu and 1 GiB per pod

**Recommended strategy:** Kueue admission (all-or-nothing quota) + `gpu-binpack` bin-packing profile

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
- Kueue keeps the Job suspended until quota for **all** workers is available, then admits it as a unit. No half-started jobs holding GPUs.
- A ClusterQueue per team inside a shared cohort gives fair sharing: idle quota is borrowable and reclaimed with preemption when its owner needs it back.
- `gpu-binpack` uses MostAllocated with `nvidia.com/gpu` weight 5: fill GPU nodes before touching empty ones, so whole nodes stay free for big jobs and idle nodes can be scaled down. Trade-off: larger blast radius per node, which is why it is a separate profile and services stay on the default scheduler.

## Findings
- **NOTE - EKS: default scheduler config is not editable.** That is why the weights live in a second scheduler selected via `schedulerName`.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "3/8 nodes are available.",
  "default_scheduler": {
    "placed": "3/3",
    "nodes": [
      "node-x",
      "node-x",
      "node-x"
    ],
    "network_domains": [
      "?"
    ]
  },
  "binpack_profile": {
    "placed": "3/3",
    "nodes": [
      "node-x",
      "node-x",
      "node-x"
    ],
    "network_domains": [
      "?"
    ]
  },
  "gpu_fragmentation": {
    "free_gpus": 3.0,
    "stranded_gpus": 0.0,
    "fully_free_nodes": 3,
    "stranded_ratio": 0.0
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
- Kubernetes 1.35 alpha Workload API (native gang scheduling in kube-scheduler); behind a feature gate, not for production yet.
- Run the descheduler (e.g. HighNodeUtilization / RemovePodsViolatingNodeAffinity) to repack drifted pods; pair with Karpenter consolidation.
