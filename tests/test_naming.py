"""Rename guard (S-E0-02): the old brand must not come back.

The natural phenomenon ("aurora borealis", "aurora corona", ...) is real content and stays,
so the pattern matches only brand spellings, never a bare "aurora".
The guard is git-only by design: it checks tracked files and skips outside a checkout.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BRAND = r"aurorae|aurora_images|aurora-uploads|AURORA_[A-Z]"


def _brand_hits() -> list[str]:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    result = subprocess.run(  # noqa: S603 - fixed arguments, no user input
        [git, "grep", "-n", "-i", "-E", BRAND, "--", ".", ":!uv.lock", ":!tests/test_naming.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr  # 1 = no match
    return result.stdout.splitlines()


def test_old_brand_name_is_gone() -> None:
    assert _brand_hits() == []


@pytest.mark.parametrize("prefix", ["SYNTHWERK_", "AURORAE_", "AURORA_"])
def test_only_the_service_prefix_is_read(prefix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from synthwerk_vision.platform.config import Settings

    monkeypatch.setenv(f"{prefix}TOP_K", "3")
    assert Settings(_env_file=None).top_k == 5  # type: ignore[call-arg]
    monkeypatch.setenv("SYNTHWERK_VISION_TOP_K", "4")
    assert Settings(_env_file=None).top_k == 4  # type: ignore[call-arg]
