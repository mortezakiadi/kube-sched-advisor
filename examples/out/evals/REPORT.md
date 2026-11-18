# Scheduling plan: `evals`

> Run an evaluation sweep: 20 pods with 1 GPU each, spot is fine, we checkpoint

**Recommended strategy:** Indexed Job on `gpu-binpack` (MostAllocated, GPU weight 5)

| field | value |
|---|---|
| kind | batch |
| workers / replicas | 20 |
| per pod | 1 GPU, 12 CPU, 100 GiB |
| GPU model | any |
| gang (all-or-nothing) | False |
| fast interconnect | False |
| priority | low |

## Why
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- Pods are independent (no all-reduce), so gang semantics are unnecessary; each index can retry on its own (`backoffLimitPerIndex`).
- `gpu-binpack` uses MostAllocated with `nvidia.com/gpu` weight 5: fill GPU nodes before touching empty ones, so whole nodes stay free for big jobs and idle nodes can be scaled down. Trade-off: larger blast radius per node, which is why it is a separate profile and services stay on the default scheduler.
- Low priority with `preemptionPolicy: Never`: the job never evicts others, and can be preempted itself - make sure it checkpoints.

## Findings
- **NOTE - EKS: default scheduler config is not editable.** That is why the weights live in a second scheduler selected via `schedulerName`.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "10/12 nodes are available.",
  "default_scheduler": {
    "placed": "20/20",
    "nodes": [
      "h100-a-2",
      "h100-a-2",
      "h100-a-1",
      "h100-a-2",
      "h100-a-1",
      "h100-a-2",
      "h100-a-0",
      "h100-a-1",
      "h100-a-2",
      "h100-a-0",
      "h100-a-1",
      "h100-a-2",
      "h100-b-1",
      "h100-a-0",
      "h100-a-1",
      "h100-a-2",
      "h100-a-3",
      "a100-0",
      "a100-1",
      "h100-b-1"
    ],
    "network_domains": [
      "?",
      "block-a",
      "block-b"
    ]
  },
  "binpack_profile": {
    "placed": "20/20",
    "nodes": [
      "h100-a-3",
      "h100-a-3",
      "h100-a-0",
      "h100-a-0",
      "h100-a-0",
      "h100-a-0",
      "h100-a-1",
      "h100-a-1",
      "h100-a-1",
      "h100-a-1",
      "h100-a-1",
      "h100-a-1",
      "h100-b-2",
      "h100-b-2",
      "h100-b-2",
      "h100-a-2",
      "h100-a-2",
      "h100-a-2",
      "h100-a-2",
      "h100-a-2"
    ],
    "network_domains": [
      "block-a",
      "block-b"
    ]
  },
  "gpu_fragmentation": {
    "free_gpus": 59.0,
    "stranded_gpus": 27.0,
    "fully_free_nodes": 4,
    "stranded_ratio": 0.46
  }
}
```

## Apply
```bash
kubectl apply -f 00-priorityclass.yaml
kubectl apply -f 10-scheduler.yaml
kubectl apply -f 30-workload.yaml
```

## Alternatives considered
- Kueue: queue + quota + all-or-nothing admission; best when many teams share GPUs.
- Volcano / KAI / YuniKorn: full batch schedulers replacing the scheduling path for batch pods (gang, queues, fair share, GPU sharing); heavier to operate.
- scheduler-plugins Coscheduling (PodGroup + minMember) as a lightweight second scheduler.
- Run the descheduler (e.g. HighNodeUtilization / RemovePodsViolatingNodeAffinity) to repack drifted pods; pair with Karpenter consolidation.
