# Local-first ADS-B receiver appliance

This repository builds reproducible Armbian appliance images for Orange Pi Zero3 and Orange Pi Zero2. Each image runs an RTL-SDR and pinned [readsb](https://github.com/wiedehopf/readsb) locally. Normal installation is flash, boot, configure in a browser, and receive. It does not require an ADS-B platform, cloud service, database, container runtime, or SSH for routine setup.

The default LAN data interfaces are:

| Interface | Default | Exposure | Purpose |
| --- | --- | --- | --- |
| Beast TCP | `0.0.0.0:30005` | Private IPv4, ULA, and link-local sources only | Binary Beast stream for multiple simultaneous consumers |
| readsb JSON HTTP | `0.0.0.0:8080` | Private IPv4, ULA, and link-local sources only | Read-only `/data/aircraft.json`, `/data/receiver.json`, and `/data/stats.json` |
| Administration | `127.0.0.1:8443` | Localhost by default; configurable to one private management address | Authenticated HTTPS setup, status, and configuration |

Wildcard Beast and JSON listeners are protected by the appliance nftables table. They are not public services. Do not add router port forwards, public firewall exceptions, or a public admin bind. The schema rejects public literal addresses and an unspecified admin bind.

## Architecture

```mermaid
flowchart LR
    SDR["RTL-SDR"] --> Readsb["readsb"]
    LocalConfig["Validated local config"] --> Apply["Atomic apply and rollback"]
    Apply --> Readsb
    Readsb --> Beast["Beast TCP :30005"]
    Readsb --> Files["Atomic JSON files in /run/readsb"]
    Files --> JSON["Read-only JSON HTTP :8080"]
    Browser["Phone or laptop"] --> Admin["TLS admin :8443"]
    Admin --> Apply
    NetworkManager["NetworkManager"] --> Ethernet["Ethernet preferred"]
    NetworkManager --> WiFi["Wi-Fi fallback"]
    NetworkManager --> SetupAP["Explicit setup AP only"]
```

Active configuration is `/etc/adsb-receiver/config.json`. Generated readsb arguments, the last-known-good configuration, rollback material, bootstrap state, and the last apply error live under `/var/lib/adsb-receiver/`. Wi-Fi secrets live only in root-readable NetworkManager keyfiles. Administrator passwords are stored as salted scrypt hashes. They are not part of the JSON configuration.

`readsb.service` depends only on the installed local configuration. A factory configuration is rendered into the image, so first boot starts decoding even without Ethernet, Wi-Fi, DNS, internet access, or receiver coordinates. Coordinates are required before setup can be completed.

## Hardware targets

`config/targets.json` is the target source of truth. Every enabled target gets an isolated build and artifact.

| Target | Armbian board | Status | Estimated minimum | Practical recommendation |
| --- | --- | --- | --- | --- |
| Orange Pi Zero3 | `orangepizero3` | Enabled, physical acceptance pending | 1 GiB RAM, 8 GiB SD, Ethernet, one USB host port | 2 GiB RAM, 16 GiB high-endurance SD, stable PSU, RTL-SDR Blog V3 or equivalent |
| Orange Pi Zero2 | `orangepizero2` | Enabled, physical acceptance pending | 1 GiB RAM, 8 GiB SD, Ethernet, one USB host port | 2 GiB RAM, 16 GiB high-endurance SD, stable PSU, RTL-SDR Blog V3 or equivalent |

The build pins the Armbian framework, board kernel revision, Debian Trixie, and readsb commit. `./build.sh` builds every enabled target or only the named targets. Output remains separated under `dist/<target>/`.

## Build and validation

Run cheap checks before an image build:

```fish
python3 scripts/validate_repository.py
python3 -m unittest discover -s tests -v
bash -n build.sh userpatches/customize-image.sh userpatches/extensions/adsb-kernel-pin.sh userpatches/overlay/etc/adsb-receiver/build-inputs.sh
git diff --check
```

Expected result:

```text
validated 2 target(s), 2 enabled
...
OK
```

The full Armbian build requires a privileged Docker-capable Linux host with at least 8 GiB RAM and 50 GiB free. From Fish on macOS, invoke the Bash script directly on that Linux host. Do not source it.

```fish
./build.sh
./build.sh orangepi-zero3
```

GitHub Actions is authoritative. `.github/workflows/validate.yml` runs the cheap gate. `.github/workflows/build-image.yml` is manual and uses the official Armbian Action pinned to `0620eb67885d19aeabd62655e60870ffd1efad63`. Appliance version `2026.08.22.1` is distinct from Armbian's internal version. Build artifacts include the compressed image, checksum, target snapshot, framework and OS revisions, kernel revision, readsb revision, and available Armbian source metadata.

## Flash and first boot

1. Download the image and checksum for the correct board from a GitHub Release.
2. Verify the checksum on macOS.
3. Flash with Raspberry Pi Imager or Balena Etcher. Confirm the target disk twice because flashing overwrites it.
4. Insert the card, connect the RTL-SDR, optionally connect Ethernet, and boot.
5. On first boot, the appliance writes `adsb-receiver/setup-credentials.txt` to the writable boot partition. This file contains the per-device setup SSID, unique AP password, setup URL, one-time setup password, and TLS certificate fingerprint.
6. Power the receiver down, read that file from the card on another computer, then boot again. This awkward little card shuffle avoids the much worse design of a universal setup password.

Checksum example:

```fish
shasum -a 256 -c adsb-receiver-orangepi-zero3-2026.08.22.1.img.xz.sha256
```

Expected result:

```text
adsb-receiver-orangepi-zero3-2026.08.22.1.img.xz: OK
```

If wired Ethernet has a usable DHCP address, Ethernet stays active and the setup file names its HTTPS URL. If Ethernet has no usable address, NetworkManager starts `ADSB-SETUP-<device suffix>` on `192.168.77.0/24`; the fixed gateway and setup URL are `https://192.168.77.1:8443/`. No separate `hostapd`, `dnsmasq`, or unmanaged `wpa_supplicant` configuration is installed.

Verify the displayed certificate fingerprint against `setup-credentials.txt` before accepting the per-device self-signed certificate. Authenticate as `setup` with the one-time setup password.

The onboarding page collects:

- Wi-Fi SSID and password, if Wi-Fi is wanted
- receiver name, latitude, longitude, and altitude in meters
- RTL-SDR gain, with `auto` recommended initially
- Beast and JSON listener enablement, addresses, and ports
- administrator password and run-mode management address

The server validates Wi-Fi input, writes a candidate root-only NetworkManager profile, and activates it before installing the persistent profile. Secrets never appear in a command argument or request log. On successful setup it atomically applies configuration, stores the last-known-good copy, disables the AP, removes bootstrap material where supported, and reboots into run mode.

## Run-mode networking

Ethernet and configured Wi-Fi can remain enabled together. The appliance sets the active Ethernet connection route metric to `100`; its generated Wi-Fi profile uses `600`, so Ethernet is preferred and Wi-Fi remains fallback. Loss of an uplink does not stop local decoding.

Run mode never enters setup merely because Ethernet is down. The AP starts only for an incomplete first boot or a validated recovery marker. NetworkManager owns managed network state throughout.

## Consumer endpoints

For a receiver at `192.168.1.40`:

| Consumer | Connection to configure |
| --- | --- |
| `adsb-feeder` | Beast TCP host `192.168.1.40`, port `30005` |
| SkySpy | Beast host `192.168.1.40`, port `30005`, or JSON base `http://192.168.1.40:8080/data/` if that deployment supports readsb JSON |
| Aerodrome | Beast host `192.168.1.40`, port `30005`, or JSON base `http://192.168.1.40:8080/data/` if supported by the installed version |

Container implementations use different environment variable names. The generic Compose wiring is:

```yaml
services:
  local-adsb-consumer:
    image: <consumer-image-and-pinned-tag>
    environment:
      # Map these values to the consumer's documented settings.
      RECEIVER_PROTOCOL: beast
      RECEIVER_HOST: 192.168.1.40
      RECEIVER_PORT: "30005"
      READSB_JSON_BASE_URL: http://192.168.1.40:8080/data/
```

Do not copy those generic variable names blindly. Use the actual names supported by the pinned `adsb-feeder`, SkySpy, or Aerodrome image. The stable appliance-side contract is the host, protocol, port, and JSON paths.

Test the read-only HTTP service:

```fish
curl -fsS http://192.168.1.40:8080/data/receiver.json | jq
curl -fsS http://192.168.1.40:8080/data/aircraft.json | jq '.aircraft | length'
```

Expected result:

```text
{
  "version": "...",
  "refresh": 1000,
  "lat": 41.8781,
  "lon": -87.6298
}
0
```

Zero aircraft is valid when reception is quiet. HTTP `503` means readsb has not produced the requested file yet.

The pinned readsb contract uses `--net-bo-port 30005` for its Beast TCP server, `--net-bind-address` for the Beast bind, and `--write-json /run/readsb --write-json-every 1` for atomic JSON files. The small HTTP service exposes only `aircraft.json`, `receiver.json`, and `stats.json`; it has no write or admin routes. readsb accepts multiple concurrent Beast clients.

The pinned readsb revision accepts receiver latitude and longitude but does not define a `--alt` option. Altitude remains validated appliance metadata for onboarding and consumers; it is intentionally not emitted as a readsb argument.

## Local configuration contract

The machine-readable schema is `schemas/receiver-config.schema.json`; a complete example is `examples/local-config.json`. Schema version 2 rejects unknown fields and arbitrary readsb arguments. Supported advanced options are limited to PPM correction and maximum range.

Important rules:

- `setupComplete: true` requires latitude, longitude, and altitude.
- Beast, JSON HTTP, and admin ports must be distinct when enabled.
- Beast and JSON addresses can be `0.0.0.0`, loopback, link-local, or a private IPv4 address.
- Admin cannot bind `0.0.0.0` or a public literal address. It defaults to localhost.
- Wi-Fi and administrator secrets are not schema fields.
- Optional outbound connectors support only `beast_out` and `beast_reduce_plus_out`.
- The setup subnet, gateway, and recovery marker are fixed safety contracts.

Validate and apply locally on the appliance:

```bash
sudo adsb-config validate /path/to/candidate.json
sudo adsb-config apply /path/to/candidate.json
sudo adsb-config status
```

Expected result:

```text
{
  "mode": "run",
  "services": {
    "readsb.service": "active",
    "adsb-json-http.service": "active",
    "adsb-admin.service": "active"
  },
  ...
}
```

These are device-side Bash commands. Controller-side macOS examples in this document use Fish-compatible syntax.

## Web administration security and access

Configuration mode binds only to the selected configuration interface: the usable wired address or `192.168.77.1` on the setup AP. Run mode binds to `admin.address`. The default `127.0.0.1` deliberately provides no unauthenticated LAN administration. To administer from a LAN browser, set one specific private management address during onboarding.

All admin modes use a per-device self-signed TLS certificate with TLS 1.2 or newer. Run-mode writes require HTTP Basic authentication with the salted scrypt password hash, a same-origin check, an unpredictable CSRF header token, and a matching `Secure`, `HttpOnly`, `SameSite=Strict` cookie. Request bodies and secrets are not logged.

To disable the run-mode UI, set `admin.enabled` to `false` and apply. To re-enable it, use recovery mode and set it to `true`. To change the password, enter a new one on the admin page. Leaving the password field empty preserves the current hash.

To verify the installed certificate fingerprint on the appliance:

```bash
openssl x509 -in /etc/adsb-receiver/admin-cert.pem -noout -fingerprint -sha256
```

Expected result:

```text
sha256 Fingerprint=AA:BB:...:FF
```

Compare every byte with the fingerprint obtained from the boot-partition setup file or a previously trusted record.

## Non-SSH recovery

To request configuration mode on the next boot:

1. Power down the appliance and mount its writable boot partition on another computer.
2. Create `adsb-receiver/recovery-request` containing exactly `ADSB-RECEIVER-CONFIG-MODE` plus one newline.
3. Reinsert the card and boot.
4. Read the newly generated `adsb-receiver/setup-credentials.txt`, then use the wired setup URL or temporary AP.

Fish-friendly macOS example after confirming the exact mounted boot volume:

```fish
mkdir -p /Volumes/ADSB-BOOT/adsb-receiver
printf 'ADSB-RECEIVER-CONFIG-MODE\n' > /Volumes/ADSB-BOOT/adsb-receiver/recovery-request
diskutil unmount /Volumes/ADSB-BOOT
```

Expected result:

```text
Volume ADSB-BOOT on disk... unmounted
```

The appliance accepts only a regular file with exact content at the fixed path. A misspelled, oversized, symlinked, or differently formatted marker is ignored. Entering recovery mode does not expose a run-mode admin listener on every interface. After successful setup, the marker and temporary AP are removed.

If a bad configuration prevents readsb from staying active, apply automatically restores the previous files and records the error. An administrator can also invoke:

```bash
sudo adsb-config rollback
```

Expected result: `readsb.service` returns to `active` using `/var/lib/adsb-receiver/last-known-good.json`. Reflashing is the final recovery path, not the first one.

## Troubleshooting

Service and SDR checks:

```bash
systemctl status readsb.service adsb-json-http.service adsb-admin.service adsb-network-mode.service
adsb-config status
lsusb -d 0bda:2838
ss -lntp | grep -E ':(30005|8080|8443)\b'
```

Expected healthy indicators:

```text
Active: active (running)
Bus ... ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T
LISTEN ... :30005 ... readsb
LISTEN ... :8080 ... adsb-json-server
```

The admin listener appears only when enabled and bound to an address present on the device. A different RTL-SDR USB ID can be healthy even if the exact `lsusb -d` check is empty.

Recent errors, with no secret-bearing request bodies:

```bash
journalctl -u adsb-initialize -u adsb-network-mode -u readsb -u adsb-json-http -u adsb-admin --since today
sudo cat /var/lib/adsb-receiver/last-apply-error.json
nmcli device status
nmcli connection show --active
```

Expected healthy network state resembles:

```text
DEVICE  TYPE      STATE      CONNECTION
end0    ethernet  connected  Wired connection 1
wlan0   wifi      connected  adsb-wifi
```

Do not include NetworkManager keyfiles, `setup-credentials.txt`, HTTP Authorization headers, or `/var/lib/adsb-receiver/bootstrap-state.json` in diagnostics.

## Retired central configuration workflow

The mandatory signed config-server design, Minisign key, refresh timer, agent, Caddy example, and schema version 1 are removed. There is no normal startup fetch and no required `adsb-platform` deployment. A future remote management feature must be separately authenticated, explicitly enabled, disabled by default, and feed the same local validate-and-apply path. It must never become a readsb startup dependency.

## What validation does and does not prove

Repository tests cover schema validation, deterministic readsb arguments, first boot without internet, configuration-mode decisions, exact recovery markers, atomic apply and rollback, secret redaction, password authentication, CSRF checks, NetworkManager keyfile generation, firewall policy, systemd structure, documentation constants, immutable build pins, and workflow action pins.

That evidence does not prove an image boots or that the board-specific Ethernet name, writable boot layout, Wi-Fi AP mode, NetworkManager shared addressing, USB power, RTL-SDR access, Beast stream, JSON compatibility, TLS browser flow, or consumer applications work on physical hardware. Those are acceptance tests, not vibes.
