"""Tests for the installed package scaffold."""

import os
import subprocess
import sys
from importlib.metadata import version as installed_version
from pathlib import Path

import openmtgdata


def test_package_import_and_version() -> None:
    assert openmtgdata.__version__ == installed_version("openmtgdata")
    assert ".dev" in openmtgdata.__version__


def test_import_and_module_cli_need_no_dataset_files(tmp_path: Path) -> None:
    import_result = subprocess.run(
        [sys.executable, "-c", "import openmtgdata; print(openmtgdata.__version__)"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert import_result.returncode == 0
    assert import_result.stdout.strip() == openmtgdata.__version__

    help_result = subprocess.run(
        [sys.executable, "-m", "openmtgdata", "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "usage: openmtgdata" in help_result.stdout
    assert help_result.stderr == ""

    version_result = subprocess.run(
        [sys.executable, "-m", "openmtgdata", "--version"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert version_result.returncode == 0
    assert version_result.stdout.strip() == f"openmtgdata {openmtgdata.__version__}"


def test_console_script_help_and_version(tmp_path: Path) -> None:
    console_script = Path(sys.executable).with_name(
        "openmtgdata.exe" if os.name == "nt" else "openmtgdata"
    )
    help_result = subprocess.run(
        [str(console_script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "usage: openmtgdata" in help_result.stdout

    version_result = subprocess.run(
        [str(console_script), "--version"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert version_result.returncode == 0
    assert version_result.stdout.strip() == f"openmtgdata {openmtgdata.__version__}"
