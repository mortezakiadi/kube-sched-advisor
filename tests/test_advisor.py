import json
import sys
import types
from pathlib import Path

import pytest

from sched_advisor import cluster as C
from sched_advisor.advisor import advise
from sched_advisor.models import WorkloadKind
from sched_advisor.parser import parse, parse_rule_based
from sched_advisor.simulator import SimPod, filter_nodes

EX = Path(__file__).parent.parent / "examples"


@pytest.fixture
def snap():
    return C.load_yaml(str(EX / "cluster.yaml"))


@pytest.fixture
def snap_kueue():
    return C.load_yaml(str(EX / "cluster-with-kueue.yaml"))


# ---------------------------------------------------------------- parsing
def test_parse_training():
    a = parse_rule_based("Pretrain on 4 nodes with 8 H100 GPUs per node, FSDP, NVLink")
    assert (a.kind, a.replicas, a.gpus_per_pod, a.gpu_model) == (WorkloadKind.TRAINING, 4, 8, "H100")
    assert a.is_gang and a.needs_fast_interconnect


def test_parse_total_gpus_split_into_nodes():
    a = parse_rule_based("train a model on 64 H100s")
    assert (a.replicas, a.gpus_per_pod) == (8, 8)


def test_parse_inference_and_batch():
    i = parse_rule_based("Production inference endpoint, 3 replicas with 1 A100 GPU each")
    assert i.kind == WorkloadKind.INFERENCE and i.spread_across_zones
    b = parse_rule_based("evaluation sweep: 20 pods with 1 GPU each, spot is fine")
    assert b.kind == WorkloadKind.BATCH and b.spot_ok and not b.is_gang


# -------------------------------------------------------------- simulator
def test_filter_message_matches_kube_scheduler_style(snap):
    pod = SimPod({"nvidia.com/gpu": 8}, {"nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3"}, ["nvidia.com/gpu"])
    msg = filter_nodes(pod, snap.nodes).message(len(snap.nodes))
    assert msg == "2/12 nodes are available."


# ---------------------------------------------------------------- advisor
def test_gang_without_addons_uses_coscheduling(snap):
    plan = advise(parse_rule_based("4 nodes x 8 H100 GPUs per node, NCCL allreduce"), snap)
    assert "Coscheduling" in plan.strategy
    assert "20-podgroup.yaml" in plan.files
    titles = {f.title for f in plan.findings}
    assert "Partial placement would deadlock the job" in titles
    assert "Enough GPUs in total, but they are fragmented" in titles


def test_gang_with_kueue_uses_tas(snap_kueue):
    plan = advise(parse_rule_based("2 nodes x 8 H100 GPUs per node, NVLink, shared pool"), snap_kueue)
    assert plan.strategy.startswith("Kueue")
    job = plan.files["30-workload.yaml"][1]
    assert job["spec"]["suspend"] is True
    ann = job["spec"]["template"]["metadata"]["annotations"]
    assert any(k.startswith("kueue.x-k8s.io/podset-") for k in ann)


def test_inference_blocked_by_spot_taint_is_explained(snap):
    plan = advise(parse_rule_based("prod inference, 3 replicas with 1 A100 GPU each"), snap)
    assert any(f.severity == "block" and "taint" in f.detail for f in plan.findings)
    assert plan.files["30-workload.yaml"][0]["kind"] == "Deployment"


def test_missing_gpu_model(snap):
    plan = advise(parse_rule_based("train on 2 nodes with 8 B200 GPUs per node"), snap)
    assert any(f.title.startswith("No B200") for f in plan.findings)


# ------------------------------------------------- live cluster (fake kubectl)
def test_load_live_reads_current_status(monkeypatch):
    node = {"metadata": {"name": "gpu-1", "labels": {"nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3",
                                                     "cloud.google.com/gce-topology-block": "b1"}},
            "spec": {"taints": [{"key": "nvidia.com/gpu", "effect": "NoSchedule"}]},
            "status": {"allocatable": {"cpu": "190000m", "memory": "2000Gi", "nvidia.com/gpu": "8"},
                       "images": [{"names": ["trainer:1"]}]}}
    pod = {"spec": {"nodeName": "gpu-1", "containers": [
        {"resources": {"requests": {"cpu": "500m", "memory": "1024Mi", "nvidia.com/gpu": "3"}}}]}}
    outputs = {"nodes": {"items": [node]}, "pods": {"items": [pod]},
               "version": {"serverVersion": {"major": "1", "minor": "33+", "gitVersion": "v1.33.4-gke.100"}}}

    def fake_check_output(cmd):
        key = "version" if "version" in cmd else cmd[cmd.index("get") + 1]
        return json.dumps(outputs[key]).encode()

    monkeypatch.setattr(C.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout="customresourcedefinition.apiextensions.k8s.io/clusterqueues.kueue.x-k8s.io\n"))
    s = C.load_live()
    n = s.nodes[0]
    assert n.free("nvidia.com/gpu") == 5 and n.allocatable["cpu"] == 190 and n.requested["memory"] == 1
    assert s.installed == ["kueue"] and s.managed == "gke" and s.k8s_version == "1.33"
    assert s.interconnect_label == "cloud.google.com/gce-topology-block"
    assert "5 GPUs free" in C.summarize(s)


# ------------------------------------------------- Claude translation (mocked)
def test_llm_translation_receives_cluster_state(monkeypatch, snap):
    seen = {}

    class FakeMessages:
        def create(self, **kw):
            seen.update(kw)
            block = types.SimpleNamespace(type="tool_use", input={
                "name": "big-run", "kind": "training", "replicas": 2, "gpus_per_pod": 8,
                "gpu_model": "H100", "priority": "high", "needs_fast_interconnect": True})
            return types.SimpleNamespace(content=[block])

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: types.SimpleNamespace(messages=FakeMessages())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    ask = parse("we need a big run on whatever fast GPUs are free", use_llm=True,
                cluster_summary=C.summarize(snap))
    assert ask.gpu_model == "H100" and ask.replicas == 2 and ask.is_gang
    assert "NVIDIA-H100-80GB-HBM3" in seen["messages"][0]["content"]


def test_brief_is_short_and_plain(snap):
    from sched_advisor.report import render_brief
    plan = advise(parse_rule_based("4 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing pretraining"), snap)
    text = render_brief(plan, "out/x")
    assert text.startswith("Recommended: ")
    assert "Findings:" in text and "WARNING:" in text
    assert "```" not in text and "**" not in text and "`" not in text
    assert max(len(line) for line in text.splitlines()) <= 100
    assert len(text.splitlines()) < 30
