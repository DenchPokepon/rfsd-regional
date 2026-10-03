"""Selection edge cases: stable ranks and no duplicated work after sector changes."""

import importlib.util
from pathlib import Path

import polars as pl

spec = importlib.util.spec_from_file_location("prepare_audit", Path(__file__).resolve().parents[1] / "scripts/prepare_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_head_ranks_use_unrounded_values_and_inn_ties():
    df = pl.DataFrame({"inn": ["002", "001", "003", "004", "005"],
                       "cluster": ["a"]*5, "revenue": [10., 10., 10.0001, 0., None]})
    heads = audit.ranked_heads(df)
    assert heads["inn"].to_list() == ["003", "001", "002"]
    assert heads["rank"].to_list() == [1, 2, 3]


def test_changed_sector_firm_has_one_owner_and_head_owner_wins():
    raw, fish = "Недропользование и сырьё", "Рыбный комплекс"
    pool = [
        {"inn": "a", "cluster": raw, "reason": "scale_jump", "score": 100},
        {"inn": "a", "cluster": fish, "reason": "outlier", "score": 90},
        {"inn": "b", "cluster": raw, "reason": "scale_jump", "score": 80},
        {"inn": "b", "cluster": fish, "reason": "outlier", "score": 70},
    ]
    cases = audit.select_cases(pool, {"b": {"owner": 3}})
    assert [(r["inn"], r["owner"]) for r in cases] == [("a", 1), ("b", 3)]


def test_cases_diversify_reasons_and_cap_workload():
    pool = [{"inn": str(i), "cluster": "Торговля и потребсектор", "reason": reason, "score": 100-i}
            for i, reason in enumerate(["scale_jump", "scale_jump", "no_longer_observed", "outlier", "balance_gap"])]
    cases = audit.select_cases(pool, {})
    assert [r["reason"] for r in cases] == ["scale_jump", "no_longer_observed", "outlier"]
