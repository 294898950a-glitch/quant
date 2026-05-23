#!/usr/bin/env python3
"""Start the Guangzhou spot via Tencent CVM API and wait for ssh ready.

Runs on the Singapore sig VM (proxy host). Credentials live in
``/root/.tencent_secrets/cvm.env`` (TENCENTCLOUD_SECRET_ID +
TENCENTCLOUD_SECRET_KEY + TENCENTCLOUD_REGION). The instance id is read from
the research_queue.yaml vm_hosts entry.

Usage on sig:
    source /root/.tencent_secrets/cvm.env
    python3 /root/projects/quant/scripts/start_spot.py [--wait-ssh]

Usage from WSL (the most common case — we want framework to be able to ask
sig to start spot):
    bash scripts/start_spot.sh        # see sibling script

Hard boundaries (per CLAUDE.md):
- Does not touch verifier / cost / baseline. Only CVM start.
- Spot stop continues to be handled by spot_idle_shutdown.py.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    from tencentcloud.common import credential
    from tencentcloud.cvm.v20170312 import cvm_client, models
except ImportError:
    print(
        "tencentcloud SDK not installed. Run:\n"
        "  pip install tencentcloud-sdk-python-common tencentcloud-sdk-python-cvm",
        file=sys.stderr,
    )
    sys.exit(2)


SECRETS_PATH = Path("/root/.tencent_secrets/cvm.env")


def load_secrets_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if SECRETS_PATH.exists():
        for line in SECRETS_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                k, v = line[len("export "):].split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "TENCENTCLOUD_REGION"):
        if os.environ.get(key):
            out[key] = os.environ[key]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", default="ins-5lb9zo12", help="Tencent CVM instance id")
    parser.add_argument("--region", default=None, help="Override TENCENTCLOUD_REGION")
    parser.add_argument("--wait", type=int, default=60, help="Max seconds to wait for RUNNING")
    args = parser.parse_args()

    env = load_secrets_env()
    sid = env.get("TENCENTCLOUD_SECRET_ID")
    skey = env.get("TENCENTCLOUD_SECRET_KEY")
    region = args.region or env.get("TENCENTCLOUD_REGION") or "ap-guangzhou"
    if not sid or not skey:
        print("missing TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY", file=sys.stderr)
        return 2

    cred = credential.Credential(sid, skey)
    client = cvm_client.CvmClient(cred, region)

    # Check current state first — don't double-start.
    dreq = models.DescribeInstancesRequest()
    dreq.InstanceIds = [args.instance_id]
    dresp = client.DescribeInstances(dreq)
    if not dresp.InstanceSet:
        print(f"instance {args.instance_id} not found", file=sys.stderr)
        return 1
    inst = dresp.InstanceSet[0]
    print(f"current state={inst.InstanceState} ip={inst.PublicIpAddresses}")
    if inst.InstanceState == "RUNNING":
        print("already running")
        return 0
    if inst.InstanceState not in {"STOPPED", "STARTING"}:
        # PENDING / TERMINATING / etc — not safe to issue StartInstances.
        print(f"refusing to start from state={inst.InstanceState}", file=sys.stderr)
        return 1

    if inst.InstanceState == "STOPPED":
        sreq = models.StartInstancesRequest()
        sreq.InstanceIds = [args.instance_id]
        sresp = client.StartInstances(sreq)
        print(f"StartInstances RequestId={sresp.RequestId}")

    # Poll until RUNNING.
    deadline = time.time() + args.wait
    while time.time() < deadline:
        time.sleep(5)
        dresp = client.DescribeInstances(dreq)
        inst = dresp.InstanceSet[0]
        print(f"  state={inst.InstanceState} ip={inst.PublicIpAddresses}")
        if inst.InstanceState == "RUNNING":
            return 0
    print(f"timed out waiting for RUNNING after {args.wait}s", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
