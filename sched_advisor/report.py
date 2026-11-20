"""Write the plan to disk: one YAML file per step + a README-style REPORT.md."""
from __future__ import annotations

import json
import os
import re
import textwrap

import yaml

from .advisor import Plan
from .parser import ask_to_json

_ICON = {"block": "BLOCKER", "warn": "WARNING", "info": "NOTE"}


def render_markdown(plan: Plan) -> str:
    a = plan.ask
    lines = [
        f"# Scheduling plan: `{a.name}`",
        "",
        f"> {a.raw_text.strip()}" if a.raw_text else "",
        "",
        f"**Recommended strategy:** {plan.strategy}",
        "",
        "| field | value |", "|---|---|",
        f"| kind | {a.kind.value} |",
        f"| workers / replicas | {a.replicas} |",
        f"| per pod | {a.gpus_per_pod} GPU, {a.cpu_per_pod:g} CPU, {a.memory_gib_per_pod:g} GiB |",
        f"| GPU model | {a.gpu_model or 'any'} |",
        f"| gang (all-or-nothing) | {a.is_gang} |",
        f"| fast interconnect | {a.needs_fast_interconnect} |",
        f"| priority | {a.priority.value} |",
        "",
        "## Why",
        *[f"- {r}" for r in plan.rationale],
        "",
    ]
    if plan.findings:
        lines += ["## Findings", *[f"- **{_ICON[f.severity]} - {f.title}.** {f.detail}" for f in plan.findings], ""]
    lines += ["## Simulation (filter -> score -> place, like kube-scheduler)", "```json",
              json.dumps(plan.simulation, indent=2), "```", "",
              "## Apply", "```bash", *[f"kubectl apply -f {f}" for f in sorted(plan.files)], "```", ""]
    if plan.alternatives:
        lines += ["## Alternatives considered", *[f"- {x}" for x in plan.alternatives], ""]
    return "\n".join(lines)


def write(plan: Plan, out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for fname, docs in plan.files.items():
        path = os.path.join(out_dir, fname)
        with open(path, "w") as fh:
            yaml.safe_dump_all(docs, fh, sort_keys=False)
        written.append(path)
    for fname, content in (("REPORT.md", render_markdown(plan)), ("ask.json", ask_to_json(plan.ask))):
        path = os.path.join(out_dir, fname)
        with open(path, "w") as fh:
            fh.write(content)
        written.append(path)
    return written


def _plain(text: str) -> str:
    return re.sub(r"[`*]", "", text)


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", _plain(text), maxsplit=1)[0]


def _placement_line(sim: dict) -> str | None:
    parts = []
    placed = (sim.get("default_scheduler") or {}).get("placed")
    if placed:
        parts.append(f"default scheduler places {placed} pods")
    dom = sim.get("interconnect_domains") or {}
    if dom:
        parts.append(f"whole job fits in one {dom['label']}: {dom['fits_entirely_in']}"
                     if dom.get("fits_entirely_in") else
                     f"no single {dom['label']} has room for the whole job")
    frag = sim.get("gpu_fragmentation") or {}
    if frag:
        parts.append(f"{frag['free_gpus']:g} GPUs free, {frag['fully_free_nodes']} whole nodes free")
    return "; ".join(parts) or None


def render_brief(plan: Plan, out_dir: str, width: int = 100) -> str:
    """Short, terminal-friendly summary: strategy, key reasons, findings, placement."""
    def wrap(prefix: str, text: str) -> str:
        return textwrap.fill(text, width, initial_indent=prefix, subsequent_indent=" " * len(prefix))

    lines = [wrap("Recommended: ", _plain(plan.strategy)), ""]
    lines += ["Why:", *[wrap("  - ", _first_sentence(r)) for r in plan.rationale], ""]
    if plan.findings:
        lines += ["Findings:", *[wrap("  ", f"{_ICON[f.severity]}: {_plain(f.title)}.") for f in plan.findings], ""]
    placement = _placement_line(plan.simulation)
    if placement:
        lines += [wrap("Simulation: ", placement), ""]
    lines.append(f"Manifests and the full report: {out_dir}/")
    return "\n".join(lines)
