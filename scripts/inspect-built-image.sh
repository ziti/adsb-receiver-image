#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 1 )); then
  echo "usage: inspect-built-image.sh IMAGE.img.xz" >&2
  exit 2
fi

work=$(mktemp -d)
image=$work/image.img
root_mount=$work/root
bootstrap_mount=$work/bootstrap
loop=
cleanup() {
  mountpoint -q "$bootstrap_mount" && umount "$bootstrap_mount" || true
  mountpoint -q "$root_mount" && umount "$root_mount" || true
  [[ -n $loop ]] && losetup -d "$loop" || true
  rm -rf "$work"
}
trap cleanup EXIT

xz -dc "$1" > "$image"
loop=$(losetup --show --find --partscan "$image")
mkdir -p "$root_mount" "$bootstrap_mount"
lsblk -o NAME,SIZE,FSTYPE,LABEL,PARTTYPE "$loop"
test "$(lsblk -n -o TYPE "$loop" | grep -c '^part$')" = 2
test "$(blkid -s TYPE -o value "${loop}p2")" = vfat
test "$(blkid -s LABEL -o value "${loop}p2")" = ADSB-BOOT
mount -o ro "${loop}p1" "$root_mount"
mount -o ro "${loop}p2" "$bootstrap_mount"
grep -F 'LABEL=ADSB-BOOT /boot/adsb-bootstrap vfat rw,nosuid,nodev,noexec,umask=0077' "$root_mount/etc/fstab"
test -f "$root_mount/etc/adsb-receiver-release"
test -f "$root_mount/usr/share/adsb-receiver/default-config.json"
test -f "$root_mount/usr/share/adsb-receiver/receiver-config.schema.json"
for unit in adsb-initialize adsb-network-mode adsb-firewall readsb adsb-json-http adsb-admin; do
  test -L "$root_mount/etc/systemd/system/multi-user.target.wants/$unit.service"
done
for package in network-manager nftables readsb; do
  grep -Fqx "Package: $package" "$root_mount/var/lib/dpkg/status"
done
test ! -e "$root_mount/usr/local/sbin/adsb-config-agent"
test ! -e "$root_mount/etc/systemd/system/adsb-config-refresh.timer"
if grep -R -Fq outboundConnectors "$root_mount/usr/share/adsb-receiver" "$root_mount/usr/local/sbin/adsb-config"; then
  echo "outbound connector support remains in the built appliance" >&2
  exit 1
fi
if find "$bootstrap_mount" -mindepth 1 -type f \( -name 'config.json' -o -name 'admin-password.json' -o -name '*.pem' \) | grep -q .; then
  echo "ADSB-BOOT contains a forbidden persistent configuration or secret" >&2
  exit 1
fi
