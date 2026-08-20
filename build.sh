#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
build_config="$repo_root/config/build.json"
targets_config="$repo_root/config/targets.json"
armbian_dir="$repo_root/.armbian-build"
dist_dir="$repo_root/dist"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

usage() {
  cat <<'EOF'
Usage: ./build.sh [target ...]

Builds every enabled target when no target is given. Each target name must be a
key from config/targets.json. The build host must be Linux with Docker and the
privileges required by the official Armbian build framework.
EOF
}

[[ ${1:-} != "--help" && ${1:-} != "-h" ]] || { usage; exit 0; }
[[ $(uname -s) == "Linux" ]] || die "Armbian image builds require a privileged Linux host. Run this in a Linux VM or GitHub Actions, not macOS."
require git; require jq; require sha256sum; require xz
require docker
docker info >/dev/null 2>&1 || die "Docker must be running and accessible to the current user"
[[ -f "$build_config" && -f "$targets_config" ]] || die "missing build configuration"

armbian_repo=$(jq -er '.armbian.repository' "$build_config")
armbian_revision=$(jq -er '.armbian.revision' "$build_config")
image_version=$(jq -er '.imageVersion' "$build_config")
ssh_keys_file=$(jq -er '.defaults.sshAuthorizedKeysFile' "$build_config")
[[ "$ssh_keys_file" != /* ]] && ssh_keys_file="$repo_root/$ssh_keys_file"
[[ -s "$ssh_keys_file" ]] || die "administrator SSH keys are required at $ssh_keys_file (start with config/authorized_keys.example)"
git_commit=$(git -C "$repo_root" rev-parse --verify HEAD 2>/dev/null || printf 'uncommitted')

if [[ ! -d "$armbian_dir/.git" ]]; then
  git clone "$armbian_repo" "$armbian_dir"
fi
git -C "$armbian_dir" fetch --tags --force
git -C "$armbian_dir" checkout --detach "$armbian_revision"

if (($# == 0)); then
  build_targets=()
  while IFS= read -r target; do
    build_targets[${#build_targets[@]}]="$target"
  done < <(jq -r '.targets | to_entries[] | select(.value.enabled == true) | .key' "$targets_config")
else
  build_targets=("$@")
fi
[[ ${build_targets[0]+set} ]] || die "no enabled build targets"
mkdir -p "$dist_dir"

for target in "${build_targets[@]}"; do
  jq -e --arg target "$target" '.targets[$target]' "$targets_config" >/dev/null || die "unknown target: $target"
  board=$(jq -er --arg target "$target" '.targets[$target].board' "$targets_config")
  arch=$(jq -er --arg target "$target" '.targets[$target].architecture' "$targets_config")
  release=$(jq -er '.armbian.release' "$build_config")
  branch=$(jq -er --arg target "$target" '.targets[$target].armbian.branch' "$targets_config")
  kernel_revision=$(jq -er --arg target "$target" '.targets[$target].armbian.kernelRevision' "$targets_config")
  target_dir="$dist_dir/$target"
  rm -rf "$target_dir"
  mkdir -p "$target_dir"
  rm -rf "$armbian_dir/output/images"
  cp -a "$repo_root/userpatches/." "$armbian_dir/userpatches/"
  ADSB_IMAGE_VERSION="$image_version" ADSB_GIT_COMMIT="$git_commit" ADSB_ARMBIAN_REVISION="$armbian_revision" \
    ADSB_TARGET="$target" ADSB_TARGET_ARCH="$arch" ADSB_READSB_REVISION="$(jq -er '.readsb.revision' "$build_config")" \
    ADSB_CONFIG_URL_TEMPLATE="$(jq -er '.defaults.configUrlTemplate' "$build_config")" \
    ADSB_ADMIN_AUTHORIZED_KEYS="$(<"$ssh_keys_file")" \
    "$armbian_dir/compile.sh" \
      BOARD="$board" BRANCH="$branch" RELEASE="$release" KERNELBRANCH="commit:$kernel_revision" BUILD_MINIMAL=yes BUILD_DESKTOP=no \
      KERNEL_CONFIGURE=no CLEAN_LEVEL=make,oldcache
  images=()
  while IFS= read -r candidate; do
    images[${#images[@]}]="$candidate"
  done < <(find "$armbian_dir/output/images" -maxdepth 1 -type f -name '*.img' -print)
  if [[ ! ${images[0]+set} || ${images[1]+set} ]]; then
    die "expected exactly one uncompressed image for $target, found $(find "$armbian_dir/output/images" -maxdepth 1 -type f -name '*.img' -print | wc -l | tr -d ' ')"
  fi
  image="$target_dir/adsb-receiver-${target}-${image_version}.img"
  cp "${images[0]}" "$image"
  xz -T0 -9e "$image"
  sha256sum "${image}.xz" > "${image}.xz.sha256"
  jq -n --arg target "$target" --arg board "$board" --arg arch "$arch" --arg version "$image_version" --arg git "$git_commit" --arg armbian "$armbian_revision" --arg kernel "$kernel_revision" --arg release "$release" \
    '{target:$target,board:$board,architecture:$arch,imageVersion:$version,repositoryCommit:$git,armbianRevision:$armbian,kernelRevision:$kernel,debianRelease:$release}' > "$target_dir/build-manifest.json"
done
