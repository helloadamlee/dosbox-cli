# Known issues

Loose ends from shipping the v0.1.0 / v0.1.1 release bundles. Nothing
here blocks the current release — these are the things worth knowing
about before touching that area again.

For the host-control protocol's own open questions (reconnect support,
the ROM/VIDRAM MCB defect, a windowed-focus stall that's still
unreproduced), see [`host-control-windows-pipe-roadmap.md`](host-control-windows-pipe-roadmap.md)
instead — this doc is just the release-bundle side of things.

## macOS app bundling still points at the old path

`Makefile.am:45` calls `./appbundledeps.py` instead of
`scripts/appbundledeps.py`, left over from the `3fb7ef870` reorg. It's the
last of that batch still broken.

It stays broken on purpose. It sits inside the `if MACOSX` block, and there
is no macOS anything in this project — no build job, no machine, no way to
exercise a fix. A blind one-line edit that nobody can run isn't better than
a visibly broken path. If it's ever wanted it's exactly that one line.

## The Linux bundle needs a symlink on non-Debian distros

`libpcap` is the sticking point, and no package fixes it. Debian and Ubuntu
ship libpcap under its historical soname `libpcap.so.0.8`; everyone else
follows upstream and ships `libpcap.so.1`. Nothing on Fedora provides the
Debian name, so the emulator doesn't start at all — it dies in the loader
before any of our code runs:

```
error while loading shared libraries: libpcap.so.0.8:
cannot open shared object file: No such file or directory
```

A compatibility symlink clears it, and `QUICKSTART.md` now carries that plus
the `dnf` line that works on Fedora. The symlink is sound because both
distros are on the same upstream release series — Ubuntu 24.04 has libpcap
1.10.4, Fedora 43 has 1.10.6 — so the soname difference is Debian convention
rather than an ABI break. That's the assumption to re-check if it ever stops
working: if those versions drift apart, a symlink across a soname boundary
stops being safe.

The cleaner fix, if this gets annoying, is to stop linking libpcap at all.
It's only there for NE2000 ethernet emulation, which the host-control/MCP
use case never touches.

Fedora also needs `libXrandr` named explicitly — Ubuntu's SDL2 package
depends on it, Fedora's doesn't — which is the general shape of the problem:
the Debian package list doesn't translate one-for-one, because what arrives
free as a dependency differs per distro.

The package list itself is no longer taking anyone's word for it.
`scripts/check_runtime_deps.py` runs in the release build and fails it if
the emulator links anything `QUICKSTART.md` doesn't account for, against a
committed soname → package mapping. Writing it turned up two libraries the
list had been missing all along (`libgl1`, `libpng16-16t64`); both are
present on any desktop system, which is why nobody hit it.

## Two things left out on purpose, not by accident

- **No macOS bundle.** The build-script fix accidentally unblocked macOS
  compiling again, but there's no macOS job in `release.yml`. Not a
  priority for this project right now.
- **Linux bundle needs Ubuntu 24.04+ / Debian 13+.** It's built against
  glibc 2.38+, so anything older won't run it. Building on `ubuntu-22.04`
  instead of `ubuntu-latest` would widen that, at the cost of an older
  toolchain — a real option, just not one I've taken yet.
