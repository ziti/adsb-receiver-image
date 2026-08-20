#!/usr/bin/env bash
# Enforced by Armbian's late_family_config hook before kernel source resolution.
#
# armbian/build relaunches its framework in Docker and intentionally forwards
# only a curated environment. Keep this map self-contained rather than relying
# on an arbitrary workflow environment variable. validate_repository.py runs
# this hook for every enabled target and requires the resolved value to match
# config/targets.json.

function late_family_config__900_adsb_kernel_pin() {
	case "${BOARD}:${BRANCH}" in
		orangepizero2:current|orangepizero3:current)
			KERNELBRANCH="commit:bf3be28f6721e24961992ebb9e61c0cf21a56806"
			;;
		*)
			return 0
			;;
	esac
	display_alert "ADS-B kernel source pin" "${KERNELBRANCH}" "info"
}
