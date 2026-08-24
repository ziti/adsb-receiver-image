#!/usr/bin/env python3
"""Fast, dependency-light validation for public repository contracts."""

from __future__ import annotations

import argparse
import configparser
import json
import re
import subprocess
import sys
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")
IMAGE_VERSION = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d+$")
SSH_PUBLIC_KEY = re.compile(r"^(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp(?:256|384|521))\s+\S+(?:\s+.*)?$")
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
        if not relative:
            continue
        path = ROOT / relative
        if path.is_file():
            text.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(text)


def validate_systemd_units() -> None:
    units = ROOT / "userpatches/overlay/etc/systemd/system"
    overlay = ROOT / "userpatches/overlay"
    for unit_path in sorted(units.glob("*")):
        if unit_path.suffix not in {".service", ".timer"}:
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        try:
            parser.read_file(unit_path.open())
        except (OSError, configparser.Error) as exc:
            fail(f"invalid systemd unit {unit_path.relative_to(ROOT)}: {exc}")
        if not parser.has_option("Unit", "Description"):
            fail(f"systemd unit lacks Unit/Description: {unit_path.relative_to(ROOT)}")
        if unit_path.suffix == ".service":
            if not parser.has_option("Service", "ExecStart"):
                fail(f"service lacks Service/ExecStart: {unit_path.relative_to(ROOT)}")
            executable = parser["Service"]["ExecStart"].split()[0].lstrip("-!@:")
            if executable.startswith("/") and not (overlay / executable.lstrip("/")).exists():
                fail(f"service executable is absent from overlay: {executable}")
        else:
            if not parser.has_option("Timer", "OnBootSec"):
                fail(f"timer lacks Timer/OnBootSec: {unit_path.relative_to(ROOT)}")
            if not parser.has_section("Install") or not parser.has_option("Install", "WantedBy"):
                fail(f"timer lacks Install/WantedBy: {unit_path.relative_to(ROOT)}")


def validate_kernel_pin_extension(targets: dict[str, object]) -> None:
    """Execute the Armbian hook and compare each enabled target to its declaration.

    The official Action's Docker relaunch does not forward arbitrary workflow
    variables, so the extension intentionally carries its own board/branch map.
    Exercising the hook makes that necessary duplication fail closed if it drifts
    from config/targets.json.
    """
    extension = ROOT / "userpatches/extensions/adsb-kernel-pin.sh"
    if not extension.is_file():
        fail("kernel-pin Armbian extension is missing")
    command = r'''
display_alert() { :; }
source "$1"
BOARD="$2"
BRANCH="$3"
late_family_config__900_adsb_kernel_pin
printf '%s' "${KERNELBRANCH:-}"
'''
    for target_id, target in targets.items():
        if not target["enabled"]:
            continue
        result = subprocess.run(
            [
                "bash",
                "-c",
                command,
                "validate-kernel-pin",
                str(extension),
                target["board"],
                target["armbian"]["branch"],
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            fail(
                f"kernel-pin extension failed for {target_id!r}: "
                f"{result.stderr.strip() or 'unknown error'}"
            )
        expected = f"commit:{target['armbian']['kernelRevision']}"
        if result.stdout != expected:
            fail(
                f"kernel-pin extension for {target_id!r} resolved {result.stdout!r}, "
                f"expected {expected!r} from config/targets.json"
            )


def validate_bootstrap_partition() -> None:
    extension = ROOT / "userpatches/extensions/adsb-bootstrap-partition.sh"
    inspector = ROOT / "scripts/inspect-built-image.sh"
    if not extension.is_file() or not inspector.is_file():
        fail("ADSB-BOOT partition extension or built-image inspector is missing")
    text = extension.read_text()
    for contract in (
        "ADSB_BOOTSTRAP_MIB=128",
        "ADSB_BOOTSTRAP_LABEL=ADSB-BOOT",
        "ADSB_BOOTSTRAP_MOUNT=/boot/adsb-bootstrap",
        "USE_HOOK_FOR_PARTITION=yes",
        "mkfs.fat -F 32",
        "--offset=\"${bootstrap_start}\"",
        "\"${SDCARD}.raw\" \"${bootstrap_blocks}\"",
        "type=0c",
        "LABEL=%s %s vfat rw,nosuid,nodev,noexec,umask=0077",
    ):
        if contract not in text:
            fail(f"bootstrap partition extension is missing contract: {contract}")
    if '${SRC}/../custom' in text:
        fail("bootstrap partition extension must not access the action checkout outside Armbian userpatches")
    create_hook = re.search(
        r"^function create_partition_table__950_adsb_bootstrap_partition\(\) \{\n(?P<body>.*?)^\}\n",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not create_hook:
        fail("bootstrap partition extension lacks its pre-loop create_partition_table hook")
    create_body = create_hook.group("body")
    if re.search(r"\bLOOP\b", create_body):
        fail("bootstrap partition create hook must not reference LOOP before Armbian attaches it")
    if "partprobe" in create_body:
        fail("bootstrap partition create hook must not probe partitions before Armbian attaches LOOP")
    format_hook = re.search(
        r"^function format_partitions__950_adsb_bootstrap_partition\(\) \{\n(?P<body>.*?)^\}\n",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not format_hook:
        fail("bootstrap partition extension lacks its post-loop format_partitions hook")
    format_body = format_hook.group("body")
    if "LOOP" in format_body or "partx" in format_body or "mount -o" in format_body:
        fail("bootstrap partition formatter must not require a second loop-device mapping")
    canonical_schema = ROOT / "schemas/receiver-config.schema.json"
    image_schema = ROOT / "userpatches/overlay/usr/share/adsb-receiver/receiver-config.schema.json"
    if not image_schema.is_file() or image_schema.read_bytes() != canonical_schema.read_bytes():
        fail("image overlay receiver schema must exactly match schemas/receiver-config.schema.json")
    size = re.search(r"ADSB_BOOTSTRAP_MIB=(\d+)", text)
    if not size or not 64 <= int(size.group(1)) <= 256:
        fail("ADSB-BOOT partition must be 64..256 MiB")
    for script in (extension, inspector):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        if result.returncode:
            fail(f"invalid shell syntax in {script.relative_to(ROOT)}: {result.stderr.strip()}")


def validate_customize_build_inputs(build: dict[str, object], targets: dict[str, object]) -> None:
    """Check image inputs that cross Armbian's inner Docker/chroot boundary."""
    inputs = ROOT / "userpatches/overlay/etc/adsb-receiver/build-inputs.sh"
    authorized_keys = ROOT / "userpatches/overlay/etc/adsb-receiver/admin-authorized_keys"
    if not inputs.is_file():
        fail("customize build-inputs overlay file is missing")
    if not authorized_keys.is_file():
        fail("administrator public-key overlay file is missing")
    key_lines = [line for line in authorized_keys.read_text().splitlines() if line and not line.startswith("#")]
    if not key_lines or any(not SSH_PUBLIC_KEY.fullmatch(line) for line in key_lines):
        fail("administrator public-key overlay file must contain one or more valid SSH public keys")
    command = r'''
set -Eeuo pipefail
source "$1"
adsb_target_id "$2"
printf '%s\n%s\n%s\n%s\n' \
  "$ADSB_IMAGE_VERSION" "$ADSB_ARMBIAN_REVISION" "$ADSB_READSB_REVISION" \
  "$ADSB_TARGET"
'''
    for target_id, target in targets.items():
        if not target["enabled"]:
            continue
        result = subprocess.run(
            ["bash", "-c", command, "validate-build-inputs", str(inputs), target["board"]],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            fail(
                f"customize build-inputs file is invalid for {target_id!r}: "
                f"{result.stderr.strip() or 'unknown error'}"
            )
        image_version, armbian_revision, readsb_revision, resolved_target = result.stdout.splitlines()
        if not IMAGE_VERSION.fullmatch(image_version) or image_version != build["imageVersion"]:
            fail("customize build-inputs image version differs from config/build.json")
        if armbian_revision != build["armbian"]["revision"]:
            fail("customize build-inputs Armbian revision differs from config/build.json")
        if readsb_revision != build["readsb"]["revision"]:
            fail("customize build-inputs readsb revision differs from config/build.json")
        if resolved_target != target_id:
            fail(
                f"customize build-inputs board map resolved {resolved_target!r}, "
                f"expected {target_id!r}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target")
    parser.add_argument("--production", action="store_true", help="reject deployment placeholders")
    args = parser.parse_args()

    build = load_json("config/build.json")
    targets_document = load_json("config/targets.json")
    schema = load_json("schemas/receiver-config.schema.json")
    example = load_json("examples/local-config.json")
    factory = load_json("userpatches/overlay/usr/share/adsb-receiver/default-config.json")

    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for label, document in (("example", example), ("factory", factory)):
        errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
        if errors:
            fail(f"{label} configuration violates receiver-config.schema.json: {errors[0].message}")
    if factory.get("setupComplete") is not False:
        fail("factory configuration must enter configuration mode on first boot")
    if factory.get("listeners", {}).get("beast", {}).get("port") != 30005:
        fail("factory Beast listener must default to TCP 30005")
    if factory.get("listeners", {}).get("jsonHttp", {}).get("port") != 8080:
        fail("factory JSON HTTP listener must default to TCP 8080")

    targets = targets_document.get("targets") if isinstance(targets_document, dict) else None
    if not isinstance(targets, dict) or not targets:
        fail("config/targets.json must contain a non-empty targets object")
    if args.target and args.target not in targets:
        fail(f"selected target {args.target!r} is not declared")

    board_ids: set[str] = set()
    enabled_targets = []
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
        if target["enabled"]:
            enabled_targets.append(target_id)
    if not enabled_targets:
        fail("at least one target must be enabled")

    armbian = build.get("armbian", {}) if isinstance(build, dict) else {}
    readsb = build.get("readsb", {}) if isinstance(build, dict) else {}
    if not SHA.fullmatch(armbian.get("revision", "")):
        fail("Armbian framework revision must be an immutable 40-character commit")
    if not SHA.fullmatch(readsb.get("revision", "")):
        fail("readsb revision must be an immutable 40-character commit")
    if not IMAGE_VERSION.fullmatch(build.get("imageVersion", "")):
        fail("imageVersion must use YYYY.MM.DD.N format")
    if armbian.get("release") != "trixie":
        fail("this appliance currently supports only the validated Debian trixie release")
    customize_script = (ROOT / "userpatches/customize-image.sh").read_text()
    if ". /etc/os-release" not in customize_script or "VERSION_CODENAME" not in customize_script:
        fail("customize-image.sh must validate the target userspace via /etc/os-release")
    if "DEBIAN_RELEASE=${RELEASE}" in customize_script:
        fail("customize-image.sh must not record the unavailable build-time RELEASE variable")
    if ". /tmp/overlay/etc/adsb-receiver/build-inputs.sh" not in customize_script:
        fail("customize-image.sh must source repository-validated inputs from Armbian's overlay mount")
    overlay_copy = "cp -a /tmp/overlay/. /"
    if overlay_copy not in customize_script:
        fail("customize-image.sh must materialize Armbian's overlay into the target filesystem")
    if customize_script.index(overlay_copy) > customize_script.index("systemctl enable"):
        fail("customize-image.sh must materialize the overlay before enabling its systemd units")
    validate_customize_build_inputs(build, targets)
    validate_systemd_units()

    workflows = ROOT / ".github" / "workflows"
    for workflow in workflows.glob("*.yml"):
        for line in workflow.read_text().splitlines():
            if "uses:" not in line:
                continue
            reference = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            if "@" not in reference or not SHA.fullmatch(reference.rsplit("@", 1)[1]):
                fail(f"{workflow.relative_to(ROOT)} contains a floating action reference: {reference}")
    build_workflow = (workflows / "build-image.yml").read_text()
    if f"armbian/build@{armbian['revision']}" not in build_workflow:
        fail("build workflow must pin the official Armbian action revision")
    for required_reference in (
        f"ARMBIAN_ACTION_REVISION: {armbian['revision']}",
        f"armbian_branch: {armbian['revision']}",
    ):
        if required_reference not in build_workflow:
            fail("config/build.json Armbian revision must match every build workflow reference")
    if "armbian_version:" in build_workflow:
        fail("appliance image_version must not be passed to armbian_version")
    if "armbian_extensions: adsb-kernel-pin,adsb-bootstrap-partition" not in build_workflow:
        fail("build workflow must enable both ADS-B Armbian extensions")
    if "scripts/inspect-built-image.sh" not in build_workflow:
        fail("build workflow must inspect the partition layout after every image build")
    if "armbian_release_prerelease: true" not in build_workflow or "needs: build" not in build_workflow or 'gh release edit "$IMAGE_VERSION" --prerelease=false --latest' not in build_workflow:
        fail("build workflow must promote the shared release only after every inspected target succeeds")
    if re.search(r"^\s+ADSB_[A-Z_]+:\s+\$\{\{", build_workflow, re.MULTILINE):
        fail("build workflow must not pass arbitrary ADSB variables into Armbian's inner Docker boundary")
    if f"default: {build['imageVersion']}" not in build_workflow:
        fail("manual image_version default must match config/build.json")
    if 'test "$ADSB_IMAGE_VERSION" = "$IMAGE_VERSION"' not in build_workflow:
        fail("manual image_version must be checked against the committed image input")
    if '"kernelRevision": targets["targets"][target_name]["armbian"]["kernelRevision"]' not in build_workflow:
        fail("build manifest must read the declared kernel revision from config/targets.json")
    matrix_targets = set(re.findall(r"^\s*- target: ([a-z0-9-]+)\s*$", build_workflow, re.MULTILINE))
    if matrix_targets != set(enabled_targets):
        fail(
            "enabled targets and build workflow matrix differ: "
            f"targets={sorted(enabled_targets)}, matrix={sorted(matrix_targets)}"
        )
    for source_checkout in ("git -C build rev-parse HEAD", "git -C os rev-parse HEAD"):
        if source_checkout not in build_workflow:
            fail("build manifest must record both Armbian framework and os checkout revisions")
    validate_kernel_pin_extension(targets)
    validate_bootstrap_partition()
    if (ROOT / ".gitea" / "workflows" / "build-image.yml").exists():
        fail("obsolete Gitea image-build workflow is still enabled")

    readme = (ROOT / "README.md").read_text()
    for phrase in ("reproducible", "Armbian", "Orange Pi Zero3", "Orange Pi Zero2", "GitHub Actions", "last-known-good", "30005", "8080", "8443", "NetworkManager"):
        if phrase not in readme:
            fail(f"README is missing required project documentation: {phrase}")

    text = tracked_text()
    if PRIVATE_KEY.search(text):
        fail("tracked private key detected")
    if TOKEN.search(text):
        fail("tracked access token detected")
    readsb_unit = (ROOT / "userpatches/overlay/etc/systemd/system/readsb.service").read_text()
    if "adsb-config-agent" in readsb_unit or "network-online.target" in readsb_unit:
        fail("readsb startup must not depend on the retired remote configuration path or internet access")
    config_runtime = (ROOT / "userpatches/overlay/usr/local/sbin/adsb-config").read_text()
    network_runtime = (ROOT / "userpatches/overlay/usr/local/sbin/adsb-network-mode").read_text()
    initialize_unit = (ROOT / "userpatches/overlay/etc/systemd/system/adsb-initialize.service").read_text()
    for contract in ("preflight_admin_endpoint", "local_ipv4_addresses", "clear_loaded_networkmanager_state", '"FSTYPE,LABEL"', 'fields != ["vfat", "ADSB-BOOT"]'):
        if contract not in config_runtime:
            fail(f"adsb-config is missing durability contract: {contract}")
    if "config_module.validate_bootstrap_mount()" not in network_runtime:
        fail("configuration-mode bootstrap must validate the ADSB-BOOT mount before writing credentials")
    if "After=NetworkManager.service" not in initialize_unit or "Wants=NetworkManager.service" not in initialize_unit:
        fail("factory-reset initialization must run after the local NetworkManager daemon starts")
    required_runtime = {
        "usr/local/sbin/adsb-config",
        "usr/local/sbin/adsb-network-mode",
        "usr/local/sbin/adsb-json-server",
        "usr/local/sbin/adsb-admin-server",
        "usr/local/sbin/adsb-readsb",
    }
    overlay = ROOT / "userpatches/overlay"
    for relative in required_runtime:
        runtime = overlay / relative
        if not runtime.is_file():
            fail(f"runtime component is missing: {relative}")
        if not runtime.stat().st_mode & 0o111:
            fail(f"runtime component is not executable: {relative}")
    retired_files = [
        ROOT / "userpatches/overlay/usr/local/sbin/adsb-config-agent",
        ROOT / "userpatches/overlay/etc/systemd/system/adsb-config-refresh.timer",
    ]
    retired_example = ROOT / "examples/config-server"
    if any(path.exists() for path in retired_files) or (retired_example.exists() and any(path.is_file() for path in retired_example.rglob("*"))):
        fail("retired mandatory remote-configuration artifacts remain in the repository")
    if 'install -m 0600 /tmp/overlay/etc/adsb-receiver/admin-authorized_keys' not in customize_script:
        fail("customize-image.sh must install administrator keys from Armbian's overlay mount")
    if "ADSB_ADMIN_AUTHORIZED_KEYS" in customize_script:
        fail("customize-image.sh must not require a workflow-only administrator-key variable")
    print(f"validated {len(targets)} target(s), {len(enabled_targets)} enabled")


if __name__ == "__main__":
    main()
