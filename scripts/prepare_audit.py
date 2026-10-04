"""Prepare a small, reproducible review packet; never change RFSD observations."""

import argparse
import json
from pathlib import Path
import shutil

import polars as pl

from firm_level import scan_rfsd, dfo_filter, region_code
from firm_level.analysis import BASE, CLEAN, CLUSTER, DIVISION, REVENUE, SUMMARY
from firm_level.dataset import DATA_DIR, PROJECT_ROOT
from firm_level.results import create_run, file_hash, finish_run
from firm_level.sources import SOURCES, verify_file

# Group, reviewer, number of head firms in the first pass. Ten remain available.
BLOCKS = [
    ("Недропользование и сырьё", 1, 7), ("Лесное хозяйство", 1, 5),
    ("Рыбный комплекс", 2, 5), ("Сельское хозяйство", 2, 5),
    ("Транспорт и логистика", 3, 5), ("Пищепром", 3, 5),
    ("Строительство, энергетика и коммунальные виды", 4, 10),
    ("Торговля и потребсектор", 5, 10), ("Прочее", 0, 3),
]
OWNERS = {name: owner for name, owner, _ in BLOCKS}
REASONS = {
    "scale_jump": "Изменение выручки примерно в 1000 раз",
    "no_longer_observed": "Выручка перестала наблюдаться в срезе ДФО",
    "outlier": "Метка outlier=1",
    "balance_gap": "Активы и пассивы расходятся",
    "missing_revenue_large_assets": "Нет выручки при крупных активах",
    "profit_gap": "Не сходится валовая прибыль полной формы",
}
FIELDS = ["inn", "year", "region", "region_taxcode", "okved", "okved_section", "okopf",
          "financial", "outlier", "filed", "imputed", "eligible", "simplified",
          "line_2110", "line_2120", "line_2100", "line_1600", "line_1700", "line_2400"]


def verify_run(path):
    meta = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for name, info in meta["files"].items():
        verify_file(path / name, info["sha256"])
    return meta


def ranked_heads(clean):
    """Rank unrounded positive revenue, breaking ties by INN."""
    return (clean.filter(pl.col("revenue") > 0)
            .sort(["cluster", "revenue", "inn"], descending=[False, True, False])
            .with_columns(pl.col("inn").cum_count().over("cluster").alias("rank"))
            .filter(pl.col("rank") <= 10))


def candidate_pool(audit):
    rows = []
    for row in pl.read_parquet(audit / "anomalies.parquet").iter_rows(named=True):
        if row["year"] >= 2017 and row["base"] and row["reason"] in REASONS:
            rows.append({"inn": row["inn"], "cluster": row["cluster"], "year": row["year"],
                         "years": str(row["year"]), "reason": row["reason"], "score": row["score_thousand"]})
    for row in pl.read_parquet(audit / "changes.parquet").iter_rows(named=True):
        if row["comparison_year"] >= 2017 and (row["base"] or row["base_previous"]) and row["reason"] in REASONS:
            year = row["comparison_year"]
            rows.append({"inn": row["inn"], "cluster": row["cluster"] or row["cluster_previous"],
                         "year": year, "years": f"{year-1}, {year}",
                         "reason": row["reason"], "score": row["score_thousand"]})
    return sorted(rows, key=lambda r: (-r["score"], r["inn"], -r["year"]))


def select_cases(pool, records):
    """Up to three distinct firms per reviewer; diversify reasons before repeating."""
    picked = []
    assigned_owner = {inn: row["owner"] for inn, row in records.items()}
    for owner in range(6):
        seen = set()
        for reason in REASONS:
            for row in pool:
                assigned = assigned_owner.get(row["inn"], OWNERS.get(row["cluster"], 0))
                if assigned == owner and row["reason"] == reason and row["inn"] not in seen:
                    picked.append(row | {"owner": owner})
                    assigned_owner[row["inn"]] = owner
                    seen.add(row["inn"])
                    break
            if len(seen) == 3:
                break
    return picked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-run", type=Path, required=True)
    parser.add_argument("--figures-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    meta = verify_run(args.audit_run)
    figure_meta = verify_run(args.figures_run)
    if meta["sources"]["rfsd"] != SOURCES["rfsd"]:
        raise ValueError("Audit and local RFSD revisions differ")
    linked_hashes = figure_meta["metadata"]["input_manifest_sha256"].values()
    if file_hash(args.audit_run / "manifest.json") not in linked_hashes:
        raise ValueError("Figures were not built from this audit run")
    run = json.loads((args.audit_run / "run.json").read_text(encoding="utf-8"))
    if run["years"] != list(range(2011, 2026)):
        raise ValueError("Audit packet requires diagnostics for all 2011–2025 years")
    exclusions = pl.DataFrame(run["manual_exclusions"], schema={"inn": pl.String, "year": pl.Int64})
    for key, info in SOURCES["rfsd"]["files"].items():
        verify_file(DATA_DIR / key, info["sha256"])
    current = scan_rfsd(2024).filter(dfo_filter()).select(FIELDS).with_columns(
        CLUSTER.alias("cluster"), REVENUE.alias("revenue"), region_code().alias("region_code"),
        DIVISION.alias("division"), BASE.alias("base"), CLEAN.alias("flag_clean"),
    ).collect()
    clean = current.filter(pl.col("base") & pl.col("flag_clean")).join(exclusions, on=["inn", "year"], how="anti")
    if clean["inn"].n_unique() != clean.height:
        raise ValueError("Duplicate INNs in 2024")
    reference = pl.read_parquet(args.audit_run / "groups.parquet").filter(
        (pl.col("year") == 2024) & (pl.col("sample") == "clean") & (pl.col("dimension") == "cluster"))
    calculated = clean.group_by("cluster").agg(SUMMARY)
    check = calculated.join(reference, left_on="cluster", right_on="group", suffix="_saved", validate="1:1")
    if check.height != reference.height or check.filter((pl.col("revenue_bln")-pl.col("revenue_bln_saved")).abs()>1e-7).height:
        raise ValueError("Current aggregates differ from audit")
    heads = ranked_heads(clean)
    records, coverage, tables = {}, [], {}
    for index, (group, owner, count) in enumerate(BLOCKS, 1):
        h = heads.filter(pl.col("cluster") == group)
        denominator = clean.filter((pl.col("cluster") == group) & (pl.col("revenue") > 0))["revenue"].sum()
        h = h.with_columns((pl.col("rank") <= count).alias("first_pass"),
                           (pl.col("revenue") / denominator).alias("revenue_share"), pl.lit(owner).alias("owner"))
        tables[f"group_{index:02d}_heads"] = h
        chosen = h.filter("first_pass")
        coverage.append({"owner": owner, "cluster": group, "firms_to_check": chosen.height,
                         "positive_revenue_bln": denominator/1e6,
                         "selected_revenue_bln": chosen["revenue"].sum()/1e6,
                         "planned_coverage": chosen["revenue"].sum()/denominator})
        for row in chosen.iter_rows(named=True):
            records[row["inn"]] = {"owner": owner, "inn": row["inn"], "cluster": group,
                                    "years": "2024", "reason": f"Выручка 2024: место {row['rank']} в группе"}
    candidates = select_cases(candidate_pool(args.audit_run), records)
    for row in candidates:
        if row["inn"] in records:
            old = records[row["inn"]]
            old["years"] = ", ".join(map(str, sorted({int(y) for y in (old['years']+', '+row['years']).split(', ')})))
            old["reason"] += "; " + REASONS[row["reason"]]
        else:
            records[row["inn"]] = {k: row[k] for k in ["owner", "inn", "cluster", "years"]} | {"reason": REASONS[row["reason"]]}
    records["2526003992"] = {"owner": 0, "inn": "2526003992", "cluster": "Вне коммерческой выборки",
                              "years": "2024, 2025", "reason": "Пожарская ЦРБ: проверить масштаб; уже исключена по ОКОПФ"}
    selected = pl.DataFrame(list(records.values())).sort("owner", "cluster", "inn")
    history = scan_rfsd(list(range(2011, 2026))).filter(pl.col("inn").is_in(list(records))).select(FIELDS).with_columns(
        pl.lit(True).alias("present"), dfo_filter().alias("in_dfo"), region_code().alias("region_code"),
        CLUSTER.alias("cluster"), (BASE & CLEAN & dfo_filter()).alias("in_clean"),
    ).collect().join(exclusions.with_columns(pl.lit(True).alias("excluded")), on=["inn", "year"], how="left").with_columns(
        (pl.col("in_clean") & ~pl.col("excluded").fill_null(False)).alias("in_clean"))
    grid = selected.select("owner", "inn").join(pl.DataFrame({"year": list(range(2011, 2026))}), how="cross")
    history = grid.join(history, on=["inn", "year"], how="left", validate="1:1").with_columns(pl.col("present").fill_null(False)).sort("owner", "inn", "year")
    tables.update({"assignments": selected, "coverage": pl.DataFrame(coverage),
                   "cases": pl.DataFrame(candidates), "history": history,
                   "sector_history": pl.read_parquet(args.audit_run / "groups.parquet").filter(pl.col("dimension") == "cluster")})
    output = create_run("audit-packet", args.output_dir)
    for name, table in tables.items():
        table.write_parquet(output / f"{name}.parquet")
        table.write_csv(output / f"{name}.csv", include_bom=True)
    packets = []
    for owner in range(6):
        roster = selected.filter(pl.col("owner") == owner).to_dicts()
        for row in roster:
            row.update({"name": "", "checked_years": "", "status": "", "source": "", "comment": "", "action": ""})
        pl.DataFrame(roster).write_csv(output / f"review_{owner}.csv", include_bom=True)
        packets.append({"owner": owner, "review": roster,
                        "coverage": [r for r in coverage if r["owner"] == owner],
                        "history": history.filter(pl.col("owner") == owner).to_dicts()})
    (output / "packets.json").write_text(json.dumps(packets, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    for stem in ["01_coverage", "04_concentration_cleaning"]:
        shutil.copy2(args.figures_run / f"{stem}.png", output / f"{stem}.png")
    task = (PROJECT_ROOT / "docs/audit_task.md").read_text(encoding="utf-8")
    for stem in ["01_coverage", "04_concentration_cleaning"]:
        task = task.replace(f"figures/{stem}.png", f"{stem}.png")
    (output / "audit_task.md").write_text(task, encoding="utf-8")
    metadata = {"year": 2024, "hashes_verified": True, "manual_exclusions": run["manual_exclusions"],
                "audit_run": str(args.audit_run), "audit_manifest_sha256": file_hash(args.audit_run / "manifest.json"),
                "figure_manifest_sha256": file_hash(args.figures_run / "manifest.json"),
                "selection": "Top 10 available; first pass 5/7/10 by block; up to 3 anomaly firms per reviewer from 2017–2025 diagnostics",
                "note": "Planned coverage, not completed audit; selected histories include moves outside DFO"}
    finish_run(output, "batch6-audit-packet", metadata)
    print(json.dumps({"output": str(output), "firms": selected.height,
                      "workload": selected.group_by('owner').len().sort('owner').to_dicts()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
