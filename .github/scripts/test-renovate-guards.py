#!/usr/bin/env python3
"""Regression tests for Renovate regex guardrails."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RENOVATE_CONFIG = REPO_ROOT / "renovate.json"


@dataclass(frozen=True)
class GuardTest:
    name: str
    package: str
    version: str
    should_reject: bool


def parse_renovate_regex(value: str) -> re.Pattern[str]:
    """Parse Renovate's /pattern/flags or !/pattern/flags regex string."""
    if value.startswith("!"):
        value = value[1:]
    if not value.startswith("/"):
        raise ValueError(f"not a Renovate regex: {value!r}")

    end = value.rfind("/")
    if end == 0:
        end = -1
    if end == -1:
        raise ValueError(f"unterminated Renovate regex: {value!r}")

    pattern = value[1:end]
    flags_text = value[end + 1 :]
    flags = re.IGNORECASE if "i" in flags_text else 0
    return re.compile(pattern, flags)


def package_matches(rule: dict, package: str) -> bool:
    package_names = rule.get("matchPackageNames")
    if not package_names:
        return True

    for matcher in package_names:
        if isinstance(matcher, str) and matcher.startswith("/"):
            if parse_renovate_regex(matcher).search(package):
                return True
        elif matcher == package:
            return True
    return False


def rule_rejects(rule: dict, package: str, version: str) -> bool:
    allowed_versions = rule.get("allowedVersions")
    if not isinstance(allowed_versions, str) or not allowed_versions.startswith("!/"):
        return False
    if not package_matches(rule, package):
        return False
    return bool(parse_renovate_regex(allowed_versions).search(version))


def image_parts(image: str) -> tuple[str, str]:
    package, version = image.rsplit(":", 1)
    return package, version


def main() -> int:
    config = json.loads(RENOVATE_CONFIG.read_text(encoding="utf-8"))
    negated_regex_rules = [
        rule
        for rule in config.get("packageRules", [])
        if isinstance(rule.get("allowedVersions"), str)
        and rule["allowedVersions"].startswith("!/")
    ]

    tests = [
        GuardTest("plex rejects PR-192 arm64 suffix", *image_parts("plexinc/pms-docker:1.43.2-arm64"), True),
        GuardTest("plex rejects armhf suffix", *image_parts("plexinc/pms-docker:1.43.2-armhf"), True),
        GuardTest("plex rejects arm64v8 suffix", *image_parts("plexinc/pms-docker:1.43.2-arm64v8"), True),
        GuardTest("plex rejects arm32v7 suffix", *image_parts("plexinc/pms-docker:1.43.2-arm32v7"), True),
        GuardTest("plex rejects arm32v6 suffix", *image_parts("plexinc/pms-docker:1.43.2-arm32v6"), True),
        GuardTest("plex rejects aarch64 suffix", *image_parts("plexinc/pms-docker:1.43.2-aarch64"), True),
        GuardTest("plex rejects amd64 suffix", *image_parts("plexinc/pms-docker:1.43.2-amd64"), True),
        GuardTest("plex rejects mixed-case arch suffix", *image_parts("plexinc/pms-docker:1.43.2-ARM64"), True),
        GuardTest("plex allows bare amd64 manifest tag", *image_parts("plexinc/pms-docker:1.43.2.10687-563d026ea"), False),
        GuardTest("linuxserver rejects amd64 prefix", *image_parts("lscr.io/linuxserver/foo:amd64-1.0.0-ls123"), True),
        GuardTest("linuxserver rejects arm64v8 prefix", *image_parts("lscr.io/linuxserver/foo:arm64v8-1.0.0-ls123"), True),
        GuardTest("linuxserver rejects arm32v7 prefix", *image_parts("lscr.io/linuxserver/foo:arm32v7-1.0.0-ls123"), True),
        GuardTest("linuxserver rejects arm32v6 prefix", *image_parts("lscr.io/linuxserver/foo:arm32v6-1.0.0-ls123"), True),
        GuardTest("linuxserver rejects aarch64 prefix", *image_parts("lscr.io/linuxserver/foo:aarch64-1.0.0-ls123"), True),
        GuardTest("linuxserver rejects arm64 prefix", *image_parts("lscr.io/linuxserver/foo:arm64-1.0.0-ls123"), True),
        GuardTest("linuxserver rejects arm prefix", *image_parts("lscr.io/linuxserver/foo:arm-1.0.0-ls123"), True),
        GuardTest("linuxserver rejects i386 prefix", *image_parts("lscr.io/linuxserver/foo:i386-1.0.0-ls123"), True),
        GuardTest("linuxserver rejects x86_64 prefix", *image_parts("lscr.io/linuxserver/foo:x86_64-1.0.0-ls123"), True),
        GuardTest("linuxserver rejects amd64 suffix", *image_parts("lscr.io/linuxserver/foo:1.0.0-ls123-amd64"), True),
        GuardTest("linuxserver rejects mixed-case arch suffix", *image_parts("lscr.io/linuxserver/foo:1.0.0-ls123-AMD64"), True),
        GuardTest("linuxserver allows normal build tag", *image_parts("lscr.io/linuxserver/foo:1.0.0-ls123"), False),
    ]

    failures = 0
    for test in tests:
        rejected = any(rule_rejects(rule, test.package, test.version) for rule in negated_regex_rules)
        passed = rejected == test.should_reject
        status = "PASS" if passed else "FAIL"
        expectation = "reject" if test.should_reject else "allow"
        print(f"{status}: {test.name} ({test.package}:{test.version}) expected {expectation}")
        if not passed:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
