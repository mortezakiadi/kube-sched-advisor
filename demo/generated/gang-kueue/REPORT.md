# Scheduling plan: `gang-kueue`

> 2 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing fine-tune, shared pool with other teams, 1 cpu and 1 GiB per pod

**Recommended strategy:** Kueue admission (all-or-nothing quota) + Topology-Aware Scheduling + `gpu-binpack` bin-packing profile

| field | value |
|---|---|
| kind | training |
| workers / replicas | 2 |
| per pod | 8 GPU, 1 CPU, 1 GiB |
| GPU model | H100 |
| gang (all-or-nothing) | True |
| fast interconnect | True |
| priority | normal |

## Why
- Pin to `nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3` so the pod only lands on H100 nodes.
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- Kueue keeps the Job suspended until quota for **all** workers is available, then admits it as a unit. No half-started jobs holding GPUs.
- A ClusterQueue per team inside a shared cohort gives fair sharing: idle quota is borrowable and reclaimed with preemption when its owner needs it back.
- Topology-Aware Scheduling: the pod-set annotation asks Kueue to place every worker inside one `network.topology/block` (required - simulation found block-a fits).
- `gpu-binpack` uses MostAllocated with `nvidia.com/gpu` weight 5: fill GPU nodes before touching empty ones, so whole nodes stay free for big jobs and idle nodes can be scaled down. Trade-off: larger blast radius per node, which is why it is a separate profile and services stay on the default scheduler.

## Findings
- **NOTE - EKS: default scheduler config is not editable.** That is why the weights live in a second scheduler selected via `schedulerName`.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "2/5 nodes are available.",
  "default_scheduler": {
    "placed": "2/2",
    "nodes": [
      "node-x",
      "node-x"
    ],
    "network_domains": [
      "block-a"
    ]
  },
  "binpack_profile": {
    "placed": "2/2",
    "nodes": [
      "node-x",
      "node-x"
    ],
    "network_domains": [
      "block-a"
    ]
  },
  "gpu_fragmentation": {
    "free_gpus": 22.0,
    "stranded_gpus": 6.0,
    "fully_free_nodes": 2,
    "stranded_ratio": 0.27
  },
  "interconnect_domains": {
    "label": "network.topology/block",
    "pods_that_fit_per_domain": {
      "block-a": 2,
      "block-b": 0
    },
    "fits_entirely_in": "block-a"
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
