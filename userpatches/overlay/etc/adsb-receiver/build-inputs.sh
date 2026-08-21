#!/usr/bin/env bash
# Build-only, non-secret inputs for customize-image.sh.
#
# armbian/build mounts userpatches/overlay read-only at /tmp/overlay inside the
# target chroot before it calls customize-image.sh. Keep values that must cross
# that boundary here, rather than relying on arbitrary GitHub $GITHUB_ENV
# entries that the Action's inner Docker invocation deliberately does not pass.
# validate_repository.py requires these values to match config/build.json.

# shellcheck disable=SC2034 # customize-image.sh reads these after sourcing this file.
ADSB_READSB_REVISION='d9a4c62655490e70d07704e207738bb9c6cffde1'
ADSB_CONFIG_URL_TEMPLATE='https://adsb-server.local/adsb/config/{receiver_id}.json'
