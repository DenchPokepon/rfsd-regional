"""Export firms by Federal District (DFO, SKFO, YUFO, SFO, SZFO) and selected OKVED divisions to Excel.

OKVED groups:
  - Производство продуктов: divisions 10–15 (Section C)
  - Торговля: divisions 45–47 (Section G)
  - Транспорт: divisions 49–53 (Section H)

Supported Federal Districts:
  - ДФО (DFO): Дальневосточный федеральный округ
  - СКФО (SKFO): Северо-Кавказский федеральный округ (472k rows -> fits in 1 master Excel file)
  - ЮФО (YUFO): Южный федеральный округ (1.55M rows -> multi-year period files < 1M rows each)
  - СФО (SFO): Сибирский федеральный округ (2.48M rows -> multi-year period files < 1M rows each)
  - СЗФО (SZFO): Северо-Западный федеральный округ (3.12M rows -> multi-year period files < 1M rows each)

Outputs per Federal District:
  1. {fo}_okved_dynamics_all_years.xlsx:
     - Sheet 'Сводная_Анализ': Native Excel Pivot Table (Region x Year x OKVED division)
     - Sheet 'Сводная': Aggregated dynamics (2011–2025) with outlier-cleaned totals
  2. {fo}_okved_all_years.parquet:
     - 100% full microdata panel (all years, all firms, 50 columns)
  3. Microdata in Excel (sheets: 'Сводная_Анализ', 'Сводная', 'Данные'):
     - For SKFO: skfo_okved_all_years.xlsx (all 15 years fit on one sheet <= 1,048,570 rows)
     - For YUFO, SFO, SZFO: contiguous multi-year period files {fo}_okved_{start_year}_{end_year}.xlsx (< 1,000,000 rows each)

Usage:
    uv run --group analysis python scripts/fo_okved_excel.py --district ALL
    uv run --group analysis python scripts/fo_okved_excel.py --district SKFO
    uv run --group analysis python scripts/fo_okved_excel.py --district SFO --year 2024
"""

import argparse
from collections.abc import Sequence
import itertools
from pathlib import Path
import subprocess
import sys
import time

import polars as pl
import xlsxwriter

from rfsd_regional import (
    FEDERAL_DISTRICTS,
    district_filter,
    load_renaming_dict,
    region_code,
    scan_rfsd,
)
from rfsd_regional.analysis import BASE, DIVISION, SECTION
from rfsd_regional.results import create_run, finish_run

# Requested OKVED divisions
REQUESTED_DIVISIONS = (
    list(range(10, 16))   # 10–15: Производство пищевых продуктов и напитков
    + list(range(45, 48))  # 45–47: Торговля оптовая и розничная
    + list(range(49, 54))  # 49–53: Транспортировка и хранение
)

# Essential statutory financial statement lines (Form 1 Balance Sheet, Form 2 P&L, Cash Flow)
ESSENTIAL_LINES = [
    # Form 1: Balance Sheet Assets
    "line_1100",  # Внеоборотные активы
    "line_1150",  # Основные средства
    "line_1200",  # Оборотные активы
    "line_1210",  # Запасы
    "line_1230",  # Дебиторская задолженность
    "line_1240",  # Финансовые вложения (краткосрочные)
    "line_1250",  # Денежные средства и денежные эквиваленты
    "line_1600",  # Баланс (актив всего)
    # Form 1: Balance Sheet Equity & Liabilities
    "line_1300",  # Капитал и резервы
    "line_1310",  # Уставный капитал
    "line_1370",  # Нераспределенная прибыль (непокрытый убыток)
    "line_1400",  # Долгосрочные обязательства
    "line_1410",  # Заемные средства (долгосрочные)
    "line_1500",  # Краткосрочные обязательства
    "line_1510",  # Заемные средства (краткосрочные)
    "line_1520",  # Кредиторская задолженность
    "line_1700",  # Баланс (пассив всего)
    # Form 2: Profit & Loss Statement
    "line_2110",  # Выручка
    "line_2120",  # Себестоимость продаж
    "line_2100",  # Валовая прибыль (убыток)
    "line_2210",  # Коммерческие расходы
    "line_2220",  # Управленческие расходы
    "line_2200",  # Прибыль (убыток) от продаж
    "line_2310",  # Доходы от участия в других организациях
    "line_2320",  # Проценты к получению
    "line_2330",  # Проценты к уплате
    "line_2340",  # Прочие доходы
    "line_2350",  # Прочие расходы
    "line_2300",  # Прибыль (убыток) до налогообложения
    "line_2410",  # Текущий налог на прибыль
    "line_2400",  # Чистая прибыль (убыток)
    # Form 3 / 4: Net Assets & Cash Flows
    "line_3600",  # Чистые активы
    "line_4100",  # Сальдо денежных потоков от текущих операций
    "line_4200",  # Сальдо денежных потоков от инвестиционных операций
    "line_4300",  # Сальдо денежных потоков от финансовых операций
    "line_4400",  # Сальдо денежных потоков за отчетный период
]


def get_column_mapping() -> dict[str, str]:
    """Map each essential line code to 'line_XXXX_descriptive'."""
    renames = load_renaming_dict()
    mapping = {}
    for line in ESSENTIAL_LINES:
        desc = renames.get(line, "")
        mapping[line] = f"{line}_{desc}" if desc else line
    return mapping


def get_district_info(district_key: str) -> dict:
    key = district_key.upper()
    if key not in FEDERAL_DISTRICTS:
        raise KeyError(f"Unknown district: {district_key}. Choices: {list(FEDERAL_DISTRICTS)}")
    return FEDERAL_DISTRICTS[key]


def query_district_okved(district_key: str, year: int | Sequence[int] | None = None) -> pl.DataFrame:
    """Query RFSD for a given district, commercial non-financial firms, and requested OKVED divisions."""
    dist_info = get_district_info(district_key)
    region_map = {code: names[1] for code, names in dist_info["regions"].items()}

    lf = (
        scan_rfsd(year=year)
        .filter(district_filter(district_key) & BASE)
        .with_columns(
            region_code().alias("region_code"),
            DIVISION.alias("okved_division"),
            SECTION.alias("section"),
        )
        .filter(pl.col("okved_division").is_in(REQUESTED_DIVISIONS))
        .with_columns(
            pl.col("region_code")
            .replace_strict(region_map, default="Неизвестный регион")
            .alias("region_name")
        )
    )

    column_mapping = get_column_mapping()
    lf = lf.rename(column_mapping)
    renamed_lines = list(column_mapping.values())

    base_cols = [
        "year",
        "region_code",
        "region_name",
        "inn",
        "ogrn",
        "okved",
        "okved_division",
        "section",
        "okopf",
    ]
    flag_cols = ["eligible", "filed", "simplified", "outlier", "imputed"]
    select_cols = base_cols + renamed_lines + flag_cols

    return (
        lf.select(select_cols)
        .sort(["year", "region_name", "okved"], descending=[True, False, False])
        .collect()
    )


def build_dynamics_summary(df: pl.DataFrame) -> pl.DataFrame:
    """Aggregate panel by year x region x division x section with outlier exclusion for financial sums."""
    clean = (pl.col("outlier") == 0).fill_null(False)
    return (
        df.group_by(["year", "region_name", "okved_division", "section"])
        .agg([
            pl.len().alias("firms_total"),
            (pl.col("filed") == 1).sum().alias("firms_filed"),
            (
                clean
                & pl.col("line_2110_PL_revenue").is_not_null()
                & (pl.col("line_2110_PL_revenue") > 0)
            )
            .sum()
            .alias("firms_revenue_positive"),
            (pl.col("line_2110_PL_revenue").filter(clean).sum() / 1e6).alias("revenue_bln"),
            (pl.col("line_2100_PL_gross_profit").filter(clean).sum() / 1e6).alias("gross_profit_bln"),
            (pl.col("line_2400_PL_net_profit").filter(clean).sum() / 1e6).alias("net_profit_bln"),
            (pl.col("line_1600_B_assets").filter(clean).sum() / 1e6).alias("assets_bln"),
            (pl.col("line_1300_B_total_equity").filter(clean).sum() / 1e6).alias("equity_bln"),
        ])
        .sort(["year", "region_name", "okved_division"])
    )


def write_full_workbook(
    xlsx_path: Path,
    df_data: pl.DataFrame | None = None,
    df_summary: pl.DataFrame | None = None,
) -> None:
    """Write multi-sheet workbook using native xlsxwriter constant_memory streaming."""
    wb = xlsxwriter.Workbook(
        str(xlsx_path),
        {"constant_memory": True, "use_zip64": True},
    )

    num_fmt_int = wb.add_format({"num_format": "#,##0"})
    num_fmt_float = wb.add_format({"num_format": "#,##0.0"})
    header_fmt = wb.add_format({"bold": True})

    # 1. Summary sheet (if provided)
    if df_summary is not None:
        ws_sum = wb.add_worksheet("Сводная")
        ws_sum.freeze_panes(1, 0)
        sum_headers = list(df_summary.columns)
        ws_sum.write_row(0, 0, sum_headers, header_fmt)
        ws_sum.autofilter(0, 0, df_summary.height, len(sum_headers) - 1)

        col_widths_sum = {
            "year": 8,
            "region_name": 32,
            "okved_division": 16,
            "section": 10,
            "firms_total": 14,
            "firms_filed": 14,
            "firms_revenue_positive": 24,
            "revenue_bln": 16,
            "gross_profit_bln": 18,
            "net_profit_bln": 16,
            "assets_bln": 16,
            "equity_bln": 16,
        }
        for idx, col in enumerate(sum_headers):
            width = col_widths_sum.get(col, max(len(col) + 2, 14))
            ws_sum.set_column(idx, idx, width)

        float_cols_sum = [c for c, dt in zip(sum_headers, df_summary.dtypes) if dt == pl.Float64]
        float_indices_sum = {sum_headers.index(c) for c in float_cols_sum if c in sum_headers}
        int_cols_sum = [c for c, dt in zip(sum_headers, df_summary.dtypes) if dt in (pl.UInt32, pl.Int64, pl.Int32) and c != "year"]
        int_indices_sum = {sum_headers.index(c) for c in int_cols_sum if c in sum_headers}

        for row_idx, row in enumerate(df_summary.iter_rows(), start=1):
            for col_idx, val in enumerate(row):
                if col_idx in float_indices_sum and val is not None:
                    ws_sum.write_number(row_idx, col_idx, val, num_fmt_float)
                elif col_idx in int_indices_sum and val is not None:
                    ws_sum.write_number(row_idx, col_idx, val, num_fmt_int)
                else:
                    ws_sum.write(row_idx, col_idx, val)

    # 2. Raw data sheet (if provided and non-empty)
    if df_data is not None and df_data.height > 0:
        ws_data = wb.add_worksheet("Данные")
        ws_data.freeze_panes(1, 0)
        headers = list(df_data.columns)
        ws_data.write_row(0, 0, headers, header_fmt)
        ws_data.autofilter(0, 0, df_data.height, len(headers) - 1)

        col_widths_data = {
            "year": 8,
            "region_code": 14,
            "region_name": 32,
            "inn": 13,
            "ogrn": 16,
            "okved": 10,
            "okved_division": 16,
            "section": 10,
            "okopf": 10,
            "eligible": 10,
            "filed": 8,
            "simplified": 12,
            "outlier": 10,
            "imputed": 10,
        }
        for idx, col in enumerate(headers):
            width = col_widths_data.get(col, max(len(col) + 2, 16))
            ws_data.set_column(idx, idx, width)

        float_cols = [c for c, dt in zip(headers, df_data.dtypes) if dt == pl.Float64]
        float_indices = {headers.index(c) for c in float_cols if c in headers}

        print(f"Streaming {df_data.height:,} rows to sheet 'Данные'...")
        t_stream = time.time()
        for row_idx, row in enumerate(df_data.iter_rows(), start=1):
            for col_idx, val in enumerate(row):
                if col_idx in float_indices and val is not None:
                    ws_data.write_number(row_idx, col_idx, val, num_fmt_int)
                else:
                    ws_data.write(row_idx, col_idx, val)
            if row_idx % 200000 == 0:
                print(f"  Streamed {row_idx:,} / {df_data.height:,} rows ({time.time()-t_stream:.1f}s)...")

    wb.close()


# Default contiguous multi-year periods for Federal Districts exceeding 1,000,000 rows.
# Every period file contains several contiguous years and < 1,000,000 rows.
DEFAULT_DISTRICT_PERIODS: dict[str, list[tuple[int, int]]] = {
    "SKFO": [(2011, 2025)],
    "YUFO": [(2011, 2017), (2018, 2025)],
    "SFO": [(2011, 2015), (2016, 2020), (2021, 2025)],
    "SZFO": [(2011, 2013), (2014, 2016), (2017, 2020), (2021, 2025)],
    "DFO": [(2011, 2017), (2018, 2025)],
}


def partition_years_into_chunks(
    year_counts: dict[int, int],
    max_rows: int = 1_000_000,
    min_years: int = 2,
    district_key: str | None = None,
) -> list[tuple[int, int]]:
    """Determine contiguous multi-year period chunks such that each chunk has < max_rows."""
    if not year_counts:
        raise ValueError("year_counts cannot be empty")

    years = sorted(year_counts.keys())
    total_rows = sum(year_counts.values())

    # If fits on a single Excel sheet (< max_rows and <= 1,048,570)
    if total_rows <= 1_048_570 and total_rows < max_rows:
        return [(years[0], years[-1])]

    # Check predefined optimal partitions first
    if district_key and district_key.upper() in DEFAULT_DISTRICT_PERIODS:
        predefined = DEFAULT_DISTRICT_PERIODS[district_key.upper()]
        predef_years = []
        for s, e in predefined:
            predef_years.extend(range(s, e + 1))
        if sorted(predef_years) == years:
            valid = True
            for s, e in predefined:
                chunk_sum = sum(year_counts.get(y, 0) for y in range(s, e + 1))
                if chunk_sum >= max_rows:
                    valid = False
                    break
            if valid:
                return predefined

    # Dynamic search for optimal contiguous partitions
    n = len(years)
    for k in range(2, min(n // min_years + 1, 8)):
        candidates = []
        for cuts in itertools.combinations(range(min_years, n - min_years + 1), k - 1):
            idx = [0] + list(cuts) + [n]
            if any(idx[i + 1] - idx[i] < min_years for i in range(k)):
                continue
            sums = [sum(year_counts.get(years[j], 0) for j in range(idx[i], idx[i + 1])) for i in range(k)]
            if all(s < max_rows for s in sums):
                # Prefer quinquennial alignment (2016, 2021)
                align_score = sum(1 for c in cuts if years[c] in (2016, 2021))
                # Minimize variance
                std = (sum((s - total_rows / k) ** 2 for s in sums) / k) ** 0.5
                candidates.append((align_score, -std, idx))
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best_idx = candidates[0][2]
            return [(years[best_idx[i]], years[best_idx[i + 1] - 1]) for i in range(k)]

    raise ValueError(f"Cannot partition years {years[0]}–{years[-1]} ({total_rows:,} rows) into chunks < {max_rows:,}")


def add_native_excel_pivot_batch(items: list[tuple[Path, str]]) -> bool:
    """Use Microsoft Excel COM automation to add Pivot Tables to multiple workbooks in a single Excel session."""
    if not items:
        return True

    items_clean = [(p.resolve(), title) for p, title in items if p.exists()]
    if not items_clean:
        return False

    script_lines = [
        "$excel = New-Object -ComObject Excel.Application",
        "$excel.Visible = $false",
        "$excel.DisplayAlerts = $false",
        "try {",
    ]

    for p, title in items_clean:
        posix_p = p.as_posix().replace("'", "''")
        safe_title = title.replace("'", "''").replace('"', '')
        script_lines.append(f"""
    $wb = $null
    try {{
        $wb = $excel.Workbooks.Open('{posix_p}')
        $wsData = $wb.Sheets.Item("Сводная")
        $lastRow = $wsData.UsedRange.Rows.Count
        $lastCol = $wsData.UsedRange.Columns.Count

        try {{
            $existingPivot = $wb.Sheets.Item("Сводная_Анализ")
            if ($existingPivot -ne $null) {{ $existingPivot.Delete() }}
        }} catch {{}}

        $wsPivot = $wb.Sheets.Add($wb.Sheets.Item(1))
        $wsPivot.Name = "Сводная_Анализ"

        $dataRange = $wsData.Range($wsData.Cells.Item(1, 1), $wsData.Cells.Item($lastRow, $lastCol))
        $pivotCache = $wb.PivotCaches().Create(1, $dataRange)
        $pivotTable = $pivotCache.CreatePivotTable($wsPivot.Range("A5"), "PivotTable1")

        $fieldRow = $pivotTable.PivotFields("region_name")
        $fieldRow.Orientation = 1

        $fieldCol = $pivotTable.PivotFields("year")
        $fieldCol.Orientation = 2

        $fieldFilter = $pivotTable.PivotFields("okved_division")
        $fieldFilter.Orientation = 3
        $fieldFilter.Caption = "Класс ОКВЭД"

        $fieldRev = $pivotTable.PivotFields("revenue_bln")
        $fieldRev.Orientation = 4
        $fieldRev.Function = -4157
        $fieldRev.NumberFormat = "0.0"
        $fieldRev.Name = "Выручка, млрд руб."

        $fieldProfit = $pivotTable.PivotFields("net_profit_bln")
        $fieldProfit.Orientation = 4
        $fieldProfit.Function = -4157
        $fieldProfit.NumberFormat = "0.0"
        $fieldProfit.Name = "Чистая прибыль, млрд руб."

        $pivotTable.TableStyle2 = "PivotStyleMedium9"

        $wsPivot.Cells.Item(1, 1).Value2 = "{safe_title}"
        $wsPivot.Cells.Item(1, 1).Font.Bold = $true
        $wsPivot.Cells.Item(1, 1).Font.Size = 14

        $wsPivot.Columns.Item(1).ColumnWidth = 32
        $lastPivotCol = $wsPivot.UsedRange.Columns.Count
        for ($c = 2; $c -le $lastPivotCol; $c++) {{
            $wsPivot.Columns.Item($c).ColumnWidth = 20
        }}

        $wsPivot.Activate()
        $wsPivot.Range("A1").Select()

        $wb.Save()
        $wb.Close()
        Write-Host "OK: {posix_p}"
    }} catch {{
        Write-Warning "Failed on {posix_p}: $_"
        try {{ if ($wb -ne $null) {{ $wb.Close($false) }} }} catch {{}}
    }}
""")

    script_lines.append("""
} finally {
    $excel.Quit()
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null
}
""")

    ps_script = "\n".join(script_lines)
    tmp_ps1 = items_clean[0][0].parent / "tmp_batch_pivot.ps1"
    try:
        tmp_ps1.write_text(ps_script, encoding="utf-8-sig")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp_ps1)],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        if proc.stdout:
            for line in proc.stdout.splitlines():
                if line.strip():
                    print(f"  [Excel COM] {line.strip()}")
        if proc.stderr:
            print(proc.stderr.strip(), file=sys.stderr)
        ok_count = proc.stdout.count("OK:")
        if ok_count < len(items_clean):
            print(f"Warning: Only {ok_count}/{len(items_clean)} workbooks had Pivot Tables attached successfully!", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"Batch Excel COM error: {e}", file=sys.stderr)
        return False
    finally:
        if tmp_ps1.exists():
            tmp_ps1.unlink(missing_ok=True)


def add_native_excel_pivot(xlsx_path: Path, district_name: str, title: str | None = None) -> bool:
    """Use Microsoft Excel COM automation to add a 100% valid native Pivot Table to 'Сводная_Анализ'."""
    p_title = title or f"Сводная таблица: динамика по {district_name} (2011–2025)"
    return add_native_excel_pivot_batch([(xlsx_path, p_title)])


def export_district(
    district_key: str,
    year: int | None,
    output_base_dir: Path,
    split_all_years: bool = False,
    max_rows_per_file: int = 1_000_000,
) -> dict:
    """Run export for a specific district."""
    dist_info = get_district_info(district_key)
    short_name = dist_info["short_name"]
    full_name = dist_info["name"]
    fo_code = district_key.lower()

    dist_dir = output_base_dir / fo_code
    dist_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n========================================================")
    print(f"Processing {short_name} ({full_name})")
    print(f"Output directory: {dist_dir}")
    print(f"========================================================")

    t0 = time.time()
    if year is not None:
        print(f"Querying firm-level data for year {year}...")
        df = query_district_okved(district_key, year=year)
        print(f"Loaded: {df.height:,} rows in {time.time()-t0:.1f}s")

        df_summary = build_dynamics_summary(df)

        year_xlsx = dist_dir / f"{fo_code}_okved_{year}.xlsx"
        print(f"Writing {year_xlsx.name}...")
        write_full_workbook(year_xlsx, df, df_summary=df_summary)

        year_parquet = dist_dir / f"{fo_code}_okved_{year}.parquet"
        df.write_parquet(year_parquet)

        print("Adding native Pivot Table via Excel COM...")
        add_native_excel_pivot(year_xlsx, short_name, f"Сводная таблица: {short_name} ({year})")

        return {
            "district": district_key,
            "mode": "single_year",
            "year": year,
            "rows": df.height,
            "files": [str(year_xlsx), str(year_parquet)],
        }
    else:
        print(f"Querying full multi-year panel (2011-2025)...")
        df_all = query_district_okved(district_key, year=None)
        print(f"Multi-year panel loaded: {df_all.height:,} rows in {time.time()-t0:.1f}s")

        # 1. Complete Parquet panel
        all_parquet = dist_dir / f"{fo_code}_okved_all_years.parquet"
        df_all.write_parquet(all_parquet)
        print(f"Saved complete panel Parquet: {all_parquet.name} ({all_parquet.stat().st_size / 1e6:.1f} MB)")

        # 2. Dynamic summary Excel (with Pivot Table across all years)
        df_summary = build_dynamics_summary(df_all)
        summary_xlsx = dist_dir / f"{fo_code}_okved_dynamics_all_years.xlsx"
        # Write only summary sheet (no raw data sheet)
        write_full_workbook(summary_xlsx, df_data=None, df_summary=df_summary)
        print(f"Saved dynamics summary Excel: {summary_xlsx.name}")

        pivot_items: list[tuple[Path, str]] = [
            (summary_xlsx, f"Сводная таблица: динамика по {short_name} (2011–2025)")
        ]
        generated_files = [str(all_parquet), str(summary_xlsx)]
        period_records: list[dict] = []

        # 3. Microdata in Excel
        if split_all_years:
            target_years = sorted(df_all["year"].unique().to_list(), reverse=True)
            by_year_dir = dist_dir / "by_year"
            by_year_dir.mkdir(parents=True, exist_ok=True)
            for yr in target_years:
                df_yr = df_all.filter(pl.col("year") == yr)
                if df_yr.is_empty():
                    continue
                df_sum_yr = build_dynamics_summary(df_yr)
                yr_xlsx = by_year_dir / f"{fo_code}_okved_{yr}.xlsx"
                print(f"Exporting firm-level workbook for {yr} ({df_yr.height:,} rows)...")
                write_full_workbook(yr_xlsx, df_data=df_yr, df_summary=df_sum_yr)
                pivot_items.append((yr_xlsx, f"Сводная таблица: {short_name} ({yr})"))
                generated_files.append(str(yr_xlsx))
                period_records.append({"start_year": yr, "end_year": yr, "rows": df_yr.height, "file": str(yr_xlsx)})
        elif df_all.height <= 1_048_570 and df_all.height < max_rows_per_file:
            print(f"Total rows ({df_all.height:,}) fit into single master Excel workbook!")
            all_xlsx = dist_dir / f"{fo_code}_okved_all_years.xlsx"
            write_full_workbook(all_xlsx, df_data=df_all, df_summary=df_summary)
            pivot_items.append((all_xlsx, f"Сводная таблица: динамика по {short_name} (2011–2025)"))
            print(f"Saved master workbook: {all_xlsx.name} ({all_xlsx.stat().st_size / 1e6:.1f} MB)")
            generated_files.append(str(all_xlsx))
            min_yr = int(df_all["year"].min()) if not df_all.is_empty() else 2011
            max_yr = int(df_all["year"].max()) if not df_all.is_empty() else 2025
            period_records.append({"start_year": min_yr, "end_year": max_yr, "rows": df_all.height, "file": str(all_xlsx)})
        else:
            print(f"Total rows ({df_all.height:,}) exceed single-sheet threshold ({max_rows_per_file:,}).")
            year_counts = {
                int(row["year"]): int(row["len"])
                for row in df_all.group_by("year").len().iter_rows(named=True)
            }
            periods = partition_years_into_chunks(
                year_counts,
                max_rows=max_rows_per_file,
                min_years=2,
                district_key=district_key,
            )
            print(f"Partitioned into {len(periods)} multi-year periods (< {max_rows_per_file:,} rows each):")
            for s_yr, e_yr in periods:
                p_cnt = sum(year_counts.get(y, 0) for y in range(s_yr, e_yr + 1))
                print(f"  - {s_yr}–{e_yr}: {p_cnt:,} rows")

            for s_yr, e_yr in periods:
                df_period = df_all.filter((pl.col("year") >= s_yr) & (pl.col("year") <= e_yr))
                df_sum_period = build_dynamics_summary(df_period)
                period_xlsx = dist_dir / f"{fo_code}_okved_{s_yr}_{e_yr}.xlsx"
                print(f"Exporting period workbook {s_yr}–{e_yr} ({df_period.height:,} rows) -> {period_xlsx.name}...")
                write_full_workbook(period_xlsx, df_data=df_period, df_summary=df_sum_period)
                pivot_items.append((period_xlsx, f"Сводная таблица: {short_name} ({s_yr}–{e_yr})"))
                generated_files.append(str(period_xlsx))
                period_records.append({"start_year": s_yr, "end_year": e_yr, "rows": df_period.height, "file": str(period_xlsx)})

        # Add Pivot Tables in batch using single Excel instance
        print(f"Attaching native Pivot Tables via Excel COM to {len(pivot_items)} workbooks...")
        pivot_ok = add_native_excel_pivot_batch(pivot_items)
        if not pivot_ok:
            print(f"Warning: Excel COM pivot creation reported warnings or failures for {short_name}", file=sys.stderr)

        return {
            "district": district_key,
            "mode": "all_years",
            "total_rows": df_all.height,
            "periods": period_records,
            "files": generated_files,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--district",
        type=str,
        default="ALL",
        help="Target district: DFO, SKFO, YUFO, SFO, SZFO, or ALL (default: ALL)",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Specific reporting year (default: export all years dynamics + period microdata)",
    )
    parser.add_argument(
        "--split-all-years",
        action="store_true",
        default=False,
        help="Export individual annual Excel workbooks for all 15 years (2011–2025) into by_year/",
    )
    parser.add_argument(
        "--max-rows-per-chunk",
        type=int,
        default=1_000_000,
        help="Maximum rows per Excel period workbook (default: 1,000,000)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Target run directory (auto-created if omitted)",
    )
    args = parser.parse_args()

    target_districts = (
        ["SKFO", "YUFO", "SFO", "SZFO"]
        if args.district.upper() == "ALL"
        else [args.district.upper()]
    )

    args.output_dir = create_run("fo-okved-excel", args.output_dir)
    print(f"Run directory: {args.output_dir}")

    results = []
    for d in target_districts:
        res = export_district(
            d,
            year=args.year,
            output_base_dir=args.output_dir,
            split_all_years=args.split_all_years,
            max_rows_per_file=args.max_rows_per_chunk,
        )
        results.append(res)

    finish_run(
        args.output_dir,
        "fo-okved-excel",
        {
            "districts": target_districts,
            "year": args.year,
            "split_all_years": args.split_all_years,
            "max_rows_per_chunk": args.max_rows_per_chunk,
            "results": results,
            "indicators_count": len(ESSENTIAL_LINES),
        },
    )
    print(f"\nAll requested exports finished successfully -> {args.output_dir}")


if __name__ == "__main__":
    main()

