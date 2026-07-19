# coolscanpy

coolscanpy is a direct-USB acquisition library for Nikon Coolscan film scanners.
It exposes a python-sane-style API: open a device, read and set typed options,
call `scan()` and get back an array. On top of that plain surface it adds a
roll-feeder extension for whole-roll workflows. A roll can be previewed
across all 40 addressable slots, each frame's transport spacing adjusted
individually, then batch-scanned in one continuous reservation. Each
scanned frame comes back as scanner-linear RGB, an aligned infrared plane,
and an in-memory receipt.

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
Fine scans and IR capture have not been re-run live since the extraction;
they are the remaining validation step.

Coverage is uneven by material. `Material.COLOR_NEGATIVE` scans through a
direct-USB single-pass path and is implemented end to end, preview through
receipt. `Material.BLACK_AND_WHITE_NEGATIVE` previews and approves correctly,
but its fine-scan path routes through SANE and that route is not yet wired
into the roll batch engine; calling `scan()` or `scan_many()` on a
black-and-white roll raises `NotImplementedError` with a message explaining
the gap.

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
SA-21/SA-30 configuration above. The code does not assume LS-5000-only
behavior where the protocol is generic, but nothing beyond the one
combination above has run against real film. Reports and pull requests
against other bodies are welcome, and an LS-50 test is particularly wanted:
its transport and optics differ from the LS-5000, and none of those
differences are covered here yet.

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
inversion and print rendering belong to the application above this library.
The infrared plane comes back raw as well. Turning it into a defect mask
and healing the dust it reveals is the job of
[digital-fauxice](https://github.com/rohanpandula/digital-fauxice), a
from-scratch reimplementation of Digital ICE built to consume exactly this
kind of RGBI capture.

## License

GPL-3.0-only. The code began life on a fork branch of
[NegPy](https://github.com/marcinz606/NegPy), marcinz606's film-negative
processing application, and is republished here as a standalone package
under the same license.
