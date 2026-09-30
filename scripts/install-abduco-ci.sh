#!/usr/bin/env bash
# Build a pinned abduco from source into a job-local dir and put it on the CI job's PATH.
#
# Why from source: `rig omp run` execs abduco, and the omp-restore positive control in
# tests/test_tmux_e2e.py proves a restored pane really relaunches omp through it. Ubuntu 24.04
# (the Forgejo runner host) has no abduco package, so apt cannot provide it. Job-local, not
# `make install` into /usr/local: jobs share the runner host, and the matrix jobs would race on
# one system-wide path. Needs a C compiler + make (build-essential, installed by the apt step).
set -euo pipefail

ABDUCO_VERSION="0.6"
# sha256 of abduco-<version>.tar.gz (the GitHub release asset; Homebrew pins the same) — bump both together
ABDUCO_SHA256="c90909e13fa95770b5afc3b59f311b3d3d2fdfae23f9569fa4f96a3e192a35f4"

root="${RUNNER_TEMP:-$(mktemp -d)}/abduco-${ABDUCO_VERSION}"
rm -rf "$root"
mkdir -p "$root/bin"
tgz="$root/abduco.tar.gz"
curl -fsSL -o "$tgz" "https://github.com/martanne/abduco/releases/download/v${ABDUCO_VERSION}/abduco-${ABDUCO_VERSION}.tar.gz"
echo "${ABDUCO_SHA256}  $tgz" | sha256sum -c -
tar -xzf "$tgz" -C "$root"
make -C "$root/abduco-${ABDUCO_VERSION}" abduco
install -m 0755 "$root/abduco-${ABDUCO_VERSION}/abduco" "$root/bin/abduco"
echo "$root/bin" >> "$GITHUB_PATH"
"$root/bin/abduco" -v
