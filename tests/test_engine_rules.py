"""Runs the original numpy-only engine checks (test_engine.py in the project folder) as part of the suite."""
import pathlib, runpy


def test_original_engine_sanity_checks():
    runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent / "test_engine.py"))
