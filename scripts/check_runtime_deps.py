#!/usr/bin/env python3
"""Fail the build when the emulator links a library no package accounts for.

QUICKSTART.md tells a Linux user which packages to install before the bundled
emulator will start. That list is only trustworthy if something checks it, so
this script takes the `ldd` output release.yml already prints and asserts every
linked library is accounted for by the mapping below.

The mapping is the real deliverable. It is what a packager on a non-Debian
distro needs in order to translate the install line, and committing it means
the next library the emulator picks up becomes a failed build here rather than
a user's bug report about a binary that will not start.

Provenance: derived on 2026-08-21 from the Ubuntu 24.04 (noble) archive
indices -- dists/noble/Contents-amd64.gz for soname ownership, and the
main/universe Packages indices for the dependency closure. 24.04 because that
is what `ubuntu-latest` resolves to for the linux-bundle job, and what the
released binary is therefore built and linked against. The sonames come from
the ldd output of the v0.1.1 release build (run 32076731098).

Package names are Ubuntu 24.04 names, including the t64 transition spellings
(libasound2t64, libpcap0.8t64, libpng16-16t64). A different distro will spell
them differently; the soname column is the part that transfers.

Usage:
    ldd path/to/dosbox-x | python3 scripts/check_runtime_deps.py
    python3 scripts/check_runtime_deps.py path/to/dosbox-x

Exits 0 when every linked library is mapped or allowlisted, 1 otherwise.
"""

import re
import subprocess
import sys

# Provided by glibc and the GCC runtime, so they are covered by the documented
# glibc 2.38+ / libstdc++ (GCC 13+) floor in QUICKSTART.md rather than by any
# package a user installs. See the "minimum distro" section there: a release
# old enough to lack these fails at startup naming a GLIBC_2.xx version, which
# is a distro-version problem and not a missing-package problem.
GLIBC_ALLOWLIST = {
    "libc.so.6",
    "libm.so.6",
    "libpthread.so.0",
    "libdl.so.2",
    "librt.so.1",
    "libgcc_s.so.1",
    "libstdc++.so.6",
}

# Matched by prefix: the dynamic loader's soname carries the architecture.
GLIBC_PREFIX_ALLOWLIST = ("ld-linux",)

# The kernel supplies this one; it has no file on disk and no owning package.
VDSO = "linux-vdso.so.1"

# soname -> (Ubuntu 24.04 package, origin)
#   "direct"     = named in QUICKSTART.md's apt-get line
#   "transitive" = arrives as a dependency of one of those
# That distinction is the finding, not decoration: it is what says whether the
# install line names a package or merely gets lucky through someone else's
# dependencies.
RUNTIME_LIBS = {

    # Named directly in QUICKSTART's apt-get line.
    "libasound.so.2": ("libasound2t64", "direct"),  # also in: liboss4-salsa-asound2
    "libfluidsynth.so.3": ("libfluidsynth3", "direct"),
    "libGL.so.1": ("libgl1", "direct"),
    "libncurses.so.6": ("libncurses6", "direct"),
    # QUICKSTART.md installs this as "libpcap0.8", which on 24.04 is a
    # virtual package with exactly one provider -- apt resolves it to
    # libpcap0.8t64, the real package named here.
    "libpcap.so.0.8": ("libpcap0.8t64", "direct"),
    "libpng16.so.16": ("libpng16-16t64", "direct"),
    "libSDL2-2.0.so.0": ("libsdl2-2.0-0", "direct"),
    "libSDL2_net-2.0.so.0": ("libsdl2-net-2.0-0", "direct"),
    "libslirp.so.0": ("libslirp0", "direct"),

    # Pulled in as a dependency of one of the packages above.
    "libapparmor.so.1": ("libapparmor1", "transitive"),
    "libasyncns.so.0": ("libasyncns0", "transitive"),
    "libbsd.so.0": ("libbsd0", "transitive"),
    "libcap.so.2": ("libcap2", "transitive"),
    "libdb-5.3.so": ("libdb5.3t64", "transitive"),
    "libdbus-1.so.3": ("libdbus-1-3", "transitive"),
    "libdecor-0.so.0": ("libdecor-0-0", "transitive"),
    "libdrm.so.2": ("libdrm2", "transitive"),
    "libexpat.so.1": ("libexpat1", "transitive"),
    "libffi.so.8": ("libffi8", "transitive"),
    "libFLAC.so.12": ("libflac12t64", "transitive"),
    "libgbm.so.1": ("libgbm1", "transitive"),
    "libgcrypt.so.20": ("libgcrypt20", "transitive"),
    "libGLdispatch.so.0": ("libglvnd0", "transitive"),
    "libglib-2.0.so.0": ("libglib2.0-0t64", "transitive"),
    "libGLX.so.0": ("libglx0", "transitive"),
    "libgmodule-2.0.so.0": ("libglib2.0-0t64", "transitive"),
    "libgobject-2.0.so.0": ("libglib2.0-0t64", "transitive"),
    "libgomp.so.1": ("libgomp1", "transitive"),
    "libgpg-error.so.0": ("libgpg-error0", "transitive"),
    "libibverbs.so.1": ("libibverbs1", "transitive"),
    "libinstpatch-1.0.so.2": ("libinstpatch-1.0-2", "transitive"),
    "libjack.so.0": ("libjack-jackd2-0", "transitive"),  # also in: libjack0
    "liblz4.so.1": ("liblz4-1", "transitive"),
    "liblzma.so.5": ("liblzma5", "transitive"),
    "libmd.so.0": ("libmd0", "transitive"),
    "libmp3lame.so.0": ("libmp3lame0", "transitive"),
    "libmpg123.so.0": ("libmpg123-0t64", "transitive"),
    "libnl-3.so.200": ("libnl-3-200", "transitive"),
    "libnl-route-3.so.200": ("libnl-route-3-200", "transitive"),
    "libogg.so.0": ("libogg0", "transitive"),
    "libopus.so.0": ("libopus0", "transitive"),
    "libpcre2-8.so.0": ("libpcre2-8-0", "transitive"),
    "libpipewire-0.3.so.0": ("libpipewire-0.3-0t64", "transitive"),
    "libpulse-simple.so.0": ("libpulse0", "transitive"),
    "libpulse.so.0": ("libpulse0", "transitive"),
    "libpulsecommon-16.1.so": ("libpulse0", "transitive"),
    "libreadline.so.8": ("libreadline8t64", "transitive"),
    "libsamplerate.so.0": ("libsamplerate0", "transitive"),
    "libsndfile.so.1": ("libsndfile1", "transitive"),
    "libsystemd.so.0": ("libsystemd0", "transitive"),
    "libtinfo.so.6": ("libtinfo6", "transitive"),
    "libvorbis.so.0": ("libvorbis0a", "transitive"),
    "libvorbisenc.so.2": ("libvorbisenc2", "transitive"),
    "libwayland-client.so.0": ("libwayland-client0", "transitive"),
    "libwayland-cursor.so.0": ("libwayland-cursor0", "transitive"),
    "libwayland-egl.so.1": ("libwayland-egl1", "transitive"),
    "libX11-xcb.so.1": ("libx11-xcb1", "transitive"),
    "libX11.so.6": ("libx11-6", "transitive"),
    "libXau.so.6": ("libxau6", "transitive"),
    "libxcb.so.1": ("libxcb1", "transitive"),
    "libXcursor.so.1": ("libxcursor1", "transitive"),
    "libXdmcp.so.6": ("libxdmcp6", "transitive"),
    "libXext.so.6": ("libxext6", "transitive"),
    "libXfixes.so.3": ("libxfixes3", "transitive"),
    "libXi.so.6": ("libxi6", "transitive"),
    "libxkbcommon.so.0": ("libxkbcommon0", "transitive"),
    "libXrandr.so.2": ("libxrandr2", "transitive"),
    "libXrender.so.1": ("libxrender1", "transitive"),
    "libXss.so.1": ("libxss1", "transitive"),
    "libz.so.1": ("zlib1g", "transitive"),
    "libzstd.so.1": ("libzstd1", "transitive"),
}

# ldd prints one of:
#     "\tlinux-vdso.so.1 (0x00007ffd...)"
#     "\tlibfoo.so.1 => /lib/x86_64-linux-gnu/libfoo.so.1 (0x00007f...)"
#     "\tlibfoo.so.1 => not found"
#     "\t/lib64/ld-linux-x86-64.so.2 (0x00007f...)"
_LINE = re.compile(
    r"^\s*(?P<soname>\S+?)"
    r"(?:\s*=>\s*(?P<target>not found|\S+))?"
    r"(?:\s*\(0x[0-9a-f]+\))?\s*$"
)


def parse_ldd(text):
    """Yield (soname, target) for each real ldd line, ignoring the rest."""
    for line in text.splitlines():
        if not line.strip() or "=>" not in line and "(0x" not in line:
            continue
        m = _LINE.match(line)
        if not m:
            continue
        soname = m.group("soname").rsplit("/", 1)[-1]
        if not soname.startswith("lib") and not soname.startswith("ld-linux"):
            continue
        yield soname, m.group("target")


def is_allowlisted(soname):
    return soname in GLIBC_ALLOWLIST or soname.startswith(GLIBC_PREFIX_ALLOWLIST)


def check(text):
    unmapped, not_found, mapped = [], [], []
    for soname, target in parse_ldd(text):
        if soname == VDSO:
            continue
        if target == "not found":
            not_found.append(soname)
            continue
        if is_allowlisted(soname):
            continue
        if soname in RUNTIME_LIBS:
            mapped.append(soname)
        else:
            unmapped.append(soname)

    print("checked %d linked libraries" % (len(mapped) + len(unmapped)))

    if not_found:
        print()
        print("FAIL: the linker could not resolve these at all:")
        for soname in sorted(set(not_found)):
            print("    %s" % soname)

    if unmapped:
        print()
        print("FAIL: linked but not accounted for by any package in the mapping:")
        for soname in sorted(set(unmapped)):
            print("    %s" % soname)
        print()
        print("The emulator picked up a library this repo has never described.")
        print("Find its owning package and decide which case it is:")
        print("  * pulled in by a package QUICKSTART.md already names -> add it")
        print("    to RUNTIME_LIBS as \"transitive\"")
        print("  * owned by a package nobody installs -> add it to RUNTIME_LIBS")
        print("    as \"direct\" AND to the apt-get line in QUICKSTART.md, or the")
        print("    bundle will not start on a minimal system")
        print()
        print("On Ubuntu, the owning package is:")
        print("    dpkg -S $(ldconfig -p | awk '$1==\"<soname>\" {print $NF; exit}')")

    if not_found or unmapped:
        return 1

    direct = sum(1 for s in mapped if RUNTIME_LIBS[s][1] == "direct")
    print("all accounted for: %d from packages QUICKSTART names, %d transitive"
          % (direct, len(mapped) - direct))
    return 0


def main(argv):
    if len(argv) > 2:
        print("usage: %s [binary]   (or pipe ldd output on stdin)" % argv[0],
              file=sys.stderr)
        return 2
    if len(argv) == 2:
        try:
            text = subprocess.run(["ldd", argv[1]], check=True,
                                  capture_output=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            print("FAIL: could not run ldd on %s: %s" % (argv[1], exc),
                  file=sys.stderr)
            return 2
    else:
        text = sys.stdin.read()
        if not text.strip():
            print("FAIL: no ldd output on stdin", file=sys.stderr)
            return 2
    return check(text)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
