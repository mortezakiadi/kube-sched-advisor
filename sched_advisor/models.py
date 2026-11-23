"""Core data models: what the lab asks for, and what the cluster looks like."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

GPU = "nvidia.com/gpu"

# Labels that can tell us the GPU model, best first.
GPU_MODEL_LABELS = [
    "nvidia.com/gpu.product",                 # NVIDIA GPU Operator / GPU feature discovery
    "karpenter.k8s.aws/instance-gpu-name",    # Karpenter on EKS, e.g. "h100"
    "node.kubernetes.io/instance-type",       # always present on EKS, e.g. "p5.48xlarge"
]

# EC2 instance family -> GPU model (used when only the instance type is known)
EC2_GPU_FAMILIES = {
    "p6-b200": "B200", "p6e-gb200": "GB200", "p5en": "H200", "p5e": "H200", "p5": "H100",
    "p4de": "A100", "p4d": "A100", "g6e": "L40S", "g6": "L4", "g5": "A10G", "g4dn": "T4",
}


def gpu_model_of(label_value: str) -> str:
    """Normalize a GPU label value (product name, Karpenter gpu-name or EC2 instance type)."""
    v = (label_value or "").lower()
    family = v.split(".")[0]
    if family in EC2_GPU_FAMILIES:
        return EC2_GPU_FAMILIES[family]
    return v


class WorkloadKind(str, Enum):
    TRAINING = "training"          # distributed, all-or-nothing, long running
    INFERENCE = "inference"        # replicated service, availability matters
    BATCH = "batch"                # eval / data prep: many independent pods
    INTERACTIVE = "interactive"    # notebooks, debugging


class Priority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"                    # preemptible / spot-friendly


@dataclass
class WorkloadAsk:
    """A normalized request coming from an AI research team."""
    name: str
    kind: WorkloadKind
    replicas: int = 1                       # number of pods (workers / replicas)
    gpus_per_pod: int = 0
    cpu_per_pod: float = 1.0                # cores
    memory_gib_per_pod: float = 4.0
    gpu_model: Optional[str] = None         # e.g. "H100", "A100"
    namespace: str = "research"
    team: str = "research"
    priority: Priority = Priority.NORMAL
    gang: Optional[bool] = None             # None = infer from kind
    needs_fast_interconnect: bool = False   # NVLink / same rack / same block
    spread_across_zones: bool = False
    spot_ok: bool = False
    image: str = "ghcr.io/example/trainer:latest"
    command: list[str] = field(default_factory=list)   # empty = use the image's own entrypoint
    queue_fair_share: bool = False          # many teams share a GPU pool
    raw_text: str = ""

    @property
    def is_gang(self) -> bool:
        if self.gang is not None:
            return self.gang
        return self.kind == WorkloadKind.TRAINING and self.replicas > 1

    @property
    def total_gpus(self) -> int:
        return self.gpus_per_pod * self.replicas


@dataclass
class Taint:
    key: str
    value: str = ""
    effect: str = "NoSchedule"


@dataclass
class Node:
    name: str
    allocatable: dict[str, float]               # cpu (cores), memory (GiB), nvidia.com/gpu
    requested: dict[str, float] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    taints: list[Taint] = field(default_factory=list)
    images: list[str] = field(default_factory=list)

    def free(self, resource: str) -> float:
        return self.allocatable.get(resource, 0) - self.requested.get(resource, 0)


@dataclass
class ClusterSnapshot:
    nodes: list[Node]
    installed: list[str] = field(default_factory=list)   # e.g. ["kueue", "volcano", "jobset"]
    managed: Optional[str] = None                        # "eks" | "gke" | "aks" | None
    k8s_version: str = "1.34"
    zone_label: str = "topology.kubernetes.io/zone"
    gpu_model_label: str = "nvidia.com/gpu.product"
    interconnect_label: Optional[str] = None             # e.g. "network.topology/block"

    def has(self, component: str) -> bool:
        return component.lower() in (c.lower() for c in self.installed)
