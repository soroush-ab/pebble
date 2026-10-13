#!/usr/bin/env bash
# Host tests for the bearing math. No ESP-IDF, no hardware needed.
set -euo pipefail
cd "$(dirname "$0")"
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
gcc -std=c99 -Wall -Wextra -Wno-unused-parameter -O2 \
    -D_GNU_SOURCE \
    host_test.c ../mic_array.c ../tdoa.c -lm -o "$OUT/host_test"
"$OUT/host_test"
