#!/usr/bin/env bash
set -Eeuo pipefail

# Armbian runs this script inside the target root filesystem. RELEASE is a
# framework configuration variable and is not guaranteed to be forwarded into
# that chroot, so assert the resulting userspace rather than its transient
# build-time input.
# shellcheck disable=SC1091 # Exists in the target Debian root filesystem.
. /etc/os-release
debian_release=${VERSION_CODENAME:-}
if [[ $debian_release != "trixie" ]]; then
  echo "This image is pinned to Debian trixie; got ${debian_release:-unset}" >&2
  exit 1
fi

# The pinned Armbian framework bind-mounts userpatches/overlay at /tmp/overlay
# before entering this chroot. Read non-secret, repository-validated inputs
# from that stable path instead of relying on workflow environment forwarding.
# shellcheck source=/dev/null # Armbian creates this bind mount for customization.
. /tmp/overlay/etc/adsb-receiver/build-inputs.sh
adsb_target_id "$3"

# Armbian bind-mounts the overlay for customization but does not copy it into
# the target filesystem. Materialize the runtime configuration and units before
# enabling them later in this script.
cp -a /tmp/overlay/. /

apt-get update
apt-get install --no-install-recommends -y \
  ca-certificates curl iproute2 network-manager nftables openssh-server openssl sudo \
  rtl-sdr librtlsdr0 libusb-1.0-0 python3
apt-get install --no-install-recommends -y \
  git build-essential debhelper pkg-config fakeroot help2man libncurses-dev zlib1g-dev libzstd-dev libusb-1.0-0-dev librtlsdr-dev

install -d -m 0750 /var/lib/adsb-receiver /etc/adsb-receiver
install -d -m 0755 /run/adsb-receiver /run/readsb /boot/adsb-bootstrap
git clone https://github.com/wiedehopf/readsb.git /usr/local/src/readsb
git -C /usr/local/src/readsb checkout --detach "${ADSB_READSB_REVISION:?missing readsb revision}"
(cd /usr/local/src/readsb && DEB_BUILD_OPTIONS=noddebs dpkg-buildpackage -b -ui -uc -us --build-profiles=rtlsdr)
dpkg -i /usr/local/src/readsb_*.deb
rm -rf /usr/local/src/readsb
apt-get purge -y git build-essential debhelper pkg-config fakeroot help2man libncurses-dev zlib1g-dev libzstd-dev libusb-1.0-0-dev librtlsdr-dev
apt-get autoremove -y

id -u admin >/dev/null 2>&1 || useradd --create-home --groups sudo --shell /bin/bash admin
install -d -o admin -g admin -m 0700 /home/admin/.ssh
install -m 0600 /tmp/overlay/etc/adsb-receiver/admin-authorized_keys /home/admin/.ssh/authorized_keys
chown admin:admin /home/admin/.ssh/authorized_keys
cat > /etc/adsb-receiver-release <<EOF
IMAGE_VERSION=${ADSB_IMAGE_VERSION:?}
ARMBIAN_REVISION=${ADSB_ARMBIAN_REVISION:?}
DEBIAN_RELEASE=${debian_release}
READSB_REVISION=${ADSB_READSB_REVISION:?}
TARGET=${ADSB_TARGET:?}
REPOSITORY_COMMIT=$(cat /etc/adsb-receiver/repository-commit 2>/dev/null || printf unavailable)
EOF
systemctl disable apt-daily.timer apt-daily-upgrade.timer || true
ADSB_ETC=/etc/adsb-receiver \
ADSB_STATE=/var/lib/adsb-receiver \
ADSB_RUN=/run/adsb-receiver \
ADSB_BOOT=/boot/adsb-bootstrap \
ADSB_DEFAULT_CONFIG=/usr/share/adsb-receiver/default-config.json \
  /usr/local/sbin/adsb-config initialize
systemctl enable NetworkManager.service adsb-initialize.service adsb-network-mode.service \
  adsb-firewall.service readsb.service adsb-json-http.service adsb-admin.service
