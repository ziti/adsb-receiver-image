#!/usr/bin/env bash
# Add a small data-only FAT32 volume that desktop operating systems can mount.
# The root filesystem remains partition 1; ADSB-BOOT is partition 2.

ADSB_BOOTSTRAP_MIB=128
ADSB_BOOTSTRAP_LABEL=ADSB-BOOT
ADSB_BOOTSTRAP_MOUNT=/boot/adsb-bootstrap

function prepare_image_size__950_adsb_bootstrap_partition() {
	if [[ ${IMAGE_PARTITION_TABLE:-msdos} != msdos ]]; then
		display_alert "ADS-B bootstrap partition" "requires an msdos partition table" "err"
		return 1
	fi
	EXTRA_ROOTFS_MIB_SIZE=$(( ${EXTRA_ROOTFS_MIB_SIZE:-0} + ADSB_BOOTSTRAP_MIB ))
	# shellcheck disable=SC2034 # Read by the Armbian partitioning framework.
	USE_HOOK_FOR_PARTITION=yes
}

function create_partition_table__950_adsb_bootstrap_partition() {
	local bootstrap_sectors root_sectors root_start total_sectors
	bootstrap_sectors=$(( ADSB_BOOTSTRAP_MIB * 1024 * 1024 / SECTOR_SIZE ))
	# shellcheck disable=SC2154 # sdsize is dynamically scoped by Armbian.
	total_sectors=$(( sdsize * 1024 * 1024 / SECTOR_SIZE ))
	root_start=$(( OFFSET * 1024 * 1024 / SECTOR_SIZE ))
	root_sectors=$(( total_sectors - root_start - bootstrap_sectors ))
	if (( root_sectors <= 0 )); then
		display_alert "ADS-B bootstrap partition" "calculated root partition is invalid" "err"
		return 1
	fi
	sfdisk "${SDCARD}.raw" <<-EOF
	label: dos
	unit: sectors

	start=${root_start}, size=${root_sectors}, type=83
	start=$(( root_start + root_sectors )), size=${bootstrap_sectors}, type=0c
	EOF
}

function format_partitions__950_adsb_bootstrap_partition() {
	local bootstrap_blocks bootstrap_sectors bootstrap_start total_sectors
	bootstrap_sectors=$(( ADSB_BOOTSTRAP_MIB * 1024 * 1024 / SECTOR_SIZE ))
	bootstrap_blocks=$(( ADSB_BOOTSTRAP_MIB * 1024 ))
	# sdsize is dynamically scoped by Armbian's prepare_partitions function.
	total_sectors=$(( sdsize * 1024 * 1024 / SECTOR_SIZE ))
	bootstrap_start=$(( total_sectors - bootstrap_sectors ))
	if (( bootstrap_start <= 0 )); then
		display_alert "ADS-B bootstrap partition" "calculated FAT offset is invalid" "err"
		return 1
	fi

	# GitHub's nested Armbian container exposes only loop0p1 and rejects an
	# additional loop mapping.  Partition 2 already exists in ${SDCARD}.raw;
	# format that exact bounded region directly, without touching a loop device.
	if ! mkfs.fat -F 32 -n "${ADSB_BOOTSTRAP_LABEL}" --offset="${bootstrap_start}" \
		"${SDCARD}.raw" "${bootstrap_blocks}"; then
		display_alert "ADS-B bootstrap partition" "failed to format partition 2 at sector ${bootstrap_start}" "err"
		return 1
	fi

	install -d -m 0755 "${MOUNT}${ADSB_BOOTSTRAP_MOUNT}"
	printf 'LABEL=%s %s vfat rw,nosuid,nodev,noexec,umask=0077,x-systemd.device-timeout=10s 0 2\n' \
		"${ADSB_BOOTSTRAP_LABEL}" "${ADSB_BOOTSTRAP_MOUNT}" >> "${SDCARD}/etc/fstab"
}
