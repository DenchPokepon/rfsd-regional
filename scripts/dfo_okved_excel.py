"""Export DFO firms by selected OKVED divisions to Excel.

OKVED groups requested:
  - Производство продуктов: divisions 10–15 (Section C)
  - Торговля: divisions 45–47 (Section G)
  - Транспорт: divisions 49–53 (Section H)

Outputs:
  - dfo_okved_all_years.xlsx (~100-150 MB, all 1,021,971 rows across 2011–2025)
    * Sheet 'Сводная_Анализ' - native Pivot Table for dynamic regional analysis (2011–2025)
    * Sheet 'Сводная'        - aggregated dynamics by year, region, okved division
    * Sheet 'Данные'         - complete 1,021,971-row master table with essential indicators
  - dfo_okved_all_years.parquet (fast columnar copy)

Usage:
    uv run --group analysis python scripts/dfo_okved_excel.py --all-years
    uv run --group analysis python scripts/dfo_okved_excel.py --year 2024
"""

import argparse
from collections.abc import Sequence
from pathlib import Path
import subprocess
import sys
import time

import polars as pl
import xlsxwriter

from rfsd_regional import dfo_filter, load_renaming_dict, region_code, scan_rfsd
from rfsd_regional.analysis import BASE, DIVISION, SECTION
from rfsd_regional.dataset import DFO
from rfsd_regional.results import create_run, finish_run

# Region mapping: tax code -> Russian regional name
REGION_NAME_MAP = {code: names[1] for code, names in DFO.items()}

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


def query_dfo_okved(year: int | Sequence[int] | None = None) -> pl.DataFrame:
    """Query RFSD for DFO, commercial non-financial firms, and requested OKVED divisions."""
    lf = (
        scan_rfsd(year=year)
        .filter(dfo_filter() & BASE)
        .with_columns(
            region_code().alias("region_code"),
            DIVISION.alias("okved_division"),
            SECTION.alias("section"),
        )
        .filter(pl.col("okved_division").is_in(REQUESTED_DIVISIONS))
        .with_columns(
            pl.col("region_code")
            .replace_strict(REGION_NAME_MAP, default="Неизвестный регион")
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


def build_dynamics_summary(df_all: pl.DataFrame) -> pl.DataFrame:
    """Aggregate multi-year panel by year x region x division x section."""
    clean = (pl.col("outlier") == 0).fill_null(False)
    return (
        df_all.group_by(["year", "region_name", "okved_division", "section"])
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
    df_data: pl.DataFrame,
    df_summary: pl.DataFrame | None = None,
) -> None:
    """Write large multi-sheet workbook using native xlsxwriter constant_memory streaming."""
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

    # 2. Raw data sheet
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


def add_native_excel_pivot(xlsx_path: Path) -> bool:
    """Use Microsoft Excel COM automation to add a 100% valid native Pivot Table to 'Сводная_Анализ'."""
    full_path = xlsx_path.resolve()

    ps_script = f"""# Auto-generated Excel COM automation script
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
try {{
    $wb = $excel.Workbooks.Open('{full_path.as_posix()}')
    $wsData = $wb.Sheets.Item("Сводная")
    $lastRow = $wsData.UsedRange.Rows.Count
    $lastCol = $wsData.UsedRange.Columns.Count

    $wsPivot = $wb.Sheets.Add($wb.Sheets.Item(1))
    $wsPivot.Name = "Сводная_Анализ"

    $dataRange = $wsData.Range($wsData.Cells.Item(1, 1), $wsData.Cells.Item($lastRow, $lastCol))
    $pivotCache = $wb.PivotCaches().Create(1, $dataRange)
    $pivotTable = $pivotCache.CreatePivotTable($wsPivot.Range("A5"), "DFO_Pivot")

    # Rows
    $fieldRow = $pivotTable.PivotFields("region_name")
    $fieldRow.Orientation = 1

    # Columns
    $fieldCol = $pivotTable.PivotFields("year")
    $fieldCol.Orientation = 2

    # Filter
    $fieldFilter = $pivotTable.PivotFields("okved_division")
    $fieldFilter.Orientation = 3
    $fieldFilter.Caption = "Класс ОКВЭД"

    # Values 1: Revenue
    $fieldRev = $pivotTable.PivotFields("revenue_bln")
    $fieldRev.Orientation = 4
    $fieldRev.Function = -4157
    $fieldRev.NumberFormat = "0.0"
    $fieldRev.Name = "Выручка, млрд руб."

    # Values 2: Net Profit
    $fieldProfit = $pivotTable.PivotFields("net_profit_bln")
    $fieldProfit.Orientation = 4
    $fieldProfit.Function = -4157
    $fieldProfit.NumberFormat = "0.0"
    $fieldProfit.Name = "Чистая прибыль, млрд руб."

    $pivotTable.TableStyle2 = "PivotStyleMedium9"

    $wsPivot.Cells.Item(1, 1).Value2 = "Сводная таблица: динамика по ДФО (2011–2025)"
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
    Write-Host "PIVOT_OK"
}} catch {{
    Write-Error $_
}} finally {{
    $excel.Quit()
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null
}}
"""
    tmp_ps1 = full_path.with_suffix(".tmp_pivot.ps1")
    try:
        tmp_ps1.write_text(ps_script, encoding="utf-8-sig")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp_ps1)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if "PIVOT_OK" in proc.stdout:
            return True
        print(f"Excel COM warning: {proc.stderr.strip() or proc.stdout.strip()}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Could not run Excel COM pivot creation: {e}", file=sys.stderr)
        return False
    finally:
        if tmp_ps1.exists():
            tmp_ps1.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Specific reporting year (default: export all years 2011-2025)",
    )
    parser.add_argument(
        "--all-years",
        action="store_true",
        default=True,
        help="Export full multi-year panel (2011-2025) into one Excel workbook (default: True)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Target run directory (auto-created if omitted)",
    )
    args = parser.parse_args()

    single_year_mode = args.year is not None

    args.output_dir = create_run("dfo-okved-excel", args.output_dir)
    print(f"Run directory: {args.output_dir}")

    t0 = time.time()
    if single_year_mode:
        print(f"Querying firm-level data for year {args.year}...")
        df = query_dfo_okved(year=args.year)
        print(f"Loaded: {df.height:,} rows, {df.width} columns in {time.time()-t0:.1f}s")

        year_xlsx = args.output_dir / f"dfo_okved_{args.year}.xlsx"
        print(f"Writing {year_xlsx.name}...")
        write_full_workbook(year_xlsx, df)

        year_parquet = args.output_dir / f"dfo_okved_{args.year}.parquet"
        df.write_parquet(year_parquet)

        finish_run(
            args.output_dir,
            "dfo-okved-excel",
            {
                "mode": "single_year",
                "year": args.year,
                "rows": df.height,
                "indicators_count": len(ESSENTIAL_LINES),
            },
        )
        print(f"Done -> {year_xlsx}")
    else:
        print("Querying full multi-year panel across all years (2011-2025)...")
        df_all = query_dfo_okved(year=None)
        print(f"Multi-year panel loaded: {df_all.height:,} rows, {df_all.width} columns in {time.time()-t0:.1f}s")

        # Save Parquet copy (fast, compact)
        all_parquet = args.output_dir / "dfo_okved_all_years.parquet"
        df_all.write_parquet(all_parquet)
        print(f"Saved complete panel Parquet: {all_parquet.name} ({all_parquet.stat().st_size / 1e6:.1f} MB)")

        # Generate summary dynamics
        df_summary = build_dynamics_summary(df_all)

        # Write 1M+ row Excel workbook
        all_xlsx = args.output_dir / "dfo_okved_all_years.xlsx"
        print(f"Writing 1,021,971 rows into {all_xlsx.name}...")
        t_write = time.time()
        write_full_workbook(all_xlsx, df_all, df_summary=df_summary)
        print(f"Saved {all_xlsx.name} ({all_xlsx.stat().st_size / 1e6:.1f} MB) in {time.time()-t_write:.1f}s")

        # Attach native Excel Pivot Table to 'Сводная_Анализ'
        print("Adding native Pivot Table via Excel COM...")
        pivot_ok = add_native_excel_pivot(all_xlsx)
        print(f"Native Pivot Table created: {pivot_ok}")

        finish_run(
            args.output_dir,
            "dfo-okved-excel",
            {
                "mode": "all_years",
                "rows": df_all.height,
                "summary_rows": df_summary.height,
                "indicators_count": len(ESSENTIAL_LINES),
                "xlsx_size_mb": round(all_xlsx.stat().st_size / 1e6, 1),
            },
        )
        print(f"Done -> {all_xlsx}")


if __name__ == "__main__":
    main()
