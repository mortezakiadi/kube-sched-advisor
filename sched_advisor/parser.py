"""Turn a free-text ask from a research team into a WorkloadAsk.

Two parsers:
  * parse_rule_based  - offline, regex heuristics, deterministic (default).
  * parse_with_llm    - uses Claude tool-use to extract the same schema when
                        `anthropic` is installed and ANTHROPIC_API_KEY is set.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import fields

from .models import Priority, WorkloadAsk, WorkloadKind

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "eight": 8, "ten": 10, "twelve": 12, "sixteen": 16, "thirty-two": 32,
              "sixty-four": 64}
_GPU_MODELS = ["GB200", "B200", "H200", "H100", "A100", "L40S", "L4", "A10G", "T4", "MI300X"]


def _num(token: str) -> int:
    token = token.lower()
    return int(token) if token.isdigit() else _NUM_WORDS.get(token, 0)


_N = r"(\d+|" + "|".join(_NUM_WORDS) + r")"


_STOP = {"a", "an", "the", "for", "of", "to", "with", "and", "i", "we", "need", "want", "please", "run", "our",
         "my", "on", "in", "team", "can", "you", "job", "some", "that", "is"}


def _slug(text: str) -> str:
    words = [w for w in re.findall(r"[a-z][a-z0-9]+", text.lower()) if w not in _STOP][:3]
    return "-".join(words) or "workload"


def parse_rule_based(text: str, name: str | None = None) -> WorkloadAsk:
    t = text.lower()

    # --- kind -------------------------------------------------------------
    if re.search(r"\b(serve|serving|inference|endpoint|api|latency|qps)\b", t):
        kind = WorkloadKind.INFERENCE
    elif re.search(r"\b(notebook|jupyter|interactive|debug)\b", t):
        kind = WorkloadKind.INTERACTIVE
    elif re.search(r"\b(eval|evaluation|sweep|hyperparameter|batch|preprocess|embedding)\b", t) \
            and not re.search(r"\b(pre-?train|fine-?tun|train)", t):
        kind = WorkloadKind.BATCH
    else:
        kind = WorkloadKind.TRAINING

    # --- shape ------------------------------------------------------------
    nodes = re.search(_N + r"\s*(?:x\s*)?(?:gpu\s+)?(?:nodes|workers|pods|replicas|hosts)", t)
    gpn = re.search(_N + r"\s*(?:x\s*)?(?:[a-z0-9]+\s+)?gpus?\s*(?:per|each|/)\s*(?:node|worker|pod|replica|host)", t)
    models = "|".join(m.lower() for m in _GPU_MODELS)
    total = (re.search(_N + r"\s*(?:x\s*)?(?:" + models + r")?\s*gpus?\b", t)
             or re.search(_N + r"\s*(?:x\s*)?(?:" + models + r")s?\b", t))

    replicas = _num(nodes.group(1)) if nodes else 0
    gpus_per_pod = _num(gpn.group(1)) if gpn else 0
    if not gpus_per_pod and total:
        tot = _num(total.group(1))
        if replicas:
            gpus_per_pod = max(1, tot // replicas)
        elif tot > 8:                       # e.g. "64 H100s" -> 8 nodes x 8 GPUs
            gpus_per_pod, replicas = 8, tot // 8
        else:
            gpus_per_pod = tot
    replicas = replicas or 1
    if kind != WorkloadKind.BATCH and re.search(r"\bcpu[- ]only\b", t):
        gpus_per_pod = 0

    gpu_model = next((m for m in _GPU_MODELS if re.search(m.lower() + r"s?\b", t)), None)

    cpu = re.search(r"(\d+)\s*(?:cpu|cores|vcpu)", t)
    mem = re.search(r"(\d+)\s*(?:gi|gib|gb)\b", t)

    # --- intent flags -----------------------------------------------------
    if re.search(r"\b(urgent|critical|deadline|production|prod)\b", t):
        prio = Priority.CRITICAL if kind == WorkloadKind.INFERENCE else Priority.HIGH
    elif re.search(r"\b(spot|preemptible|low priority|best[- ]effort|whenever)\b", t):
        prio = Priority.LOW
    else:
        prio = Priority.NORMAL

    team = re.search(r"\bteam[: ]+([a-z0-9-]+)|([a-z0-9-]+)\s+team\b", t)

    return WorkloadAsk(
        name=name or _slug(text),
        kind=kind,
        replicas=replicas,
        gpus_per_pod=gpus_per_pod,
        cpu_per_pod=float(cpu.group(1)) if cpu else (max(4.0, 12.0 * gpus_per_pod) if gpus_per_pod else 2.0),
        memory_gib_per_pod=float(mem.group(1)) if mem else (max(16.0, 100.0 * gpus_per_pod) if gpus_per_pod else 8.0),
        gpu_model=gpu_model,
        team=(team.group(1) or team.group(2)) if team else "research",
        priority=prio,
        gang=True if re.search(r"all[- ]or[- ]nothing|gang|together|nccl|allreduce|fsdp|deepspeed|megatron", t) else None,
        needs_fast_interconnect=bool(re.search(r"nvlink|infiniband|\bib\b|same rack|rdma|low[- ]latency interconnect|tensor parallel|topology", t)),
        spread_across_zones=bool(re.search(r"highly available|\bha\b|across zones|multi[- ]zone|zone failure", t))
                            or (kind == WorkloadKind.INFERENCE and prio == Priority.CRITICAL),
        spot_ok=bool(re.search(r"\bspot|preemptible|checkpoint", t)),
        queue_fair_share=bool(re.search(r"quota|fair[- ]share|shared pool|other teams|multiple teams|queue", t)),
        raw_text=text,
    )


# ---------------------------------------------------------------------------
# Optional: LLM extraction with Claude tool use
# ---------------------------------------------------------------------------
_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "dns-1123 name for the workload"},
        "kind": {"type": "string", "enum": [k.value for k in WorkloadKind]},
        "replicas": {"type": "integer", "minimum": 1},
        "gpus_per_pod": {"type": "integer", "minimum": 0},
        "cpu_per_pod": {"type": "number"},
        "memory_gib_per_pod": {"type": "number"},
        "gpu_model": {"type": ["string", "null"]},
        "team": {"type": "string"},
        "priority": {"type": "string", "enum": [p.value for p in Priority]},
        "gang": {"type": ["boolean", "null"]},
        "needs_fast_interconnect": {"type": "boolean"},
        "spread_across_zones": {"type": "boolean"},
        "spot_ok": {"type": "boolean"},
        "queue_fair_share": {"type": "boolean"},
    },
    "required": ["name", "kind", "replicas", "gpus_per_pod"],
}


def parse_with_llm(text: str, cluster_summary: str | None = None,
                   model: str = "claude-sonnet-5-5") -> WorkloadAsk:
    import anthropic  # optional dependency

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        tools=[{"name": "record_workload", "description": "Record the normalized Kubernetes workload request.",
                "input_schema": _SCHEMA}],
        tool_choice={"type": "tool", "name": "record_workload"},
        system=("You translate requests from ML researchers into a precise Kubernetes workload spec. "
                "One pod = one node-sized worker for distributed training. Infer gang=true for "
                "multi-worker synchronous training. Use the current cluster state to fill gaps: if no GPU "
                "model is named, pick one that exists; size workers to whole nodes (gpus_per_pod = GPUs per node) "
                "for multi-node training; size cpu/memory to fit the node. Never pick a GPU model the cluster lacks "
                "unless the user explicitly named it."),
        messages=[{"role": "user", "content":
                   (f"<current_cluster_state>\n{cluster_summary}\n</current_cluster_state>\n\n" if cluster_summary else "")
                   + f"<request>\n{text}\n</request>"}],
    )
    data = next(b.input for b in resp.content if b.type == "tool_use")
    allowed = {f.name for f in fields(WorkloadAsk)}
    data = {k: v for k, v in data.items() if k in allowed}
    data["kind"] = WorkloadKind(data["kind"])
    if "priority" in data:
        data["priority"] = Priority(data["priority"])
    return WorkloadAsk(raw_text=text, **data)


def parse(text: str, name: str | None = None, use_llm: bool | None = None,
          cluster_summary: str | None = None) -> WorkloadAsk:
    if use_llm is None:
        use_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if use_llm:
        try:
            ask = parse_with_llm(text, cluster_summary)
            if name:
                ask.name = name
            return ask
        except Exception as exc:  # fall back silently to deterministic parser
            print(f"[advisor] LLM parse failed ({exc!r}); using rule-based parser")
    return parse_rule_based(text, name)


def ask_to_json(ask: WorkloadAsk) -> str:
    d = {f.name: getattr(ask, f.name) for f in fields(ask)}
    d["kind"], d["priority"] = ask.kind.value, ask.priority.value
    return json.dumps(d, indent=2)
