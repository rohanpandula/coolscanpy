# coolscanpy

coolscanpy is a direct-USB acquisition library for Nikon Coolscan film
scanners. It exposes a python-sane-style API: open a device, read and set
typed options, call `scan()` and get back an array. On top of that plain
surface it adds a roll-feeder extension for whole-roll workflows. A roll can
be previewed across all 40 addressable slots, each frame's transport spacing
adjusted individually, then batch-scanned in one continuous reservation.

Each scanned frame comes back as:
- scanner-linear RGB (4000 dpi, 16-bit)
- an aligned infrared plane
- the 285 dpi RGBI meter pass the scanner captured for auto-exposure (see
  `Frame.meter_rgbi`)
- an in-memory receipt with exposure, clipping, focus, and transport-smear
  telemetry

## Status

This code was extracted from a working NegPy integration branch. On real
hardware, an LS-5000 running firmware 1.03 with an SA-21 roll feeder
converted to SA-30 wiring, that integration produced full-roll previews and
4000 dpi 16-bit RGBI captures with receipts, and those captures fed the
downstream dust-repair pipeline.

After extraction, a hardware-free test suite (807 tests) exercises the
transport protocol, the roll engine, the receipt assembly, and the public
facade against synthetic fixtures. It passes on Ubuntu, macOS, and Windows.

Live validation ran on 2026-07-18 with the packaged wheel installed into a
clean environment against a powered LS-5000, with no SANE installed: USB
enumeration, `open()`, option introspection, and a full roll preview of a
6-slot strip. The transport index read cleanly, slots 1 through 4 came
back aligned with no manual spacing offset, slot 5 was flagged for manual
review near the strip end, and slot 6 was correctly reported as a 2-row
trailing sliver rather than a frame. That run exposed one real bug, fixed
in 0.1.1: roll fingerprinting rejected strips with a trailing sliver.

As of 0.1.3, short strips (fewer than 6 frames) work for both preview and
fine scanning. A fingerprint-count regression that rejected short strips
was fixed in this release.

The 285 dpi meter pass is now surfaced on every frame. The scanner already
captures three of these per frame during auto-exposure; the third (settled
exposure) is decoded and attached to the `Frame`. Downstream tools that
need a dual-capture (prepass + main) can use it directly. See the
[downstream pipeline](#downstream-pipeline) section.

Coverage is uneven by material. `Material.COLOR_NEGATIVE` scans through a
direct-USB single-pass path and is implemented end to end, preview through
receipt. `Material.BLACK_AND_WHITE_NEGATIVE` previews and approves
correctly, but its fine-scan path routes through SANE and that route is not
yet wired into the roll batch engine; calling `scan()` or `scan_many()` on
a black-and-white roll raises `NotImplementedError` with a message
explaining the gap.

## Downstream pipeline

coolscanpy gets the raw data off the scanner. Three other projects turn it
into a finished image:

**[digital-fauxice](https://github.com/rohanpandula/digital-fauxice)** —
infrared dust and scratch repair. A byte-exact, from-scratch
reimplementation of Digital ICE (the Nikon/Applied Science Fiction process
that uses the IR channel to find defects and reconstruct the RGB underneath).
Validated against Nikon's own output: 68 million 16-bit values per frame,
zero mismatches. It takes the 4000 dpi RGBI main scan plus the 285 dpi
meter pass as its prepass, and produces the same repaired output Nikon
would have. An optional hybrid mode routes the worst damage (where the
exact repair leaves visible scars) to a LaMa inpainting model, disclosed
and bounded. The meter pass coolscanpy now surfaces on `Frame` is exactly
what fauxice's input contract expects — same physical frame, same focus,
same transport position, captured milliseconds before the fine scan.

**[cool-colors](https://github.com/rohanpandula/cool-colors)** — C-41
color inversion. Turns the scanner-linear negative into a positive,
reproducing Nikon Scan 4's CMS-off color pipeline bit-for-bit (the
per-frame inversion LUT, fixed tone curve, and gamma 2.2). With a
captured per-frame builder LUT, output matches Nikon Scan byte-for-byte.
Without it, a principled density inversion (film-base estimation, log
inversion, normalization) gets you a natural positive from any C-41 scan.

**[NegPy](https://github.com/marcinz606/NegPy)** — the desktop
application that ties capture, repair, and inversion together behind a
GUI. It consumes coolscanpy as an optional scanner backend, digital-fauxice
as an optional IR repair engine, and runs its own inversion pipeline for
the final print rendering.

The pipeline in order: coolscanpy captures → fauxice repairs dust →
cool-colors (or NegPy) inverts to a positive. Each step is optional and
independently installable.

## Install

```
pip install coolscanpy
```

The base install covers `get_devices()`, `open()`, and everything under
`Device.roll()`. None of that needs SANE. `get_devices()`/`open()` fall back
to direct USB enumeration when python-sane is not installed, and the
roll-feeder extension talks to the scanner over raw USB in a separate
process regardless.

SANE is needed only for the plain `Device.scan()` path and the vendor
`Device.eject()` action:

```
pip install "coolscanpy[scanner]"
```

On macOS, that build needs sane-backends' headers, and Homebrew does not put
them on the default include path:

```
brew install sane-backends
CPPFLAGS="-I$(brew --prefix)/include" LDFLAGS="-L$(brew --prefix)/lib" pip install "coolscanpy[scanner]"
```

On Linux, `sudo apt install libsane-dev` before the plain `pip install`
above is usually enough.

## Quickstart

Plain scan, the python-sane-shaped path:

```python
import coolscanpy

dev = coolscanpy.open("ls5000")
print(dev.option_names)
print(dev["resolution"].constraint)

dev.resolution = 4000
dev.depth = 16
rgb = dev.scan()  # uint16, shape (H, W, 3)
dev.close()
```

Roll workflow:

```python
import coolscanpy

with coolscanpy.open("ls5000") as dev:
    with dev.roll(material=coolscanpy.Material.COLOR_NEGATIVE) as roll:
        thumbnails = roll.preview()

        for thumb in thumbnails:
            if thumb.needs_approval:
                roll.approve(thumb.slot)

        selected = [thumb.slot for thumb in thumbnails[:36]]
        for frame in roll.scan_many(selected):
            print(frame.slot, frame.rgb.shape, frame.receipt.transport_smear.verdict)
            # The 285 dpi RGBI meter pass, if present:
            if frame.meter_rgbi is not None:
                print("  prepass for fauxice:", frame.meter_rgbi.shape)

        roll.eject()
```

`roll.scan_many()` opens one continuous transport reservation for the whole
list of slots and yields a `Frame` as each completes. `roll.scan(slot)` is
sugar for scanning one slot. Calling `roll.safe_stop()` from another thread
lets the frame in flight finish normally; the next one raises
`SafeStopRequested` instead of starting.

## Hardware support

Tested: Nikon Super Coolscan 5000 ED (LS-5000), firmware 1.03, with an SA-21
roll feeder wired for SA-30 compatibility.

Untested: every other Coolscan model, and any roll feeder other than the
SA-21/SA-30 configuration above. Platforms: the suite runs on Linux and
macOS in CI. Windows is untested; the transport layer has never run there
and the concurrency tests assume POSIX file-lock semantics. The code does not assume LS-5000-only
behavior where the protocol is generic, but nothing beyond the one
combination above has run against real film. Reports and pull requests
against other bodies are welcome, and an LS-50 test is particularly wanted:
its transport and optics differ from the LS-5000, and none of those
differences are covered here yet.

Strips shorter than a full roll work for preview and, as of 0.1.3, for fine
scanning too. A preview traversal parks a short strip at the transport
end-stop, so a batch run started right after a preview can raise
`RefeedRequired`. Pull the strip out, reinsert it until the feeder grips,
and run the batch again.

The converted SA-21 parks the strip a few minutes after feeding, and the
transport will not wake from parked. Start a capture within about ninety
seconds of feeding. A stall in the transport-index read means the feeder
parked; refeed rather than retry.

## Relationship to NegPy

coolscanpy is a dependency, the way NegPy already consumes python-sane or
gphoto2. It does not import NegPy, and NegPy does not depend on it by
default. A caller wires it in behind whatever extension seam their own
application already uses for other scanner backends: open a device, run one
roll to completion, close it.

## Safety model

A roll batch takes one reservation over the physical transport for its whole
scan_many() call, not one per frame, and releases it exactly once on close.
Requesting a safe stop never interrupts the frame currently being read; only
the next one is affected.

Before the first fine scan of a batch, the roll's fingerprint (bound at the
last preview) is checked against a fresh read of the transport. If the
comparison doesn't match, `FingerprintRefused` is raised instead of scanning
under the wrong geometry. Separately, a slot whose transport origin was not
confidently automatic must be approved against its current thumbnail before
it can be fine-scanned; scanning an unapproved flagged slot raises
`ManualReviewRequired`.

Every returned frame carries transport-smear and clipping telemetry.
Clipping is informational and never gates a capture. An abnormal repeated
tail from a stopped transport does gate: that frame is refused rather than
returned with smeared rows.

## What this package is not

There is no GUI. The RGB comes back scanner-linear and unmodified; color
inversion and print rendering belong to the application above this library
(see [cool-colors](https://github.com/rohanpandula/cool-colors) for a
standalone C-41 inverter, or
[NegPy](https://github.com/marcinz606/NegPy) for the full desktop app).
The infrared plane comes back raw as well. Turning it into a defect mask
and healing the dust it reveals is the job of
[digital-fauxice](https://github.com/rohanpandula/digital-fauxice), which
consumes this package's RGBI output directly — the `Frame.meter_rgbi`
field is the 285 dpi prepass its input contract requires.

## License

GPL-3.0-only. The code began life on a fork branch of
[NegPy](https://github.com/marcinz606/NegPy), marcinz606's film-negative
processing application, and is republished here as a standalone package
under the same license.
