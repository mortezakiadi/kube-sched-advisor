"""kube-sched-advisor CLI.

  # one-shot, reads the current cluster through kubectl (default)
  python -m sched_advisor suggest "I need 4 H100 nodes for a pretraining run, NVLink, all-or-nothing"

  # interactive: keep asking in plain English
  python -m sched_advisor chat

  # offline, against a saved snapshot
  python -m sched_advisor suggest --cluster examples/cluster.yaml "..."
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from . import cluster as C
from .advisor import advise
from .parser import ask_to_json, parse
from .report import render_markdown, write


def _snapshot(args):
    return C.load_yaml(args.cluster) if args.cluster else C.load_live(args.context)


def _run(text: str, snap, args, name: str | None = None) -> None:
    ask = parse(text, name=name, use_llm=args.llm, cluster_summary=C.summarize(snap))
    plan = advise(ask, snap)
    out = os.path.join(args.out, ask.name)
    paths = write(plan, out)
    print(render_markdown(plan))
    print("\nWrote:\n  " + "\n  ".join(paths))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sched-advisor", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cluster", help="saved snapshot YAML (default: read live cluster via kubectl)")
    common.add_argument("--context", help="kubectl context")
    common.add_argument("--llm", action=argparse.BooleanOptionalAction, default=None,
                        help="translate with Claude (default: on when ANTHROPIC_API_KEY is set)")
    common.add_argument("--out", default="out")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("suggest", parents=[common], help="plain-English ask -> plan + manifests")
    s.add_argument("ask", nargs="?", help="the request (or pipe it on stdin)")
    s.add_argument("--name")
    sub.add_parser("chat", parents=[common], help="interactive session")
    sub.add_parser("status", parents=[common], help="show what the advisor sees in the cluster")
    pa = sub.add_parser("parse", parents=[common], help="only show how the ask is understood")
    pa.add_argument("ask")

    args = p.parse_args(argv)
    snap = _snapshot(args)

    if args.cmd == "status":
        print(C.summarize(snap))
    elif args.cmd == "parse":
        print(ask_to_json(parse(args.ask, use_llm=args.llm, cluster_summary=C.summarize(snap))))
    elif args.cmd == "suggest":
        _run(args.ask or sys.stdin.read(), snap, args, args.name)
    else:  # chat
        print(C.summarize(snap))
        print("\nDescribe the workload in plain English (`refresh` re-reads the cluster, `quit` exits).")
        while True:
            try:
                text = input("\nask> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if re.fullmatch(r"quit|exit|q", text):
                break
            if text == "refresh":
                snap = _snapshot(args)
                print(C.summarize(snap))
                continue
            _run(text, snap, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
