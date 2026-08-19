# Stateless ADS-B receiver image

This repository builds a small, reproducible, appliance-style Debian image for
RTL-SDR ADS-B reception. It starts with the Orange Pi Zero 3 and the official
Armbian build framework, not a prebuilt feeder image. The receiver decodes
locally with Wiedehopf's `readsb` and forwards Beast data to the central ADS-B
server. It contains no UI, database, dashboard, Docker daemon, history, or
general-purpose feeder stack. That stuff belongs at the center where it can be
maintained once instead of multiplied by every antenna.

## Hardware targets and minimums

`config/targets.json` is the source of truth. Every `enabled: true` target is
built by `./build.sh`; passing target names limits the build. Artifacts are
written separately under `dist/<target>/`, so a mixed configuration produces an
image per board without cross-contaminating names or manifests.

| Target | Build board | Absolute minimum | Practical recommendation |
| --- | --- | --- | --- |
| Orange Pi Zero 3 | `orangepizero3` | 1 GiB RAM, 8 GiB SD card, Ethernet, one USB host port | 2 GiB RAM, 16 GiB high-endurance SD card, RTL-SDR Blog V3 or equivalent |

For a future device, add a target with its Armbian `BOARD` name, architecture,
and `minimumHardware`. As a planning floor, do not add a receiver below 1 GiB
RAM, 8 GiB storage, one reliable USB host port, and wired Ethernet. The image
does not need much CPU, but weak power supplies and bargain SD cards create the
kind of mystery failures that waste a Saturday.

## Architecture

```text
RTL-SDR -> readsb on receiver -> BeastReduce+ -> central readsb/MLAT/tar1090
                     ^
                     | signed JSON
              adsb-config-agent <- HTTPS config server
```

At boot, `adsb-config-agent.service` waits for network, downloads JSON plus its
minisign signature, verifies it with the image's public key, validates the
strict schema, renders a deterministic readsb argument file, and atomically
installs it. `readsb` starts only when that argument file exists. A failed or
unavailable fetch leaves the last known good configuration running. The refresh
timer defaults to 15 minutes; the schema accepts 60 seconds through 24 hours.

Identity preference is an explicitly provisioned `/etc/adsb-receiver/receiver-id`,
then the first non-loopback Ethernet MAC, then a persistent generated UUID.
The URL template in `config/build.json` substitutes `{receiver_id}`. A shared
endpoint is also possible by using a URL without that placeholder.

## Build

Build on a Linux host with Docker, privilege to run the Armbian framework, at
least 8 GiB RAM, and 50 GiB free disk. macOS is fine for editing and flashing,
but use a Linux runner or VM for the actual image build.

```bash
./build.sh                         # every enabled hardware target
./build.sh orangepi-zero3          # only this target
```

Fish users can run those commands unchanged. `build.sh` declares Bash itself,
so do not source it into Fish. A native macOS build is unsupported: use a
Linux VM, a dedicated Linux host, or the privileged Gitea runner instead.

The build pins the Armbian framework to commit
`9de7be05323564424cf64171cb483712ec356bc1`, Debian `trixie`, and readsb commit
`d9a4c62655490e70d07704e207738bb9c6cffde1` in `config/build.json`. Each target
also pins its kernel commit in `config/targets.json`; this avoids a build
silently selecting a newer rolling stable-kernel revision than the framework's
patch stack supports. The build uses readsb's supported Debian package path with
RTL-SDR support, so it never compiles on normal device boot. Each artifact
directory contains an `.img.xz`, SHA-256 checksum, and a build manifest.

Before a production build, replace the example public key at
`userpatches/overlay/etc/adsb-receiver/publickey.minisign`, set the URL template,
and copy `config/authorized_keys.example` to the ignored
`config/authorized_keys`, replacing its contents with administrator public keys.
The build refuses to continue without that nonempty file. The image creates the
`admin` user with passwordless sudo, disables password and root SSH login, and
installs only those supplied keys.

## Configuration and signing

The machine-readable contract is [schemas/receiver-config.schema.json](schemas/receiver-config.schema.json).
An example lives in [examples/config-server/config/default.json](examples/config-server/config/default.json).
Only a narrow allowlist of readsb extra options is accepted, and output hosts,
ports, protocols, coordinates, and intervals are validated. `beast_reduce_plus_out`
is the default because it is the intended reduced Beast forwarding path; choose
`beast_out` explicitly only when central MLAT testing proves it is required.

Generate a signing key on an administrator workstation, install only its public
key in the image, then sign each JSON file before deployment:

```bash
minisign -G -p receiver-config.pub -s receiver-config.key
examples/config-server/scripts/sign-config.sh receiver-config.key config/receiver-a.json
```

Keep `receiver-config.key` off the image, repository, Caddy container, CI logs,
and build artifacts. The Caddy example merely serves already signed static files.

## CI and recovery

`.gitea/workflows/validate.yml` runs shell, JSON, unit, and simple secret checks.
`.gitea/workflows/build-image.yml` is manual and requires a privileged Linux
Docker runner with at least 8 GiB RAM and 50 GiB free. The workflow creates an
ephemeral unprivileged builder user with access to the mounted Docker socket,
because Armbian must not run `compile.sh` as root. It intentionally does not
build an Armbian image for every push.

To recover: download a known image, verify its SHA-256, flash it, connect Ethernet
and the SDR, and boot. The receiver retrieves its configuration and resumes from
the server. For diagnostics:

```bash
systemctl status adsb-config-agent.service readsb.service adsb-config-refresh.timer
journalctl -u adsb-config-agent -u readsb
adsb-config-agent status
lsusb
```

Flashing with `dd` overwrites the selected disk. Prefer Raspberry Pi Imager or
Balena Etcher. On macOS, identify the removable disk with `diskutil list`, unmount
the exact disk, then use `sudo dd if=image.img of=/dev/rdiskN bs=4m status=progress`.
Double-check `N` before pressing Enter. `dd` is called disk destroyer for a reason.

The appliance does not run unattended distribution upgrades. Rebuild and test a
new image for OS and application updates; normal receiver changes are signed
runtime configuration and do not require an image rebuild.

## Build-time versus deployed proof

The repository validates the agent and configuration logic locally, but it does
not pretend that is hardware proof. Before declaring a target ready, run a full
build on the required Linux runner, flash an SD card, verify Ethernet and an
actual RTL-SDR, confirm a signed initial fetch, test a rejected signature and
server outage against the last-known-good cache, then verify forwarding at the
central server.
