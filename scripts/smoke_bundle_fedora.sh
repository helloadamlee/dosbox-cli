#!/usr/bin/env bash
# Run a released Linux bundle on Fedora, to test the one thing Ubuntu CI
# cannot: whether a distro that packages things differently can actually run
# what we ship.
#
# Run this inside a clean Fedora 39+ root -- a container, or a WSL distro
# imported from Fedora's container base image. It installs packages and
# creates a symlink under /usr/lib64, so do not point it at a system you care
# about.
#
#   ./scripts/smoke_bundle_fedora.sh /path/to/dosbox-cli-<ver>-linux-x86_64
#
# Findings this encodes, from a run on Fedora 43 (glibc 2.42) against the
# v0.1.1 bundle on 2026-08-21:
#
#   * libXrandr must be installed explicitly. Ubuntu's libsdl2-2.0-0 depends
#     on libxrandr2, so it arrives for free there; Fedora's SDL2 lists it as
#     neither a Requires nor a Recommends. This is the "packages split
#     differently" case, and it is why translating the Debian list package for
#     package is not enough.
#
#   * libpcap is the real obstacle, and no package fixes it. Debian and Ubuntu
#     ship libpcap with the historical soname libpcap.so.0.8; Fedora ships the
#     upstream soname libpcap.so.1. Nothing in Fedora provides libpcap.so.0.8,
#     so the emulator will not start: the loader fails outright. Both are the
#     same upstream 1.10.x series (Ubuntu noble 1.10.4, Fedora 43 1.10.6) and
#     the difference is a packaging convention rather than an ABI break, so the
#     compat symlink below is sound today -- but it is a symlink across a
#     soname boundary, and it is only justified because the upstream versions
#     match. Check that before trusting it on a future release.
#
#   * libGL and libpng16 are missing on a bare Fedora too, which is the same
#     gap scripts/check_runtime_deps.py found on the Ubuntu side.
#
# Exits 0 only if ldd resolves everything and a real DOS command runs.

set -o errexit
set -o nounset
set -o pipefail

bundle="${1:-}"
if [ -z "${bundle}" ] || [ ! -d "${bundle}" ]; then
    echo "usage: $0 /path/to/dosbox-cli-<version>-linux-x86_64" >&2
    exit 2
fi
bundle="$(cd "${bundle}" && pwd)"
binary="${bundle}/dosbox-x/dosbox-x"
test -x "${binary}" || { echo "FAIL: no emulator at ${binary}" >&2; exit 2; }

echo "=== 1. before installing anything ==="
ldd "${binary}" 2>/dev/null | grep "not found" || echo "  (nothing missing)"

echo
echo "=== 2. installing the Fedora equivalents ==="
# The direct translation of QUICKSTART.md's Debian list, plus libXrandr, which
# Fedora's SDL2 does not pull in.
dnf install -y --setopt=install_weak_deps=False \
    SDL2 SDL2_net alsa-lib ncurses-libs libpcap libslirp fluidsynth-libs \
    mesa-libGL libpng libXrandr python3-pip

echo
echo "=== 3. libpcap soname compatibility ==="
if ! ldconfig -p | grep -q "libpcap\.so\.0\.8"; then
    target="$(ldconfig -p | awk '/libpcap\.so\.1 /{print $NF; exit}')"
    if [ -z "${target}" ]; then
        echo "FAIL: no libpcap at all; install libpcap" >&2
        exit 1
    fi
    echo "  Fedora has $(basename "${target}"), the bundle wants libpcap.so.0.8"
    echo "  linking libpcap.so.0.8 -> ${target}"
    ln -sf "${target}" "$(dirname "${target}")/libpcap.so.0.8"
    ldconfig
fi

echo
echo "=== 4. after install ==="
if ldd "${binary}" 2>/dev/null | grep "not found"; then
    echo "FAIL: libraries still unresolved" >&2
    exit 1
fi
echo "  ldd clean"

echo
echo "=== 5. real DOS command through the emulator ==="
# smoke_bundle.py lives in the repo, not in the bundle; pass this script's
# sibling copy when running from a checkout.
smoke="$(dirname "$0")/smoke_bundle.py"
test -f "${smoke}" || { echo "FAIL: cannot find ${smoke}" >&2; exit 2; }

venv="$(mktemp -d)/venv"
python3 -m venv "${venv}"
"${venv}/bin/pip" install --quiet "${bundle}/mcp-server"
"${venv}/bin/python" "${smoke}" "${bundle}"
