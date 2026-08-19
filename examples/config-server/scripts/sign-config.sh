#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# == 2 ]] || { echo "usage: $0 PRIVATE_KEY CONFIG.json" >&2; exit 64; }
minisign -Sm "$2" -s "$1" -x "$2.minisig"
