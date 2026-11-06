"""Extend the audited public-store filter without rolling unrelated applications."""

import argparse
import importlib.util
import json
from pathlib import Path


PROFILES = {
    "stash": ("usr/lib/python3.12/site-packages/pip/_vendor/certifi",),
    "syncthing": (),
}


def load_guard():
    spec = importlib.util.spec_from_file_location(
        "public_ca_guard", Path(__file__).with_name("ca-overlay.py")
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.PACKAGES.update(PROFILES)
    for profile in PROFILES:
        guard.NATIVE_TREES[profile] = ("etc/ssl", *guard.TREES[1:])
    return guard


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--app", choices=PROFILES, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    guard = load_guard()
    print(json.dumps(guard.harden(
        args.root, args.app, guard.policy_keys(args.policy), args.verify_only
    )))
