#!/usr/bin/env python3
"""Build locally, then verify Kindle's exact runtime without executing its app."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "apps" / "kindle-sender" / "src"
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "kindle_owned_ca", ROOT / ".github" / "scripts" / "verify-owned-image-ca.py"
)
CA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CA)


def command(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        list(args), cwd=ROOT, text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    if result.returncode:
        if capture and result.stdout:
            print(result.stdout, file=sys.stderr)
        result.check_returncode()
    return result.stdout.strip() if capture else ""


def native_build_info(binary: Path) -> str:
    text = command("go", "version", "-m", str(binary), capture=True)
    if not text.splitlines()[0].endswith(": go1.27.3"):
        raise ValueError("Application binary was not compiled with pinned Go 1.27.3")
    for setting in ("CGO_ENABLED=1", "GOOS=linux", "GOARCH=amd64"):
        if setting not in text:
            raise ValueError(f"Native application build-info missing {setting}")
    if "\tpath\tgithub.com/blazepower/kindle-sender\n" not in text:
        raise ValueError("Wrong application module")
    declared = re.findall(
        r"^\s*([a-zA-Z0-9._/-]+)\s+(v[^\s]+)", (SOURCE / "go.mod").read_text(),
        flags=re.MULTILINE,
    )
    actual = dict(re.findall(r"^\s*dep\s+(\S+)\s+(\S+)", text, re.MULTILINE))
    if not declared or actual != dict(declared):
        raise ValueError("Application dependencies differ from maintained go.mod")
    return text


def verify(output: Path) -> dict:
    if not output.resolve().is_relative_to(ROOT):
        raise ValueError("Acceptance proof must remain inside the worktree")
    output.unlink(missing_ok=True)
    workspace = ROOT / f".kindle-ca-check-{uuid.uuid4().hex}"
    workspace.mkdir()
    containers: list[str] = []
    try:
        images = {}
        for target in ("final", "kindle-ca-acceptance"):
            iid = workspace / f"{target}.iid"
            args = [
                "docker", "buildx", "build", "--load", "--pull",
                "--platform", "linux/amd64", "--build-context",
                f"kindle-policy={ROOT / '.github' / 'security'}",
                "--target", target, "--iidfile", str(iid),
            ]
            if target == "final":
                args.append("--no-cache")
            command(*args, str(SOURCE))
            image = iid.read_text().strip()
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                raise ValueError("Build did not return an immutable local image ID")
            images[target] = image
            platform = command(
                "docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}",
                image, capture=True,
            )
            if platform != "linux/amd64":
                raise ValueError("Wrong image platform")
            container = command(
                "docker", "create", "--network", "none", "--tmpfs", "/data",
                "--entrypoint",
                "/__never_started__", image, capture=True,
            )
            if not re.fullmatch(r"[a-f0-9]{64}", container):
                raise ValueError("Docker returned an invalid container ID")
            containers.append(container)
            for path, name in (
                ("/app/kindle-sender", "binary"),
                ("/etc/ssl/certs/ca-certificates.crt", "bundle"),
            ):
                command("docker", "cp", "-L", f"{container}:{path}", str(workspace / f"{target}-{name}"))
        for name in ("binary", "bundle"):
            if (workspace / f"final-{name}").read_bytes() != (workspace / f"kindle-ca-acceptance-{name}").read_bytes():
                raise ValueError(f"Acceptance and production {name} differ")
        count = CA.check_bundle((workspace / "final-bundle").read_bytes())
        build_info = native_build_info(workspace / "final-binary")
        native_tests = command(
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--tmpfs", "/data:rw,noexec,nosuid,size=1m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--memory", "256m", "--cpus", "2",
            images["kindle-ca-acceptance"], capture=True,
        )
        for name in ("Toolchain", "NativeDenylist", "TLSControls", "RuntimeRoots"):
            if f"--- PASS: TestKindleCA{name} " not in native_tests:
                raise ValueError(f"Missing native test evidence: {name}")
        proof = {
            "status": "isolated_linux_amd64_acceptance_passed_not_published",
            "image_ids": images,
            "application_version": "1.0.0",
            "source_sha256": {
                name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
                for name in ("main.go", "go.mod", "go.sum", "Dockerfile")
            },
            "binary_sha256": hashlib.sha256((workspace / "final-binary").read_bytes()).hexdigest(),
            "bundle_sha256": hashlib.sha256((workspace / "final-bundle").read_bytes()).hexdigest(),
            "system_root_count": count,
            "blocked_spki_count": len(CA.load_policy()),
            "native_build_info": build_info,
            "native_tests": native_tests,
            "safety": "No app entrypoint, real config, SMTP, media, credentials, or outbound runtime network.",
        }
        output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(proof, indent=2))
        return proof
    finally:
        try:
            for container in containers:
                command("docker", "rm", "-v", container)
        finally:
            shutil.rmtree(workspace)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".kindle-ca-proof.json")
    args = parser.parse_args()
    verify(args.output)


if __name__ == "__main__":
    main()
