"""The eval scripts are what a reader is told to run, so they have to at
least survive contact with the current code.

This exists because `eval/benchmark_methods.py` — the command the README
gives for reproducing its headline comparison table — crashed with a
`ValueError: too many values to unpack` after `_ocr_image` grew a third
return value. Nothing caught it: no test imported the script, and the
README kept advertising it. A reviewer following the instructions would
have hit a traceback.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((ROOT / "eval").glob("*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_eval_script_parses(script):
    ast.parse(script.read_text(encoding="utf-8"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_eval_script_imports_cleanly(script):
    """Import the module without running main(). Catches a script that
    references something the library no longer exports."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import importlib.util,sys; sys.path.insert(0, {str(ROOT / 'src')!r}); "
            f"sys.path.insert(0, {str(ROOT / 'eval')!r}); "
            f"spec=importlib.util.spec_from_file_location('m', {str(script)!r}); "
            f"m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-1500:]


def test_benchmark_survives_the_ocr_call_it_makes():
    """The bug this file exists for was runtime, not import-time: the
    benchmark unpacked two values from the OCR helper after it had grown a
    third. Importing the module could never catch that, so the OCR path is
    exercised directly with the vision call stubbed out.
    """
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "eval"))
    import benchmark_methods

    benchmark_methods.vision_transcribe = lambda _png: "transcribed"
    rows = benchmark_methods._bench_ocr(
        [ROOT / "eval" / "golden_dataset" / "receipt_scan.png"]
    )

    assert rows and rows[0]["tesseract_chars"] > 0
    assert "vlm_latency_s" in rows[0]


def test_readme_only_advertises_scripts_that_exist():
    """A command in the README that doesn't resolve is the same failure in a
    different place."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in set(re.findall(r"python eval/([\w./-]+\.py)", readme)):
        assert (ROOT / "eval" / name).exists(), f"README runs a missing script: {name}"
