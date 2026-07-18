"""Root pytest configuration for coolscanpy's acquisition-layer test suite.

Deliberately minimal, and deliberately not a copy of NegPy's tests/conftest.py:
that file installs a session-scoped, autouse PyQt6 QApplication fixture (plus
QT_QPA_PLATFORM/XDG_RUNTIME_DIR env setup) for a desktop app's whole suite.
coolscanpy has no Qt dependency at all, and no test here ever requests a Qt
fixture, so bringing that file over verbatim would import PyQt6 for no
reason. NegPy's --metrics-out perf-metrics harness (tests/metrics/conftest.py)
is likewise out of scope for this package's test suite.

Every scanner test in this suite fakes the boundary just below the module
under test with small classes local to that test file (FakeOption,
FakeSaneDev, FakeRawDevice, FakeRunner, and friends) rather than shared
pytest fixtures — there never was a shared fixture library to port. The one
cross-file dependency (tests/transport/test_full_negative_capture.py reuses
a handful of SANE doubles defined in tests/transport/test_coolscan_ir.py) is
preserved as a direct module import, matching the source layout.
"""
