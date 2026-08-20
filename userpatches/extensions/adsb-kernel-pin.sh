#!/usr/bin/env bash
# Enforced by Armbian's late_family_config hook before kernel source resolution.

function late_family_config__900_adsb_kernel_pin() {
	[[ "${ADSB_KERNEL_REVISION:-}" =~ ^[0-9a-f]{40}$ ]] || {
		echo "ADSB_KERNEL_REVISION must be an immutable 40-character commit" >&2
		exit 1
	}

	KERNELBRANCH="commit:${ADSB_KERNEL_REVISION}"
	display_alert "ADS-B kernel source pin" "${KERNELBRANCH}" "info"
}
