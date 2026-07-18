# Changelog

## 0.1.0

Initial standalone release, extracted from a NegPy integration branch.

Public API:

- `coolscanpy.get_devices()` / `coolscanpy.open()`: python-sane-shaped device
  enumeration and open, scoped to Nikon Coolscan LS-5000 units.
- `Device`: typed option attributes (`resolution`, `depth`, `samples`,
  `autofocus`, `auto_exposure`) with constraint validation, option
  introspection through `Device.option_names` and `Device[name]`,
  `scan()`, `cancel()`, `eject()`.
- `Device.roll()`: the 40-slot roll-feeder extension. `Roll.preview()`,
  `Roll.spacing_offset()` / `set_spacing_offset()`, `Roll.approve()` /
  `needs_approval()`, `Roll.fingerprint`, `Roll.scan()` /
  `scan_many()`, `Roll.safe_stop()`, `Roll.eject()`.
- `Frame` and `Receipt`: in-memory RGB, aligned infrared plane, and
  per-frame provenance (exposure, clipping, focus detail, transport-smear
  assessment, fingerprint hashes, artifact hashes).
- A typed exception hierarchy rooted at `PyCoolscanError`, including
  `DeviceBusy`, `FingerprintRefused`, `ManualReviewRequired`,
  `SafeStopRequested`, `FeederParked`, `TransportSmearDetected`, and
  `BatchIntegrityError`.

Streaming: `Roll.scan_many()` yields each frame as it completes instead of
buffering the whole batch, keeping memory bounded to roughly two frames in
flight regardless of how many slots are requested.

Implemented end to end for `Material.COLOR_NEGATIVE` (the direct-USB
single-pass RGBI4 route). `Material.BLACK_AND_WHITE_NEGATIVE` previews and
approves correctly; its fine-scan path is not yet wired to the roll batch
engine and raises `NotImplementedError`.

Packaging: `py.typed` marker, publish-grade `pyproject.toml` metadata,
optional `[scanner]` extra for the SANE-backed plain-scan path, console
scripts `coolscanpy-practical-parity` and `coolscanpy-roll-scan`.

Testing: 807 hardware-free tests across the transport protocol, roll
engine, receipt assembly, and public facade, run against synthetic
fixtures and replay data. No test in this suite touches real hardware.

CI: GitHub Actions matrix across Ubuntu, macOS, and Windows on Python 3.13
and 3.14, running pytest and ruff.
