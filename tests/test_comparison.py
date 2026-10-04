import json

import polars as pl
import pytest

from firm_level.comparison import add_growth, correlations
from firm_level import results


def test_growth_requires_adjacent_year_and_positive_base():
    levels = pl.DataFrame({
        "year": [2016, 2017, 2019, 2020, 2021], "rfsd_bln": [100., 120., -5., 0., 10.],
        "official_bln": [200., 220., 230., 240., 250.],
    }).with_columns(pl.lit("base").alias("sample"), pl.lit("total").alias("dimension"),
                    pl.lit("DFO").alias("group"), pl.lit("revenue_bln").alias("metric"))
    volumes = pl.DataFrame({"year": [2017, 2019, 2020, 2021], "volume_factor": [1.05, 1., 0., 1.1]}).with_columns(
        pl.lit("total").alias("dimension"), pl.lit("DFO").alias("group"))
    df = add_growth(levels, volumes)
    first = df.filter(pl.col("year") == 2017).row(0, named=True)
    assert first["rfsd_growth"] == pytest.approx(.2)
    assert first["official_growth"] == pytest.approx(.1)
    assert first["implicit_deflator_factor"] == pytest.approx(1.1 / 1.05)
    assert first["rfsd_deflated_growth"] == pytest.approx(1.2 / (1.1 / 1.05) - 1)
    assert df.filter(pl.col("year") == 2019)["rfsd_growth"].item() is None
    assert df.filter(pl.col("year") == 2020)["official_real_growth"].item() is None
    assert df.filter(pl.col("year") >= 2020)["rfsd_growth"].null_count() == 2
    assert df.filter(pl.col("year") == 2020)["rfsd_change_bln"].item() == 5
    with pytest.raises(ValueError, match="Duplicate"):
        add_growth(pl.concat([levels, levels]), volumes)


def test_correlations_use_common_finite_pairs_and_report_sensitivity():
    df = pl.DataFrame({"year": list(range(2017, 2025)),
                       "rfsd_growth": [1., 2., 3., 4., 5., 6., float("inf"), None],
                       "official_growth": [2., 4., 6., 8., 10., 12., 14., 16.],
                       "rfsd_deflated_growth": [0.] * 8, "official_real_growth": [1.] * 8,
                       }).with_columns(pl.lit("clean").alias("sample"), pl.lit("total").alias("dimension"),
                                       pl.lit("DFO").alias("group"), pl.lit("revenue_bln").alias("metric"))
    corr = correlations(df)
    all_pairs = corr.filter((pl.col("mode") == "nominal") & (pl.col("scenario") == "all")).row(0, named=True)
    assert all_pairs["n"] == 6 and all_pairs["pearson_r"] == pytest.approx(1.)
    assert all_pairs["years"] == "2017,2018,2019,2020,2021,2022"
    assert corr.filter(pl.col("mode") == "target_implicit_deflator")["pearson_r"].null_count() == 10
    assert corr.filter((pl.col("mode") == "nominal") & (pl.col("scenario") == "without_2020_2021"))["n"].item() == 4
    short = correlations(df.head(2))
    assert short["pearson_r"].null_count() == short.height


def test_result_runs_refuse_overwrite_and_index_actual_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "PROJECT_ROOT", tmp_path)
    path = results.create_run("test", tmp_path / "run")
    with pytest.raises(FileExistsError):
        results.create_run("test", path)
    (path / "table.csv").write_text("x\n1\n", encoding="utf-8")
    results.finish_run(path, "test", {"hashes_verified": False})
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["table.csv"]["sha256"] == results.file_hash(path / "table.csv")
    index = json.loads((tmp_path / "outputs/index.json").read_text(encoding="utf-8"))
    assert len(index) == 1 and index[0]["path"] == "run"
    (path / "report.md").write_text("Checked", encoding="utf-8")
    results.finish_run(path, "test", {})
    updated = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert updated["command"] == manifest["command"]
    assert updated["code_sha256"] == manifest["code_sha256"]
    assert updated["metadata"] == {"hashes_verified": False}
    (path / "table.csv").write_text("x\n2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        results.finish_run(path, "test", {})


def test_results_can_be_saved_without_git(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(results.shutil, "which", lambda _: None)
    path = results.create_run("zip", tmp_path / "run")
    (path / "table.csv").write_text("x\n1\n", encoding="utf-8")
    results.finish_run(path, "zip", {})
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["git_head"] is None
    assert manifest["git_worktree_dirty"] is None
    assert manifest["files"]["table.csv"]["sha256"] == results.file_hash(path / "table.csv")
