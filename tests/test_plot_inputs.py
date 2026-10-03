"""Reject different exclusions, but accept equivalent runs with different row order."""

import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

pytest.importorskip("matplotlib")
spec = importlib.util.spec_from_file_location("plot_results", Path(__file__).resolve().parents[1] / "scripts/plot_results.py")
plots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plots)


@pytest.mark.parametrize("different", [False, True])
def test_exclusions_compare_firm_years_not_row_order(different):
    first = [{"inn": "0123456789", "year": 2024}, {"inn": "9999999999", "year": 2025}]
    second = list(reversed(first))
    if different:
        second = [{"inn": "8888888888", "year": 2025}]
    manifest = {"sources": {"rfsd": {}}, "metadata": {"manual_exclusions": second}}
    audit = {"manual_exclusions": first, "years": list(range(2011, 2026))}
    with patch.object(sys, "argv", ["plot_results.py", "--audit-run", "audit", "--comparison-run", "comparison"]), \
         patch.object(plots, "verified_run", return_value=manifest), \
         patch.object(Path, "read_text", return_value=json.dumps(audit)), \
         patch.object(plots.pl, "read_parquet", side_effect=RuntimeError("input checks passed")):
        expected = ValueError if different else RuntimeError
        message = "Manual exclusions differ" if different else "input checks passed"
        with pytest.raises(expected, match=message):
            plots.main()
