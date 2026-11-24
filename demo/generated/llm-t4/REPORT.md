# Scheduling plan: `llm-t4`

> serve a large model, 1 replica with 4 T4 GPUs, 1 cpu and 1 GiB per pod

**Recommended strategy:** Default kube-scheduler + spreading (Deployment, topology spread, PDB)

| field | value |
|---|---|
| kind | inference |
| workers / replicas | 1 |
| per pod | 4 GPU, 1 CPU, 1 GiB |
| GPU model | T4 |
| gang (all-or-nothing) | False |
| fast interconnect | False |
| priority | normal |

## Why
- Pin to `nvidia.com/gpu.product=Tesla-T4` so the pod only lands on T4 nodes.
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- Serving needs availability, not packing: keep the default LeastAllocated scoring so replicas spread out and a single node failure costs one replica.
- Topology spread across `topology.kubernetes.io/zone` is soft (ScheduleAnyway).
- A PodDisruptionBudget keeps node drains/Karpenter consolidation from taking out several replicas at once.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "1/8 nodes are available.",
  "default_scheduler": {
    "placed": "1/1",
    "nodes": [
      "node-x"
    ],
    "network_domains": [
      "?"
    ]
  },
  "binpack_profile": {
    "placed": "1/1",
    "nodes": [
      "node-x"
    ],
    "network_domains": [
      "?"
    ]
  },
  "gpu_fragmentation": {
    "free_gpus": 4.0,
    "stranded_gpus": 0,
    "fully_free_nodes": 1,
    "stranded_ratio": 0.0
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
