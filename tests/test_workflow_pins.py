from __future__ import annotations

import importlib.util
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = REPOSITORY_ROOT / "scripts" / "check_github_action_pins.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_github_action_pins", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_policy(tmp_path: Path, workflow: str) -> tuple[int, str]:
    workflow_root = tmp_path / ".github" / "workflows"
    workflow_root.mkdir(parents=True)
    (workflow_root / "test.yml").write_text(workflow, encoding="utf-8")
    checker = _load_checker()
    checker.REPOSITORY_ROOT = tmp_path
    checker.WORKFLOW_ROOT = workflow_root
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        result = checker.main()
    return result, output.getvalue()


def test_repository_workflows_pass_pin_policy() -> None:
    checker = _load_checker()
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        result = checker.main()

    assert result == 0, output.getvalue()
    assert "7 external uses entries" in output.getvalue()


def test_full_sha_and_checkout_credential_fence_pass(tmp_path: Path) -> None:
    result, output = _run_policy(
        tmp_path,
        """jobs:
  test:
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
        with:
          persist-credentials: false
      - uses: owner/action@0123456789abcdef0123456789abcdef01234567
""",
    )

    assert result == 0, output


def test_every_mutable_or_abbreviated_ref_is_rejected(tmp_path: Path) -> None:
    for unsafe_ref in (
        "v4",
        "main",
        "release",
        "v4.2.2",
        "0123456789abcdef0123456789abcdef0123456",
    ):
        result, output = _run_policy(
            tmp_path / unsafe_ref.replace("/", "_"),
            f"""jobs:
  test:
    steps:
      - uses: owner/action@{unsafe_ref}
""",
        )

        assert result == 1
        assert "not pinned to a full lowercase commit SHA" in output


def test_checkout_must_disable_persisted_credentials(tmp_path: Path) -> None:
    result, output = _run_policy(
        tmp_path,
        """jobs:
  test:
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
""",
    )

    assert result == 1
    assert "persist-credentials: false" in output
