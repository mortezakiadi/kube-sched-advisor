#!/usr/bin/env bash
# Delete the demo cluster and everything eksctl created for it.
set -euo pipefail
cd "$(dirname "$0")"
eksctl delete cluster -f cluster.yaml --wait
