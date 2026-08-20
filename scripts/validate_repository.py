#!/usr/bin/env python3
"""Fast, dependency-light validation for public repository contracts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_TARGET_FIELDS = {"enabled", "board", "displayName", "architecture", "armbian", "minimumHardware"}
REQUIRED_ARMBIAN_FIELDS = {"branch", "kernelRevision"}
REQUIRED_HARDWARE_FIELDS = {"ramMiB", "storageGiB", "ethernet", "usbHostPorts", "notes"}
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")
# Split known token prefixes so this scanner does not flag its own detection rule.
TOKEN = re.compile(
    r"(?:" + "github" + r"_pat_|" + "gh" + r"p_|" + "gl" + r"pat-|gitea[_-]?token\s*[:=])",
    re.IGNORECASE,
)


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_json(relative: str) -> object:
    try:
        return json.loads((ROOT / relative).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{relative}: {exc}")


def tracked_text() -> str:
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    text = []
    for relative in paths:
        if not relative or relative == "userpatches/overlay/etc/adsb-receiver/publickey.minisign":
            continue
        path = ROOT / relative
        if path.is_file():
            text.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target")
    args = parser.parse_args()

    build = load_json("config/build.json")
    targets_document = load_json("config/targets.json")
    schema = load_json("schemas/receiver-config.schema.json")
    example = load_json("examples/config-server/config/default.json")

    errors = sorted(Draft202012Validator(schema).iter_errors(example), key=lambda error: list(error.path))
    if errors:
        fail("example configuration violates receiver-config.schema.json: " + errors[0].message)

    targets = targets_document.get("targets") if isinstance(targets_document, dict) else None
    if not isinstance(targets, dict) or not targets:
        fail("config/targets.json must contain a non-empty targets object")
    if args.target and args.target not in targets:
        fail(f"selected target {args.target!r} is not declared")

    board_ids: set[str] = set()
    enabled = 0
    for target_id, target in targets.items():
        if not isinstance(target, dict):
            fail(f"target {target_id!r} must be an object")
        missing = REQUIRED_TARGET_FIELDS - target.keys()
        if missing:
            fail(f"target {target_id!r} is missing fields: {', '.join(sorted(missing))}")
        if target["board"] in board_ids:
            fail(f"Armbian board ID {target['board']!r} is declared more than once")
        board_ids.add(target["board"])
        if target["architecture"] not in {"arm64", "armhf"}:
            fail(f"target {target_id!r} has unsupported architecture {target['architecture']!r}")
        if REQUIRED_ARMBIAN_FIELDS - target["armbian"].keys():
            fail(f"target {target_id!r} has incomplete Armbian configuration")
        if not SHA.fullmatch(target["armbian"]["kernelRevision"]):
            fail(f"target {target_id!r} kernelRevision must be an immutable 40-character commit")
        if REQUIRED_HARDWARE_FIELDS - target["minimumHardware"].keys():
            fail(f"target {target_id!r} has incomplete minimumHardware")
        enabled += bool(target["enabled"])
    if not enabled:
        fail("at least one target must be enabled")

    armbian = build.get("armbian", {}) if isinstance(build, dict) else {}
    readsb = build.get("readsb", {}) if isinstance(build, dict) else {}
    if not SHA.fullmatch(armbian.get("revision", "")):
        fail("Armbian framework revision must be an immutable 40-character commit")
    if not SHA.fullmatch(readsb.get("revision", "")):
        fail("readsb revision must be an immutable 40-character commit")
    if armbian.get("release") != "trixie":
        fail("this appliance currently supports only the validated Debian trixie release")

    workflows = ROOT / ".github" / "workflows"
    for workflow in workflows.glob("*.yml"):
        for line in workflow.read_text().splitlines():
            if "uses:" not in line:
                continue
            reference = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            if "@" not in reference or not SHA.fullmatch(reference.rsplit("@", 1)[1]):
                fail(f"{workflow.relative_to(ROOT)} contains a floating action reference: {reference}")
    build_workflow = (workflows / "build-image.yml").read_text()
    if "armbian/build@8de11a017f7f05a82c77850f8322928cb6a3b70c" not in build_workflow:
        fail("build workflow must pin the official Armbian action revision")
    if armbian["revision"] not in build_workflow:
        fail("config/build.json Armbian revision must match the pinned build workflow")
    if (ROOT / ".gitea" / "workflows" / "build-image.yml").exists():
        fail("obsolete Gitea image-build workflow is still enabled")

    readme = (ROOT / "README.md").read_text()
    for phrase in ("reproducible", "Armbian", "Orange Pi Zero 3", "GitHub Actions", "last-known-good"):
        if phrase not in readme:
            fail(f"README is missing required project documentation: {phrase}")

    text = tracked_text()
    if PRIVATE_KEY.search(text):
        fail("tracked private key detected")
    if TOKEN.search(text):
        fail("tracked access token detected")
    print(f"validated {len(targets)} target(s), {enabled} enabled")


if __name__ == "__main__":
    main()
