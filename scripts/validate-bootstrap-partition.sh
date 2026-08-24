#!/usr/bin/env bash
# Verify the exact raw-image offset formatter used when a nested builder cannot
# expose a second loop partition device.
set -Eeuo pipefail

work=$(mktemp -d)
image=$work/image.raw
partition=$work/adsb-boot.img
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

sector_size=512
bootstrap_mib=128
bootstrap_sectors=$(( bootstrap_mib * 1024 * 1024 / sector_size ))
bootstrap_blocks=$(( bootstrap_mib * 1024 ))
root_start=8192
root_sectors=8192
bootstrap_start=$(( root_start + root_sectors ))
total_sectors=$(( bootstrap_start + bootstrap_sectors ))

truncate -s "$(( total_sectors * sector_size ))" "$image"
sfdisk "$image" <<EOF
label: dos
unit: sectors

start=${root_start}, size=${root_sectors}, type=83
start=${bootstrap_start}, size=${bootstrap_sectors}, type=0c
EOF
mkfs.fat -F 32 -n ADSB-BOOT --offset="$bootstrap_start" "$image" "$bootstrap_blocks"
dd if="$image" of="$partition" bs="$sector_size" skip="$bootstrap_start" count="$bootstrap_sectors" status=none
fsck.fat -n "$partition"
test "$(fatlabel "$partition")" = ADSB-BOOT
