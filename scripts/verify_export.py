"""Verification script for fo_okved_excel outputs."""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

import polars as pl

EXPECTED_DISTRICT_ROWS = {
    "skfo": 472501,
    "yufo": 1547163,
    "sfo": 2484959,
    "szfo": 3120444,
}

EXPECTED_DISTRICT_PERIODS = {
    "skfo": [(2011, 2025)],
    "yufo": [(2011, 2017), (2018, 2025)],
    "sfo": [(2011, 2015), (2016, 2020), (2021, 2025)],
    "szfo": [(2011, 2013), (2014, 2016), (2017, 2020), (2021, 2025)],
}


def col_to_num(col_str: str) -> int:
    """Convert Excel column letters (A, B, ..., AX) to 1-based column index."""
    num = 0
    for c in col_str:
        num = num * 26 + (ord(c) - ord("A") + 1)
    return num


def get_excel_sheet_info(xlsx_path: Path, sheet_name: str) -> tuple[int, int]:
    """Inspect XML in .xlsx package to get (total_rows, total_cols) of a sheet in milliseconds."""
    with zipfile.ZipFile(xlsx_path) as z:
        wb_xml = ET.fromstring(z.read("xl/workbook.xml"))
        ns = {
            "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        sheet_rid = None
        for s in wb_xml.findall("main:sheets/main:sheet", ns):
            if s.attrib.get("name") == sheet_name:
                sheet_rid = s.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                break
        if not sheet_rid:
            return 0, 0
        rels_xml = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        ns_rel = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
        target = None
        for r in rels_xml.findall("rel:Relationship", ns_rel):
            if r.attrib.get("Id") == sheet_rid:
                target = r.attrib.get("Target")
                break
        if not target:
            return 0, 0
        sheet_path = "xl/" + target if not target.startswith("xl/") else target
        head = z.open(sheet_path).read(1024).decode("utf-8", errors="ignore")
        m = re.search(r'<dimension ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"', head)
        if not m:
            return 0, 0
        start_col, start_row, end_col, end_row = m.groups()
        total_rows = int(end_row) - int(start_row) + 1
        total_cols = col_to_num(end_col) - col_to_num(start_col) + 1
        return total_rows, total_cols


def verify_run(run_dir: Path) -> bool:
    print(f"\n==================================================")
    print(f"VERIFYING EXPORT DIRECTORY: {run_dir}")
    print(f"==================================================")

    all_passed = True
    workbooks_to_test: list[Path] = []

    target_districts = [d for d in EXPECTED_DISTRICT_ROWS if (run_dir / d).exists()]
    if not target_districts:
        print(f"[FAIL] No district directories found in {run_dir}")
        return False

    for fo_code in target_districts:
        expected_rows = EXPECTED_DISTRICT_ROWS[fo_code]
        fo_dir = run_dir / fo_code
        print(f"\n--- Checking district: {fo_code.upper()} ---")

        # 1. Check parquet
        parquet_path = fo_dir / f"{fo_code}_okved_all_years.parquet"
        if not parquet_path.exists():
            print(f"  [FAIL] Missing full parquet: {parquet_path.name}")
            all_passed = False
            continue
        df_parquet = pl.read_parquet(parquet_path)
        actual_parquet_rows = df_parquet.height
        print(f"  [OK] Full parquet loaded: {actual_parquet_rows:,} rows (expected: {expected_rows:,})")
        if actual_parquet_rows != expected_rows:
            print(f"  [FAIL] Parquet row mismatch! {actual_parquet_rows} != {expected_rows}")
            all_passed = False

        # Check column count & schema
        if df_parquet.width != 50:
            print(f"  [FAIL] Parquet column count mismatch: {df_parquet.width} != 50")
            all_passed = False
        else:
            print(f"  [OK] Parquet column count: 50 columns")

        if df_parquet.schema.get("inn") != pl.String or df_parquet.schema.get("ogrn") != pl.String:
            print(f"  [FAIL] INN or OGRN column is not String type!")
            all_passed = False
        else:
            inn_leading_zeros = df_parquet.filter(pl.col("inn").str.starts_with("0")).height
            print(f"  [OK] INN and OGRN are string types (found {inn_leading_zeros:,} firms with leading-zero INN)")

        # 2. Check dynamics excel
        dyn_xlsx = fo_dir / f"{fo_code}_okved_dynamics_all_years.xlsx"
        if not dyn_xlsx.exists():
            print(f"  [FAIL] Missing dynamics Excel: {dyn_xlsx.name}")
            all_passed = False
        else:
            print(f"  [OK] Dynamics Excel exists: {dyn_xlsx.name} ({dyn_xlsx.stat().st_size / 1e3:.1f} KB)")
            workbooks_to_test.append(dyn_xlsx)

        # 3. Check microdata Excel files
        expected_periods = EXPECTED_DISTRICT_PERIODS[fo_code]
        period_files: list[tuple[int, int, Path]] = []
        if len(expected_periods) == 1 and expected_periods[0] == (2011, 2025):
            master_xlsx = fo_dir / f"{fo_code}_okved_all_years.xlsx"
            if not master_xlsx.exists():
                print(f"  [FAIL] Missing master Excel: {master_xlsx.name}")
                all_passed = False
            else:
                period_files.append((2011, 2025, master_xlsx))
                workbooks_to_test.append(master_xlsx)
        else:
            for s_yr, e_yr in expected_periods:
                p_file = fo_dir / f"{fo_code}_okved_{s_yr}_{e_yr}.xlsx"
                if not p_file.exists():
                    print(f"  [FAIL] Missing period Excel: {p_file.name}")
                    all_passed = False
                else:
                    period_files.append((s_yr, e_yr, p_file))
                    workbooks_to_test.append(p_file)

        # 4. Deep verify coverage and row counts directly from physical Excel workbooks
        total_micro_rows = 0
        total_excel_data_rows = 0
        all_covered_years = []
        for s_yr, e_yr, p_file in period_files:
            expected_slice_rows = df_parquet.filter(
                (pl.col("year") >= s_yr) & (pl.col("year") <= e_yr)
            ).height
            if expected_slice_rows >= 1_000_000:
                print(f"  [FAIL] Period {s_yr}–{e_yr} has {expected_slice_rows:,} rows (>= 1,000,000)!")
                all_passed = False
            else:
                print(f"  [OK] Period {s_yr}–{e_yr} target slice: {expected_slice_rows:,} rows (< 1,000,000)")

            # Deep inspect Excel workbook package
            d_rows, d_cols = get_excel_sheet_info(p_file, "Данные")
            actual_excel_rows = max(0, d_rows - 1)
            if actual_excel_rows != expected_slice_rows:
                print(f"  [FAIL] Physical Excel file {p_file.name} sheet 'Данные' rows ({actual_excel_rows:,}) != expected ({expected_slice_rows:,})!")
                all_passed = False
            elif d_cols != 50:
                print(f"  [FAIL] Physical Excel file {p_file.name} sheet 'Данные' columns ({d_cols}) != 50!")
                all_passed = False
            else:
                print(f"  [OK] Physical Excel file {p_file.name} verified: {actual_excel_rows:,} data rows, {d_cols} columns")

            total_micro_rows += expected_slice_rows
            total_excel_data_rows += actual_excel_rows
            all_covered_years.extend(range(s_yr, e_yr + 1))

        if total_micro_rows != expected_rows or total_excel_data_rows != expected_rows:
            print(f"  [FAIL] Microdata rows sum (Parquet: {total_micro_rows:,}, Excel: {total_excel_data_rows:,}) != expected ({expected_rows:,})")
            all_passed = False
        else:
            print(f"  [OK] Zero omitted and zero duplicated rows in both Parquet and Excel: sum = {total_excel_data_rows:,}")

        if sorted(all_covered_years) != list(range(2011, 2026)):
            print(f"  [FAIL] Covered years mismatch: {all_covered_years}")
            all_passed = False
        else:
            print(f"  [OK] Complete 2011–2025 contiguous year coverage")

    # 5. Use PowerShell COM to verify opening, sheet names, and PivotTables in every workbook
    print(f"\n==================================================")
    print(f"VERIFYING EXCEL WORKBOOKS VIA EXCEL COM AUTOMATION")
    print(f"Total workbooks to test: {len(workbooks_to_test)}")
    print(f"==================================================")

    ps_check_lines = [
        "$excel = New-Object -ComObject Excel.Application",
        "$excel.Visible = $false",
        "$excel.DisplayAlerts = $false",
        "$failed = 0",
        "try {",
    ]
    for wb_path in workbooks_to_test:
        posix = wb_path.resolve().as_posix()
        is_dynamics = "dynamics" in wb_path.name
        ps_check_lines.append(f"""
    $wb = $null
    try {{
        $wb = $excel.Workbooks.Open('{posix}')
        $sheetCount = $wb.Sheets.Count
        $sheetNames = @()
        foreach ($s in $wb.Sheets) {{ $sheetNames += $s.Name }}
        
        $pivotsCount = 0
        $ptName = ""
        $ptSource = ""
        try {{
            $wsPivot = $wb.Sheets.Item("Сводная_Анализ")
            $pivotsCount = $wsPivot.PivotTables().Count
            if ($pivotsCount -ge 1) {{
                $pt = $wsPivot.PivotTables(1)
                $ptName = $pt.Name
                $ptSource = $pt.SourceData
            }}
        }} catch {{}}

        $hasDataSheet = $false
        $dataRows = 0
        $dataCols = 0
        try {{
            $wsData = $wb.Sheets.Item("Данные")
            if ($wsData -ne $null) {{
                $hasDataSheet = $true
                $dataRows = $wsData.UsedRange.Rows.Count - 1
                $dataCols = $wsData.UsedRange.Columns.Count
            }}
        }} catch {{}}

        $hasSumSheet = $false
        try {{
            $wsSum = $wb.Sheets.Item("Сводная")
            if ($wsSum -ne $null) {{ $hasSumSheet = $true }}
        }} catch {{}}

        $wb.Close($false)

        $expectedSheets = if ('{is_dynamics}' -eq 'True') {{ 2 }} else {{ 3 }}
        $valid = ($sheetCount -ge $expectedSheets) -and ($pivotsCount -ge 1) -and $hasSumSheet
        if ('{is_dynamics}' -ne 'True') {{
            $valid = $valid -and $hasDataSheet -and ($dataCols -eq 50) -and ($dataRows -gt 0)
        }}

        if ($valid) {{
            Write-Host "COM_OK: {wb_path.name} | Sheets: $($sheetNames -join ', ') | Pivot: $ptName ($ptSource)"
        }} else {{
            Write-Warning "COM_FAIL: {wb_path.name} | Sheets: $($sheetNames -join ', ') | PivotTables: $pivotsCount | DataRows: $dataRows"
            $failed++
        }}
    }} catch {{
        Write-Warning "COM_ERROR: {wb_path.name} - $_"
        $failed++
        try {{ if ($wb -ne $null) {{ $wb.Close($false) }} }} catch {{}}
    }}
""")

    ps_check_lines.append("""
} finally {
    $excel.Quit()
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null
    if ($failed -gt 0) { exit 1 } else { exit 0 }
}
""")

    tmp_check_ps1 = run_dir / "verify_com.ps1"
    try:
        tmp_check_ps1.write_text("\n".join(ps_check_lines), encoding="utf-8-sig")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp_check_ps1)],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        if proc.stdout:
            for l in proc.stdout.splitlines():
                print(f"  {l}")
        if proc.stderr:
            for l in proc.stderr.splitlines():
                print(f"  [STDERR] {l}")
        if proc.returncode != 0:
            print(f"[FAIL] Excel COM verification reported failures (exit code {proc.returncode})")
            all_passed = False
        else:
            print(f"[OK] All Excel workbooks verified cleanly via Excel COM automation!")
    finally:
        if tmp_check_ps1.exists():
            tmp_check_ps1.unlink(missing_ok=True)

    if all_passed:
        print("\n==================================================")
        print("ALL VERIFICATIONS PASSED SUCCESSFULLY!")
        print("==================================================")
    else:
        print("\n==================================================")
        print("VERIFICATION FAILED! SEE ERRORS ABOVE.")
        print("==================================================")
    return all_passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path, help="Run directory to verify")
    args = parser.parse_args()
    ok = verify_run(args.run_dir)
    sys.exit(0 if ok else 1)
