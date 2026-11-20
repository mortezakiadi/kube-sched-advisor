# Scheduling plan: `chat-serve`

> serve a chat model, 3 replicas, 1 H100 GPU each, highly available, 1 cpu and 1 GiB per pod

**Recommended strategy:** Default kube-scheduler + spreading (Deployment, topology spread, PDB)

| field | value |
|---|---|
| kind | inference |
| workers / replicas | 3 |
| per pod | 1 GPU, 1 CPU, 1 GiB |
| GPU model | H100 |
| gang (all-or-nothing) | False |
| fast interconnect | False |
| priority | normal |

## Why
- Pin to `nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3` so the pod only lands on H100 nodes.
- Serving needs availability, not packing: keep the default LeastAllocated scoring so replicas spread out and a single node failure costs one replica.
- Topology spread across `topology.kubernetes.io/zone` is **hard** (DoNotSchedule) because this is critical.
- A PodDisruptionBudget keeps node drains/Karpenter consolidation from taking out several replicas at once.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "3/3 nodes are available.",
  "default_scheduler": {
    "placed": "3/3",
    "nodes": [
      "node-x",
      "node-x",
      "node-x"
    ],
    "network_domains": [
      "block-a",
      "block-b"
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
      "block-a",
      "block-b"
    ]
  },
  "gpu_fragmentation": {
    "free_gpus": 22.0,
    "stranded_gpus": 6.0,
    "fully_free_nodes": 2,
    "stranded_ratio": 0.27
  }
}
```

## Apply
```bash
kubectl apply -f 00-priorityclass.yaml
kubectl apply -f 30-workload.yaml
```

## Alternatives considered
- Put inference on its own GPU node pool with a dedicated taint so training bin-packing never competes with it.
- If replicas need >1 GPU with NVLink (tensor parallel), request whole nodes (gpus = GPUs per node).
