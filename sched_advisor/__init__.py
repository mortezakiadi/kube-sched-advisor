"""kube-sched-advisor: turn AI-lab workload asks into Kubernetes scheduling plans."""
from .advisor import Plan, advise
from .parser import parse

__all__ = ["advise", "parse", "Plan"]
__version__ = "0.1.0"
