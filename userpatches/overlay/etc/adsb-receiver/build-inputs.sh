#!/usr/bin/env bash
# Non-secret image inputs for customize-image.sh.
#
# armbian/build mounts userpatches/overlay read-only at /tmp/overlay inside the
# target chroot before it calls customize-image.sh. Keep values that must cross
# that boundary here, rather than relying on arbitrary GitHub $GITHUB_ENV
# entries that the Action's inner Docker invocation deliberately does not pass.
# validate_repository.py requires these values to match config/build.json.

# shellcheck disable=SC2034 # customize-image.sh reads these after sourcing this file.
ADSB_IMAGE_VERSION='2026.08.21.3'
ADSB_ARMBIAN_REVISION='0620eb67885d19aeabd62655e60870ffd1efad63'
ADSB_READSB_REVISION='d9a4c62655490e70d07704e207738bb9c6cffde1'
ADSB_CONFIG_URL_TEMPLATE='https://adsb-server.local/adsb/config/{receiver_id}.json'

adsb_target_id() {
  case "$1" in
    orangepizero3) ADSB_TARGET='orangepi-zero3' ;;
    *)
      printf 'unsupported Armbian board for ADS-B appliance: %s\n' "$1" >&2
      return 1
      ;;
  esac
}
