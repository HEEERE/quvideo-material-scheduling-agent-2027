"""Evaluation reports must work with caller-relative output directories."""
import json
from pathlib import Path

import pytest

from src import evaluation, review_evaluation
from src.main import ROOT


@pytest.mark.parametrize("module,report_name,expected", [
    (evaluation, "evaluation.json", 1),
    (review_evaluation, "regressions.json", 2),
])
def test_relative_evidence_directory_from_other_cwd(tmp_path, monkeypatch, module, report_name, expected):
    monkeypatch.chdir(tmp_path)
    arguments = ["--mode", "mock", "--output-dir", "evidence"]
    if module is evaluation:
        arguments += ["--cases", "normal"]
    assert module.main(arguments) == 0
    reports = list((tmp_path / "evidence").glob(f"*/{report_name}"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["passed"] == report["total"] == expected
    for case in report["cases"]:
        links = [case["trace"]] if "trace" in case else case["traces"]
        for traces in links:
            for link in traces.values():
                path = Path(link)
                assert (path if path.is_absolute() else ROOT / path).is_file()
