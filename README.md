# CoolscanPy

CoolscanPy 0.7.10 is a standalone Python acquisition library for the Nikon
Super Coolscan 5000 ED (LS-5000). Its main path previews a strip or roll,
binds reviewed frame positions to that insertion, and captures color-negative
frames over direct USB. It returns scanner-linear pixels and acquisition
evidence for an application to save, repair, and render.

There is no GUI. The package does not require ScanStudio or NegPy, and its
public API does not depend on either application.

## Scope and support

| Area | Contract in 0.7.10 |
| --- | --- |
| Scanner | LS-5000. LS-40 and LS-50 direct-USB identities require explicit `allow_unverified=True`; all other unsupported identities remain refused. The opt-in paths are hardware-unvalidated. |
| Roll adapter | Strip-feeder identities `6Strip` and `36Strip` are accepted. A positively identified mount adapter is refused by the roll path. This allowlist is not a hardware-validation matrix. |
| Color negatives | `Material.COLOR_NEGATIVE`: direct-USB preview, review, batch fine capture, infrared, meter pass, and receipts. |
| Black-and-white negatives | Preview and approval are available. Roll fine capture is not wired; `Roll.scan()` and `scan_many()` raise `NotImplementedError`. |
| Fine-capture format | LS-5000 single-pass 4000 dpi, 16-bit RGBI acquisition. This is a fixed capture contract, not arbitrary resolution or scanner support. |
| Plain `Device.scan()` | A separate, SANE-backed array API requiring the optional `scanner` extra and host SANE support. |
| Platforms | Hardware-free CI runs on Ubuntu and macOS with Python 3.13 and 3.14. Windows has no CI or validated transport claim; concurrency tests assume POSIX locking. |

Tests use synthetic devices and protocol replays. They do not establish
compatibility with every firmware or feeder modification.

Version 0.7.10 routes presence checks through an existing preview-held worker
and gives each resumed held scan round a distinct artifact directory bound to
that held session. One LS-5000 ED / firmware 1.03 / SA-30 held-presence query
was exercised after preview; the repeated same-slot fix is software-tested but
was not rerun on hardware.

Version 0.7.9 added held-session `Roll.solve_exposure(slot=...)`, original USB
identity binding, and explicit known-blank meter-refusal skips through
`scan_many(allowed_meter_refusal_slots=...)`. Unlisted refusals and transport
failures still stop. These new workflows remain hardware-unvalidated.

The matching single-sample validation-order fix completed one LS-5000 ED /
firmware 1.03 / SA-30 4000 dpi 16-bit RGBI frame in ScanStudio on 2026-09-08,
with exactly 189,194,240 fine bytes. This is one-frame evidence, not full-roll
or other-model qualification. See [CHANGELOG.md](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/CHANGELOG.md).

Version 0.7.7 adds `samples_per_scan` (1 or 4, default 4) to `Roll.scan`,
`Roll.scan_many`, and the batch job. The 4-sample capture is byte-for-byte
unchanged; single-sample mode was initially lab-only pending a verified live trace
(see [CHANGELOG.md](https://github.com/rohanpandula/coolscanpy/blob/v0.7.7/CHANGELOG.md)).
Version 0.7.6 fixed held-session cleanup before batch iteration and retains
ownership when child shutdown is uncertain; see [cleanup contracts](#reservation-stop-and-cleanup-contracts).

## Install

Use Python 3.13 or later in a virtual environment:

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install 'coolscanpy==0.7.10'
```

Direct USB requires a host libusb 1.0 runtime that PyUSB can load. Typical
package-manager commands are:

```sh
# macOS, with Homebrew
brew install libusb

# Debian / Ubuntu
sudo apt install libusb-1.0-0
```

The process needs USB access permission and exclusive interface ownership.
Close competing scanner applications and resolve surviving workers before
opening a replacement session. Frozen applications must bundle libusb; see
the [loader contract](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/protocol/ls5000_single_pass/usb_backend.py).

Discovery, the color roll workflow, presence probing, and `Device.eject()`
use the base installation. SANE is not required for those paths.

### Optional SANE path

Install the extra only when using the plain `Device.scan()` API or the
SANE-based diagnostic workflows:

```sh
python -m pip install 'coolscanpy[scanner]==0.7.10'
```

Building `python-sane` requires host SANE headers. On macOS:

```sh
brew install sane-backends
CPPFLAGS="-I$(brew --prefix)/include" \
LDFLAGS="-L$(brew --prefix)/lib" \
python -m pip install 'coolscanpy[scanner]==0.7.10'
```

On Debian / Ubuntu, install `libsane-dev` before installing the extra.
The extra does not expand scanner support or enable black-and-white roll capture.

## Discover and open a device

```python
import coolscanpy

for info in coolscanpy.get_devices():
    print(info.id, info.model, info.supported, info.capabilities)

with coolscanpy.open("ls5000") as device:
    print(device.capabilities)
    print(device.option_names)
```

`get_devices()` returns `DeviceInfo` objects. It tries SANE enumeration and
falls back to direct USB when SANE is unavailable, fails, or finds no
Coolscan. There is no network transport. The `"ls5000"` alias requires one
supported attached scanner; use an exact discovery ID to disambiguate.
`open()` rechecks discovery and support.

Read identity from the discovery result and capabilities from
`device.capabilities`; there is no public `device.info` property. USB-only
capabilities are conservative: an unknown `adapter_frame_capacity` can be
`None`, and `can_eject=False` is not a gate on the direct-USB `eject()` method.

## Preview, review, and scan color negatives

The following interactive example writes preview TIFFs for review, asks for
an explicit slot selection, and saves the selected frames. It performs real
scanner I/O when run with a connected scanner. Use a new output directory
for each insertion; the retained attempt directory contains the acquisition
journals and intermediate evidence.

```python
from contextlib import closing
from pathlib import Path

import coolscanpy
import tifffile

output = Path("roll-capture").resolve()
output.mkdir()  # Refuse to overwrite an earlier capture directory.

with coolscanpy.open("ls5000") as device:
    with device.roll(
        material=coolscanpy.Material.COLOR_NEGATIVE,
        attempts_root=output / "attempts",
    ) as roll:
        thumbnails = roll.preview()
        for thumbnail in thumbnails:
            tifffile.imwrite(
                output / f"preview-{thumbnail.slot:02d}.tif", thumbnail.image
            )
            print(thumbnail.slot, thumbnail.needs_approval, thumbnail.warnings)

        selected = [
            int(slot) for slot in input(
                "Inspect the saved previews, then enter approved slots (e.g. 1,2): "
            ).split(",")
        ]
        for slot in selected:
            if roll.attended_binding_available:
                roll.approve(slot, attended=True)
            elif roll.needs_approval(slot):
                roll.approve(slot)

        try:
            with closing(roll.scan_many(selected, eject_after=True)) as frames:
                for frame in frames:
                    tifffile.imwrite(output / f"frame-{frame.slot:02d}-rgb.tif", frame.rgb)
                    if frame.ir is not None:
                        tifffile.imwrite(output / f"frame-{frame.slot:02d}-ir.tif", frame.ir)
                    print(frame.slot, frame.receipt.transport_smear.verdict)
        except coolscanpy.SafeStopRequested:
            print("Stopped after completing the frame already in flight.")
```

`preview()` performs a whole-roll transport read at approximately 97 dpi.
Its optional `slots` argument filters thumbnails, not the hardware read.
Up to 40 slots can be represented; inspect warnings and partial crops rather
than assuming all are complete, scanner-addressable frames.

A new preview replaces the fingerprint and clears approvals. To adjust a
boundary, use `set_spacing_offset(slot, offset_rows)` in native preview rows;
it returns a re-cropped thumbnail and invalidates that slot's approval.
`spacing_offset(slot)` reads the offset. Review the changed crop before
approving again.

When `attended_binding_available` is true, automatic detection has medium
confidence: use `approve(slot, attended=True)` for **every requested slot**,
not just flagged slots. Low-confidence detection is not rescued by this
approval. Approvals bind to the reviewed content and geometry, not to a
permanent slot number.

`scan_many()` reserves its lazy iterator before returning it. It freezes the
selected slots, approvals, and reviewed session against concurrent mutation.
Fully consume the iterator, or explicitly close it with `contextlib.closing`
as above. `scan(slot)` is the one-slot convenience API.

### Frames and evidence

A color-roll `Frame` contains scanner-linear `rgb`, an `ir` plane and
`ir_validity` mask, the settled 285 dpi `meter_rgbi` pass, and a `receipt`
with exposure, clipping, focus, transport-smear, and artifact evidence.
Optional evidence fields must be checked before use. RGB and infrared use
the package's Nikon Scan storage orientation; the meter has its own
acquisition geometry. `frame.prepare_digital_ice()` validates and returns
the scanner-native inputs for a repair call, or refuses incomplete evidence.

Pixels are not inverted or rendered for display. Clipping is informational;
stopped-transport smear can refuse a frame. The guarded color-negative
exposure solve records its commanded authority rather than promising color
equivalence with another scanner application.

Both scan methods accept `exposure_override_10ns=(red, green, blue)`:
validated per-channel fine-exposure ticks, each 10 ns, independent of the
meter measurements. Advanced contracts live alongside the implementation:

| Contract | Reference |
| --- | --- |
| Frame arrays, receipts, density ownership and repair inputs | [Public types](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/types.py) |
| Validated manual placement and restored preview state | [Preview sessions](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/roll/preview_session.py), [Roll API](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/_roll.py) |
| Short-strip geometry, terminal slots and fingerprint validation | [Transport index](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/protocol/ls5000_single_pass/roll_index.py) |
| Capture resource and implementation hashes | [Bundle integrity](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/protocol/ls5000_single_pass/bundle.py) |
| Strict EBDE padding validation and streaming/offline parity | [Packed decoder](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/protocol/ls5000_single_pass/packed.py) |
| Streaming artifact validation and raw-capture fallback | [Capture finalization](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/capture/single_pass_workflow.py) |

Concurrent decoding is advisory: invalid or unavailable derived data falls
back to the retained raw capture. `COOLSCANPY_CAPTURE_STREAMING=0` disables
that optimization. It does not disable acquisition integrity checks.

## Reservation, stop, and cleanup contracts

A successful preview leaves a child holding the scanner reservation. A
batch resumes it without another frame-table traversal. A resumed batch
that completes without stop or `eject_after=True` keeps the session held for
the next batch. Without a held session, a batch starts a fresh reservation
and releases it at completion. Repeating `preview()` supersedes the old hold
and registration.

Use these operations according to the state you own:

| Operation | Meaning |
| --- | --- |
| `roll.safe_stop()` | Stop the current batch between frames. It does not abort a frame already being captured. The consumer receives completed frames followed by `SafeStopRequested`. |
| Iterator `close()` | Stop and drain an active batch, or clean up an unstarted reservation, before relinquishing its ownership. |
| `roll.release()` | Give up a still-held session between batches. This is not an eject. |
| `roll.eject()` | Replay the held-session eject sequence within the existing reservation. Raises `EjectNotAvailable` when there is no held session. |
| `scan_many(..., eject_after=True)` | Eject after the final requested frame. A safe stop takes priority and releases without ejecting. |
| `roll.close()` | Close owned work and release the Roll reservation. Unconfirmed worker shutdown raises `DeviceBusy` and retains ownership. |
| `device.eject()` | Direct-USB unload when the caller has no capture child owning the interface. It confirms absence before returning success; an already-empty transport is a no-op success. |

Stop state is batch-scoped: creating a new batch resets the Roll stop event.
An application that acknowledges cancellation for a larger job must keep
its own job stop state across preparation, reservation, and retries. It
must serialize that state with reservation and avoid starting another batch
after acknowledging the stop. A pre-reservation call to `safe_stop()` alone
does not cancel a future batch.

In 0.7.6, stopping before the first `next()`, closing an unstarted iterator,
closing its Roll, or abandoning that iterator tears down its pending held
child. A reservation failure leaves the original hold available for cleanup.
If child exit cannot be confirmed, another batch or device close must not
silently acquire ownership. The retained `DeviceBusy` state is a recovery
condition, not an instruction to retry in a loop. Closing from an active
progress callback is also refused; request a stop and let the owning caller
complete cleanup instead.

`attempts_root` should be an absolute caller-owned directory when evidence
must survive. Without it, CoolscanPy uses temporary storage that normal
close removes; uncertain cleanup and other evidence-preserving failures
retain it. Keep attempt journals and raw data until a failure is understood.

### Recovery

| Result | What the caller should do |
| --- | --- |
| `ManualReviewRequired` | Review the current crop and geometry, then approve the requested slot. |
| `FingerprintRefused` / `RollMismatch` | Do not capture using stale geometry; establish the media state and registration. |
| `RefeedRequired` | The held reservation is unavailable. Treat a physical refeed as a new registration and preview again. |
| `TransportIndexRefused` / `MeterControllerRefused` | Retain the typed failure evidence and investigate before retrying. |
| `DeviceBusy` during shutdown | Keep ownership and evidence intact until the worker's state is resolved. |
| `FeederParked` / `EjectFailed` | Establish whether film is still gripped; follow the reported physical remedy rather than automatically repeating unload. |

`device.film_present()` returns `True`, `False`, or `None`. Unknown is not
absent, and present is not proof that motion is safe. `Device.eject()` already
confirms absence; a second presence probe should not turn its successful
return into an error. A transport-index stall is a stop condition: preserve
the attempt and do not repeatedly retry the same insertion.

For exact exception payloads and held-child teardown behavior, see
[exceptions](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/exceptions.py) and the
[capture process adapter](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/src/coolscanpy/protocol/ls5000_single_pass/capture_process.py).

## Plain scan and diagnostic commands

With the optional SANE installation, the python-sane-shaped path returns
an array directly:

```python
import coolscanpy

with coolscanpy.open("ls5000") as device:
    print(device["resolution"].constraint)
    device.resolution = 4000
    device.depth = 16
    rgb = device.scan()
    print(rgb.shape, rgb.dtype)
```

Plain scan options describe that path. They do not change the color-roll
engine's fixed acquisition contract. `Device.cancel()` is the plain-scan
cancellation API; use `Roll.safe_stop()` for a roll batch.

Installed command entry points and diagnostic modules expose their own help:

```sh
coolscanpy-roll-scan --help
coolscanpy-practical-parity --help
python -m coolscanpy.cli.vpd_dump --help
python -m coolscanpy.cli.perforation_probe --help
python -m coolscanpy.cli.unload_timer --help
python -m coolscanpy.cli.wedge_recovery --help
```

`coolscanpy-roll-scan` is a separate plan/registration workflow, with
`prepare`, `status`, `approve`, `fine`, `full-negative`, and `forward-roll`
commands; it is not a CLI wrapper for the `Roll` example above. Its live
commands require `--live` and `NEGPY_ROLL_LIVE=YES`.
`coolscanpy-practical-parity` uses SANE and requires `--live` for capture;
its default probe can still contact hardware. The diagnostic modules also
have different motion contracts: VPD, perforation, and unload-timer probes
are read-only; wedge recovery can command motion. Read their help before
invoking them on a scanner.

## Develop and test

The canonical release branch is `port/cross-platform`, even though the
GitHub default branch is `main`. For work on this release line:

```sh
git clone --branch port/cross-platform https://github.com/rohanpandula/coolscanpy.git
cd coolscanpy
uv sync --frozen --dev --python 3.13
uv run --frozen pytest -q
uv run --frozen ruff check .
```

Use **uv 0.11.30**, as required by `pyproject.toml` and both CI workflows.
If your installed uv is a different version, prefix the uv command with
`uv tool run --from uv==0.11.30`; for example:

```sh
uv tool run --from uv==0.11.30 uv sync --frozen --dev --python 3.13
```

For a regular editable install without the locked development environment:

```sh
python -m pip install -e .
```

The test suite uses synthetic devices, replay fixtures, and mocked transport
boundaries. Some tests need optional packages or external archived captures
and skip when those inputs are absent. CI runs the full suite and Ruff on
Ubuntu/macOS with Python 3.13/3.14; it does not install the optional SANE
extra or validate attached hardware. The count of passing tests is not a
scanner compatibility claim.

Build with the same locked backend used for publication:

```sh
uv sync --frozen --dev
uv build --no-sources --no-build-isolation --out-dir dist
```

Change capture-bundle hashes only after the affected regression checks pass.
`_roll.py` is outside that manifest and does not require a bundle reseal.

### Release through the existing CI

1. Update the project version in `pyproject.toml`, the `coolscanpy` entry in
   `uv.lock`, and the changelog together. Keep runtime and distribution
   metadata consistent.
2. Review the change and merge a green PR into `port/cross-platform` after
   the full tests and Ruff pass on the CI matrix.
3. Tag the reviewed, merged release commit with the matching version and
   push that tag. For this release, the tag is `v0.7.10`.
4. Monitor [the publishing workflow](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/.github/workflows/publish.yml), then
   verify both PyPI artifacts, metadata, source contents, and attestations.

Pushing a `v*` tag triggers publication. The workflow first requires the tag
commit to be an ancestor of `port/cross-platform` and passes the test suite.
It then checks the tag against the project version, builds with pinned uv
and setuptools, and publishes through PyPI Trusted Publishing (GitHub OIDC).
No local upload token is needed. A GitHub release page is not the trigger.
Do not reuse or move an already-published version tag.

## Downstream processing and license

Applications can consume CoolscanPy as an acquisition dependency:
[digital-fauxice](https://github.com/rohanpandula/digital-fauxice) handles
infrared-guided repair, [cool-colors](https://github.com/rohanpandula/cool-colors)
handles color-negative rendering, and
[NegPy](https://github.com/marcinz606/NegPy) provides a desktop processing
application. These are separate projects; installing CoolscanPy does not
install them or assert equivalence between their rendered output and a
particular scanner application.

CoolscanPy originated in a NegPy fork and is distributed independently under
[GPL-3.0-only](https://github.com/rohanpandula/coolscanpy/blob/v0.7.10/LICENSE).
