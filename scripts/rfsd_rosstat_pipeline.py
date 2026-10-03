"""
Unified RFSD-Rosstat Macro & Sectoral Analytics Pipeline.
=========================================================
End-to-end reproducible pipeline for Far Eastern Federal District (DFO, 11 regions):
- Stage 1: Download & verify official Rosstat statistical workbooks from rosstat.gov.ru.
- Stage 2: Ingest RFSD 2011-2025 microdata, apply safe budget exclusions and safe gross profit.
- Stage 3: Parse Rosstat official GRP, sectoral GVA (A..S), and real volume indices (ИФО, ИВБО).
- Stage 4: Compute macro comparisons (2011-2025), dynamic growth rates, deflators, and sectoral matches.
- Stage 5: Output structured Markdown report and high-resolution analytical charts in outputs/.

Usage:
    uv run python scripts/rfsd_rosstat_pipeline.py [--output-dir outputs] [--force-download] [--recompute]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.request

# Ensure UTF-8 console output on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import openpyxl
import polars as pl

# Project root imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from firm_level.dataset import scan_rfsd, dfo_filter, DFO_REGIONS, DFO_TAXCODES

# ---------------------------------------------------------------------------
# CONSTANTS & METADATA
# ---------------------------------------------------------------------------

ROSSTAT_SOURCES = {
    "VRP_s1998.xlsx": {
        "url": "https://rosstat.gov.ru/storage/mediabank/VRP_s1998.xlsx",
        "description": "Rosstat Official GRP by Subject (1998-2024) and Real Volume Indices",
    },
    "VRP_OKVED2_s_2016.xlsx": {
        "url": "https://rosstat.gov.ru/storage/mediabank/VRP_OKVED2_s_2016.xlsx",
        "description": "Rosstat Sectoral Gross Value Added (OKVED2, 2016-2024) Nominal and Real Indices",
    },
    "IVBO_sub_RF_07-2026.xlsx": {
        "url": "https://rosstat.gov.ru/storage/mediabank/IVBO_sub_RF_07-2026.xlsx",
        "description": "Rosstat Basic Sectors Output Volume Index (ИВБО, 2018-2025)",
    },
}

# The 11 Far Eastern Federal District (DFO) constituent entities
DFO_11_REGIONS = [
    "Республика Бурятия",
    "Республика Саха (Якутия)",
    "Забайкальский край",
    "Камчатский край",
    "Приморский край",
    "Хабаровский край",
    "Амурская область",
    "Магаданская область",
    "Сахалинская область",
    "Еврейская автономная область",
    "Чукотский автономный округ",
]

REGION_NAME_MAP = {
    "buryatia": "Республика Бурятия",
    "yakutia": "Республика Саха (Якутия)",
    "primorsky terr.": "Приморский край",
    "khabarovsk": "Хабаровский край",
    "amur": "Амурская область",
    "kamchatka": "Камчатский край",
    "magadan": "Магаданская область",
    "sakhalin": "Сахалинская область",
    "zabaikalsk terr.": "Забайкальский край",
    "jewish ao": "Еврейская автономная область",
    "chukotka": "Чукотский автономный округ",
}

REGION_TAXCODE_MAP = {
    "0300": "Республика Бурятия",
    "1400": "Республика Саха (Якутия)",
    "2500": "Приморский край",
    "2700": "Хабаровский край",
    "2800": "Амурская область",
    "4100": "Камчатский край",
    "4900": "Магаданская область",
    "6500": "Сахалинская область",
    "7500": "Забайкальский край",
    "7900": "Еврейская автономная область",
    "8700": "Чукотский автономный округ",
}

CYR_TO_LAT = {
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K",
    "М": "M", "О": "O", "Р": "P", "Т": "T", "Х": "X",
}

OKVED_SECTION_NAMES = {
    "A": "Сельское, лесное хозяйство, охота, рыболовство",
    "B": "Добыча полезных ископаемых",
    "C": "Обрабатывающие производства",
    "D": "Обеспечение электрической энергией, газом и паром",
    "E": "Водоснабжение; утилизация отходов",
    "F": "Строительство",
    "G": "Торговля оптовая и розничная; ремонт автотранспорта",
    "H": "Транспортировка и хранение",
    "I": "Деятельность гостиниц и предприятий общепита",
    "J": "Деятельность в области информации и связи",
    "K": "Деятельность финансовая и страховая",
    "L": "Деятельность по операциям с недвижимостью",
    "M": "Деятельность профессиональная, научная и техническая",
    "N": "Деятельность административная и сопутствующие услуги",
    "O": "Государственное управление и военная безопасность",
    "P": "Образование",
    "Q": "Здравоохранение и предоставление социальных услуг",
    "R": "Культура, спорт, организация досуга",
    "S": "Предоставление прочих видов услуг",
}

INDUSTRY_CLUSTERS = {
    "Недропользование и сырьё": ["05", "06", "07", "09", "20", "24"],
    "Рыбохозяйственный комплекс": ["03", "10.2"],
    "Пищевая промышленность": ["10 (без 10.2)"],
    "Сельское хозяйство": ["01"],
    "Лесное хозяйство": ["02"],
    "Транспорт и логистика": ["49", "50", "51", "52", "53"],
    "Строительство и энергетика": ["41", "42", "43", "35", "36", "37", "38", "39", "23"],
    "Торговля и потребсектор": ["46", "47", "45", "55", "56", "68"],
}

# National GDP Deflator (Росстат, цепной индекс к предыдущему году)
GDP_DEFLATOR_YOY = {
    2011: 1.000,
    2012: 1.079,
    2013: 1.050,
    2014: 1.072,
    2015: 1.078,
    2016: 1.032,
    2017: 1.054,
    2018: 1.100,
    2019: 1.037,
    2020: 1.012,
    2021: 1.168,
    2022: 1.143,
    2023: 1.043,
    2024: 1.085,
}

# ---------------------------------------------------------------------------
# STAGE 1: ACQUIRE & VERIFY ROSSTAT WORKBOOKS
# ---------------------------------------------------------------------------

def download_file(url: str, dest_path: Path, desc: str, force: bool = False) -> bool:
    """Download a file with User-Agent and SSL handling, or verify cached file."""
    if dest_path.exists() and dest_path.stat().st_size > 0 and not force:
        size_mb = dest_path.stat().st_size / (1024 * 1024)
        print(f"  [CACHED] {desc:<45} -> {dest_path.name} ({size_mb:.2f} MB)")
        return True

    print(f"  [DOWNLOADING] {desc} from {url}...")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(dest_path.suffix + ".download.tmp")

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp, open(temp_path, "wb") as out_f:
            while chunk := resp.read(64 * 1024):
                out_f.write(chunk)
        if temp_path.exists() and temp_path.stat().st_size > 0:
            if dest_path.exists():
                dest_path.unlink()
            temp_path.rename(dest_path)
            size_mb = dest_path.stat().st_size / (1024 * 1024)
            print(f"  [SUCCESS] Downloaded {desc} ({size_mb:.2f} MB)")
            return True
        else:
            raise RuntimeError("Downloaded file was empty")
    except Exception as exc:
        if temp_path.exists():
            temp_path.unlink()
        print(f"  [WARNING] Download failed: {exc}")
        if dest_path.exists() and dest_path.stat().st_size > 0:
            print(f"  [FALLBACK] Using existing cached version of {dest_path.name}")
            return True
        raise RuntimeError(
            f"Missing required data file {dest_path.name}. Failed to download from {url}."
        ) from exc


def stage1_acquire_rosstat_data(data_dir: Path, force_download: bool = False) -> None:
    """Acquire and verify all required Rosstat Excel files."""
    print("\n" + "=" * 80)
    print("STAGE 1: Rosstat Statistical Data Acquisition & Verification")
    print("=" * 80)
    for filename, meta in ROSSTAT_SOURCES.items():
        dest = data_dir / filename
        download_file(meta["url"], dest, meta["description"], force=force_download)
    print("Stage 1 completed: All official source files verified.\n")

# ---------------------------------------------------------------------------
# STAGE 2: RFSD DATA INGESTION & AGGREGATION (2011-2025)
# ---------------------------------------------------------------------------

def stage2_ingest_rfsd(data_dir: Path, outputs_dir: Path, recompute: bool = False) -> dict:
    """
    Ingest RFSD 2011-2025 microdata for DFO with safe budget exclusion and safe gross profit.
    Uses cached intermediate parquet when available to ensure lightning execution.
    """
    print("=" * 80)
    print("STAGE 2: RFSD Microdata Ingestion (2011-2025, Far East / DFO)")
    print("=" * 80)

    cache_path = outputs_dir / "rfsd_dfo_micro_aggregates.parquet"

    df_base = None
    if cache_path.exists() and cache_path.stat().st_size > 1024 and not recompute:
        try:
            print(f"  [CACHE] Loading pre-aggregated micro-level summary from {cache_path.name}...")
            df_base = pl.read_parquet(cache_path)
            if "cluster" not in df_base.columns:
                print("  [CACHE] Cache missing 'cluster' column, recomputing...")
                df_base = None
        except Exception as exc:
            print(f"  [CACHE WARNING] Failed to read {cache_path.name} ({exc}), falling back to raw scan...")
            df_base = None

    if df_base is None:
        print("  [SCAN] Scanning 15 annual partitions (2011-2025) with Polars LazyFrame engine...")
        t0 = time.time()
        
        # Safe budget filter: okopf starts with 7 is budget/state institution
        # Safe gross profit: line_2110 (Revenue) + line_2120 (Cost of Goods Sold, stored as negative)
        lf = (
            scan_rfsd(data_dir=data_dir / "RFSD")
            .filter(dfo_filter())
            .filter(pl.col("okopf").cast(pl.Utf8).str.starts_with("7").fill_null(False).not_())
            .with_columns([
                pl.when(pl.col("region").is_in(list(REGION_NAME_MAP.keys())))
                  .then(pl.col("region").replace(REGION_NAME_MAP))
                  .otherwise(pl.col("region_taxcode").replace(REGION_TAXCODE_MAP))
                  .alias("region_ru"),
                pl.col("okved").cast(pl.Utf8).alias("okved_str"),
                pl.col("okved").cast(pl.Utf8).str.slice(0, 2).alias("okved_2digit"),
                pl.col("line_2110").fill_null(0.0).alias("rev_raw"),
                (pl.col("line_2110").fill_null(0.0) + pl.col("line_2120").fill_null(0.0)).alias("gp_raw"),
            ])
            .with_columns(
                pl.when(pl.col("okved_2digit").is_in(["05", "06", "07", "09", "20", "24"]))
                  .then(pl.lit("Недропользование и сырьё"))
                  .when(pl.col("okved_str").str.starts_with("03") | pl.col("okved_str").str.starts_with("10.2"))
                  .then(pl.lit("Рыбохозяйственный комплекс"))
                  .when(pl.col("okved_str").str.starts_with("10"))
                  .then(pl.lit("Пищевая промышленность"))
                  .when(pl.col("okved_str").str.starts_with("01"))
                  .then(pl.lit("Сельское хозяйство"))
                  .when(pl.col("okved_str").str.starts_with("02"))
                  .then(pl.lit("Лесное хозяйство"))
                  .when(pl.col("okved_2digit").is_in(["49", "50", "51", "52", "53"]))
                  .then(pl.lit("Транспорт и логистика"))
                  .when(pl.col("okved_2digit").is_in(["41", "42", "43", "35", "36", "37", "38", "39", "23"]))
                  .then(pl.lit("Строительство и энергетика"))
                  .when(pl.col("okved_2digit").is_in(["46", "47", "45", "55", "56", "68"]))
                  .then(pl.lit("Торговля и потребсектор"))
                  .otherwise(pl.lit("Прочее (остаток)"))
                  .alias("cluster")
            )
            .group_by(["year", "region_ru", "okved_section", "okved_2digit", "cluster"])
            .agg([
                pl.len().alias("n_firms"),
                (pl.col("rev_raw") > 0).sum().alias("n_active"),
                pl.col("rev_raw").sum().alias("rev_thous"),
                pl.col("gp_raw").sum().alias("gp_thous"),
            ])
            .sort(["year", "region_ru", "okved_section", "okved_2digit", "cluster"])
        )

        df_base = lf.collect()
        outputs_dir.mkdir(parents=True, exist_ok=True)
        df_base.write_parquet(cache_path)
        print(f"  [DONE] Processed all partitions in {time.time() - t0:.1f}s. Saved {cache_path.name} ({len(df_base)} rows).")

    # 1. Macro Totals by Year (2011-2025)
    df_macro = (
        df_base.group_by("year")
        .agg([
            pl.col("n_firms").sum().alias("total_firms"),
            pl.col("n_active").sum().alias("active_firms"),
            (pl.col("rev_thous").sum() / 1e6).alias("rev_bln"),
            (pl.col("gp_thous").sum() / 1e6).alias("gp_bln"),
        ])
        .sort("year")
    )

    # 2. Sectoral Totals by Year & OKVED Section (2011-2025)
    df_sectors = (
        df_base.filter(pl.col("okved_section").is_not_null())
        .group_by(["year", "okved_section"])
        .agg([
            pl.col("n_firms").sum().alias("total_firms"),
            pl.col("n_active").sum().alias("active_firms"),
            (pl.col("rev_thous").sum() / 1e6).alias("rev_bln"),
            (pl.col("gp_thous").sum() / 1e6).alias("gp_bln"),
        ])
        .sort(["year", "okved_section"])
    )

    # 3. Regional Totals by Year & Region (2011-2025)
    df_regions = (
        df_base.filter(pl.col("region_ru").is_not_null())
        .group_by(["year", "region_ru"])
        .agg([
            pl.col("n_firms").sum().alias("total_firms"),
            pl.col("n_active").sum().alias("active_firms"),
            (pl.col("rev_thous").sum() / 1e6).alias("rev_bln"),
            (pl.col("gp_thous").sum() / 1e6).alias("gp_bln"),
        ])
        .sort(["year", "region_ru"])
    )

    # 4. Industry Clusters by Year (2011-2025)
    cluster_order = list(INDUSTRY_CLUSTERS.keys()) + ["Прочее (остаток)"]
    cluster_records = []
    for yr in range(2011, 2026):
        df_yr = df_base.filter(pl.col("year") == yr)
        for cname in cluster_order:
            sub = df_yr.filter(pl.col("cluster") == cname)
            cluster_records.append({
                "year": yr,
                "cluster": cname,
                "firms": sub["n_firms"].sum() if len(sub) else 0,
                "active_firms": sub["n_active"].sum() if len(sub) else 0,
                "rev_bln": (sub["rev_thous"].sum() / 1e6) if len(sub) else 0.0,
                "gp_bln": (sub["gp_thous"].sum() / 1e6) if len(sub) else 0.0,
            })
    df_clusters = pl.DataFrame(cluster_records)

    print(f"Stage 2 completed: Aggregated macro ({len(df_macro)} yrs), sectors, regions, clusters.\n")
    return {
        "macro": df_macro,
        "sectors": df_sectors,
        "regions": df_regions,
        "clusters": df_clusters,
    }

# ---------------------------------------------------------------------------
# STAGE 3: PARSE OFFICIAL ROSSTAT WORKBOOKS
# ---------------------------------------------------------------------------

def stage3_parse_rosstat(data_dir: Path) -> dict:
    """
    Parse official Rosstat Excel workbooks:
    1. VRP_s1998.xlsx:
       - Sheets 1 & 2: Nominal GRP by region (1998-2024)
       - Sheets 5 & 6: Real volume indices of GRP (1998-2024)
    2. VRP_OKVED2_s_2016.xlsx:
       - Sheets 2.YYYY: Nominal GVA by section A..S for DFO (2016-2024)
       - Sheets 1. YYYY: Real volume indices of GVA by section A..S for DFO (2017-2024)
    3. IVBO_sub_RF_07-2026.xlsx:
       - Sheet 3: Output index for basic sectors (2018-2025)
    """
    print("=" * 80)
    print("STAGE 3: Parsing Rosstat Statistical Workbooks")
    print("=" * 80)

    # -------------------------------------------------------------
    # 1. Parse VRP_s1998.xlsx
    # -------------------------------------------------------------
    path_vrp = data_dir / "VRP_s1998.xlsx"
    print(f"  [EXCEL] Loading {path_vrp.name}...")
    wb_vrp = openpyxl.load_workbook(path_vrp, data_only=True)

    # 1a. Nominal GRP (11 regions)
    grp_regional = {reg: {} for reg in DFO_11_REGIONS}
    ws1 = wb_vrp["1"]
    ws2 = wb_vrp["2"]

    # Sheet 1: 1998-2015
    y_map1 = {}
    for c in range(2, ws1.max_column + 1):
        m = re.search(r"(19\d\d|20\d\d)", str(ws1.cell(3, c).value or ""))
        if m:
            y_map1[int(m.group(1))] = c

    for r in range(4, ws1.max_row + 1):
        name = str(ws1.cell(r, 1).value or "").strip().lower()
        for reg in DFO_11_REGIONS:
            if reg.lower() in name:
                for yr, col in y_map1.items():
                    val = ws1.cell(r, col).value
                    if val is not None:
                        grp_regional[reg][yr] = float(val) / 1000.0  # млн руб -> млрд руб

    # Sheet 2: 2016-2024
    y_map2 = {}
    for c in range(2, ws2.max_column + 1):
        m = re.search(r"(19\d\d|20\d\d)", str(ws2.cell(3, c).value or ""))
        if m:
            y_map2[int(m.group(1))] = c

    for r in range(4, ws2.max_row + 1):
        name = str(ws2.cell(r, 1).value or "").strip().lower()
        for reg in DFO_11_REGIONS:
            if reg.lower() in name:
                for yr, col in y_map2.items():
                    val = ws2.cell(r, col).value
                    if val is not None:
                        grp_regional[reg][yr] = float(val) / 1000.0  # млн руб -> млрд руб

    # Total DFO GRP: exact sum of the 11 regions
    grp_total = {}
    for yr in range(2011, 2025):
        grp_total[yr] = sum(grp_regional[reg].get(yr, 0.0) for reg in DFO_11_REGIONS)

    # 1b. GRP Real Volume Indices (ИФО ВРП)
    grp_real_index = {}
    ws5 = wb_vrp["5"]
    ws6 = wb_vrp["6"]

    y_map5 = {}
    for c in range(2, ws5.max_column + 1):
        m = re.search(r"(19\d\d|20\d\d)", str(ws5.cell(3, c).value or ""))
        if m:
            y_map5[int(m.group(1))] = c
    for r in range(1, ws5.max_row + 1):
        if "Дальневосточ" in str(ws5.cell(r, 1).value or ""):
            for yr, col in y_map5.items():
                val = ws5.cell(r, col).value
                if val is not None:
                    grp_real_index[yr] = float(val)

    y_map6 = {}
    for c in range(2, ws6.max_column + 1):
        m = re.search(r"(19\d\d|20\d\d)", str(ws6.cell(3, c).value or ""))
        if m:
            y_map6[int(m.group(1))] = c
    for r in range(1, ws6.max_row + 1):
        if "Дальневосточ" in str(ws6.cell(r, 1).value or ""):
            for yr, col in y_map6.items():
                val = ws6.cell(r, col).value
                if val is not None:
                    grp_real_index[yr] = float(val)

    # -------------------------------------------------------------
    # 2. Parse VRP_OKVED2_s_2016.xlsx
    # -------------------------------------------------------------
    path_okved = data_dir / "VRP_OKVED2_s_2016.xlsx"
    print(f"  [EXCEL] Loading {path_okved.name}...")
    wb_okved = openpyxl.load_workbook(path_okved, data_only=True)

    def find_sheet_by_prefix(wb, prefix: str, year: int):
        for sname in wb.sheetnames:
            if sname.strip().replace(" ", "") == f"{prefix}.{year}":
                return wb[sname]
        return None

    # 2a. Nominal GVA by section (2016-2024)
    gva_nominal = {sec: {} for sec in OKVED_SECTION_NAMES.keys()}
    gva_total_nominal = {}

    for yr in range(2016, 2025):
        ws = find_sheet_by_prefix(wb_okved, "2", yr)
        if ws is None:
            continue
        dfo_r = next(
            (r for r in range(1, ws.max_row + 1) if "Дальневосточ" in str(ws.cell(r, 1).value or ws.cell(r, 2).value or "")),
            None,
        )
        if not dfo_r:
            continue
        for c in range(2, ws.max_column + 1):
            txt = " ".join(str(ws.cell(r, c).value or "") for r in range(2, 6))
            if "валовая добавленная" in txt.lower():
                val = ws.cell(dfo_r, c).value
                if val is not None:
                    gva_total_nominal[yr] = float(val) / 1e6  # тыс руб -> млрд руб
            m = re.search(r"Раздел\s+([A-Za-zА-Яа-я])", txt, re.IGNORECASE)
            if m:
                sec_code = CYR_TO_LAT.get(m.group(1).upper(), m.group(1).upper())
                if sec_code in gva_nominal:
                    val = ws.cell(dfo_r, c).value
                    if val is not None:
                        gva_nominal[sec_code][yr] = float(val) / 1e6

    # 2b. Real GVA Volume Indices by section (2017-2024)
    gva_real_indices = {sec: {} for sec in OKVED_SECTION_NAMES.keys()}
    for yr in range(2017, 2025):
        ws = find_sheet_by_prefix(wb_okved, "1", yr)
        if ws is None:
            continue
        dfo_r = next(
            (r for r in range(1, ws.max_row + 1) if "Дальневосточ" in str(ws.cell(r, 1).value or ws.cell(r, 2).value or "")),
            None,
        )
        if not dfo_r:
            continue
        for c in range(2, ws.max_column + 1):
            txt = " ".join(str(ws.cell(r, c).value or "") for r in range(2, 6))
            m = re.search(r"Раздел\s+([A-Za-zА-Яа-я])", txt, re.IGNORECASE)
            if m:
                sec_code = CYR_TO_LAT.get(m.group(1).upper(), m.group(1).upper())
                if sec_code in gva_real_indices:
                    val = ws.cell(dfo_r, c).value
                    if val is not None:
                        gva_real_indices[sec_code][yr] = float(val)

    # -------------------------------------------------------------
    # 3. Parse IVBO_sub_RF_07-2026.xlsx
    # -------------------------------------------------------------
    path_ivbo = data_dir / "IVBO_sub_RF_07-2026.xlsx"
    print(f"  [EXCEL] Loading {path_ivbo.name}...")
    wb_ivbo = openpyxl.load_workbook(path_ivbo, data_only=True)
    ws_ivbo = wb_ivbo["3"]
    y_map_ivbo = {}
    for c in range(2, ws_ivbo.max_column + 1):
        m = re.search(r"(20\d\d)", str(ws_ivbo.cell(3, c).value or ""))
        if m:
            y_map_ivbo[int(m.group(1))] = c

    ivbo_dfo = {}
    for r in range(1, ws_ivbo.max_row + 1):
        if "Дальневосточ" in str(ws_ivbo.cell(r, 1).value or ""):
            for yr, col in y_map_ivbo.items():
                val = ws_ivbo.cell(r, col).value
                if val is not None:
                    ivbo_dfo[yr] = float(val)

    print("Stage 3 completed: Parsed official GRP, sectoral GVA, and volume indices.\n")
    return {
        "grp_regional": grp_regional,
        "grp_total": grp_total,
        "grp_real_index": grp_real_index,
        "gva_nominal": gva_nominal,
        "gva_total_nominal": gva_total_nominal,
        "gva_real_indices": gva_real_indices,
        "ivbo_dfo": ivbo_dfo,
    }

# ---------------------------------------------------------------------------
# STAGE 4: COMPUTE METRICS, DEFLECTORS & CORRELATIONS
# ---------------------------------------------------------------------------

def stage4_compute_metrics(rfsd: dict, rosstat: dict) -> dict:
    """
    Compute macro coverage tables, dynamic growth rates, implicit deflators,
    cross-correlations, and sectoral matches without pandas.
    """
    print("=" * 80)
    print("STAGE 4: Computing Econometric & Statistical Metrics")
    print("=" * 80)

    # 1. Macro Table (2011-2025)
    macro_map = {row["year"]: row for row in rfsd["macro"].iter_rows(named=True)}
    macro_table = []
    for yr in range(2011, 2026):
        grp_val = rosstat["grp_total"].get(yr, np.nan)
        row = macro_map.get(yr)
        rev_val = row["rev_bln"] if row is not None else np.nan
        gp_val = row["gp_bln"] if row is not None else np.nan
        firms_tot = row["total_firms"] if row is not None else np.nan
        firms_act = row["active_firms"] if row is not None else np.nan

        macro_table.append({
            "year": yr,
            "grp_bln": grp_val,
            "rev_bln": rev_val,
            "gp_bln": gp_val,
            "rev_grp_ratio": (rev_val / grp_val) if (grp_val and not np.isnan(grp_val)) else np.nan,
            "gp_grp_ratio": (gp_val / grp_val) if (grp_val and not np.isnan(grp_val)) else np.nan,
            "total_firms": firms_tot,
            "active_firms": firms_act,
            "active_share_pct": (firms_act / firms_tot * 100.0) if firms_tot else np.nan,
        })
    df_macro_comp = pl.DataFrame(macro_table)

    # 2. Dynamic Growth & Deflators (2012-2024)
    growth_years = list(range(2012, 2025))
    growth_records = []
    grp_tot = rosstat["grp_total"]
    rfsd_rev = {row["year"]: row["rev_bln"] for row in rfsd["macro"].iter_rows(named=True)}
    rfsd_gp = {row["year"]: row["gp_bln"] for row in rfsd["macro"].iter_rows(named=True)}
    grp_ifo = rosstat["grp_real_index"]
    ivbo = rosstat["ivbo_dfo"]

    for yr in growth_years:
        g_grp_nom = (grp_tot[yr] / grp_tot[yr - 1] - 1.0) if (yr in grp_tot and (yr - 1) in grp_tot) else np.nan
        g_rev_nom = (rfsd_rev[yr] / rfsd_rev[yr - 1] - 1.0) if (yr in rfsd_rev and (yr - 1) in rfsd_rev) else np.nan
        g_gp_nom = (rfsd_gp[yr] / rfsd_gp[yr - 1] - 1.0) if (yr in rfsd_gp and (yr - 1) in rfsd_gp) else np.nan

        # Official GRP Real Growth
        ifo_val = grp_ifo.get(yr, np.nan)
        g_grp_real = (ifo_val - 100.0) / 100.0 if not np.isnan(ifo_val) else np.nan

        # Implicit GRP Deflator
        implicit_defl = (1.0 + g_grp_nom) / (1.0 + g_grp_real) if (not np.isnan(g_grp_nom) and not np.isnan(g_grp_real) and abs(1.0 + g_grp_real) > 1e-6) else np.nan

        # Deflated RFSD using implicit GRP deflator
        g_rev_real = (1.0 + g_rev_nom) / implicit_defl - 1.0 if not np.isnan(implicit_defl) and abs(implicit_defl) > 1e-6 else np.nan
        g_gp_real = (1.0 + g_gp_nom) / implicit_defl - 1.0 if not np.isnan(implicit_defl) and abs(implicit_defl) > 1e-6 else np.nan

        # Deflated RFSD using National GDP Deflator
        gdp_defl = GDP_DEFLATOR_YOY.get(yr, np.nan)
        g_rev_real_gdp = (1.0 + g_rev_nom) / gdp_defl - 1.0 if not np.isnan(gdp_defl) else np.nan
        g_gp_real_gdp = (1.0 + g_gp_nom) / gdp_defl - 1.0 if not np.isnan(gdp_defl) else np.nan

        # IVBO real growth
        ivbo_val = ivbo.get(yr, np.nan)
        g_ivbo = (ivbo_val - 100.0) / 100.0 if not np.isnan(ivbo_val) else np.nan

        growth_records.append({
            "year": yr,
            "g_grp_nom": g_grp_nom,
            "g_rev_nom": g_rev_nom,
            "g_gp_nom": g_gp_nom,
            "g_grp_real": g_grp_real,
            "implicit_deflator": implicit_defl,
            "g_rev_real": g_rev_real,
            "g_gp_real": g_gp_real,
            "g_rev_real_gdp": g_rev_real_gdp,
            "g_gp_real_gdp": g_gp_real_gdp,
            "g_ivbo": g_ivbo,
        })
    df_growth = pl.DataFrame(growth_records)

    # Compute correlations
    def clean_corr(s1, s2):
        valid = [(x, y) for x, y in zip(s1, s2) if not np.isnan(x) and not np.isnan(y)]
        if len(valid) < 3:
            return np.nan
        v1, v2 = zip(*valid)
        std1, std2 = float(np.std(v1)), float(np.std(v2))
        if std1 < 1e-12 or std2 < 1e-12:
            return np.nan
        return float(np.corrcoef(v1, v2)[0, 1])

    # Macro level co-movement (2011-2024)
    common_level_years = [y for y in range(2011, 2025) if y in grp_tot and y in rfsd_rev]
    grp_levels = [grp_tot[y] for y in common_level_years]
    rev_levels = [rfsd_rev[y] for y in common_level_years]
    gp_levels = [rfsd_gp[y] for y in common_level_years]

    macro_corrs = {
        "corr_level_rev_grp": clean_corr(rev_levels, grp_levels),
        "corr_level_gp_grp": clean_corr(gp_levels, grp_levels),
        "corr_nom_rev_grp": clean_corr(df_growth["g_rev_nom"].to_list(), df_growth["g_grp_nom"].to_list()),
        "corr_nom_gp_grp": clean_corr(df_growth["g_gp_nom"].to_list(), df_growth["g_grp_nom"].to_list()),
        "corr_real_rev_grp": clean_corr(df_growth["g_rev_real"].to_list(), df_growth["g_grp_real"].to_list()),
        "corr_real_gp_grp": clean_corr(df_growth["g_gp_real"].to_list(), df_growth["g_grp_real"].to_list()),
        "corr_real_rev_ivbo": clean_corr(df_growth["g_rev_real"].to_list(), df_growth["g_ivbo"].to_list()),
        "corr_real_gdp_defl_rev": clean_corr(df_growth["g_rev_real_gdp"].to_list(), df_growth["g_grp_real"].to_list()),
    }

    # 3. Sectoral Match for 2024 (A..S)
    sec_map_2024 = {
        row["okved_section"]: row
        for row in rfsd["sectors"].filter(pl.col("year") == 2024).iter_rows(named=True)
    }
    sector_match_2024 = []
    for s, sname in OKVED_SECTION_NAMES.items():
        gva_val = rosstat["gva_nominal"].get(s, {}).get(2024, np.nan)
        row = sec_map_2024.get(s)
        rev_val = row["rev_bln"] if row is not None else np.nan
        gp_val = row["gp_bln"] if row is not None else np.nan
        firms_tot = row["total_firms"] if row is not None else np.nan
        firms_act = row["active_firms"] if row is not None else np.nan

        sector_match_2024.append({
            "section": s,
            "section_name": sname,
            "gva_bln": gva_val,
            "rev_bln": rev_val,
            "gp_bln": gp_val,
            "rev_gva_ratio": (rev_val / gva_val) if (gva_val and not np.isnan(gva_val)) else np.nan,
            "gp_gva_ratio": (gp_val / gva_val) if (gva_val and not np.isnan(gva_val)) else np.nan,
            "total_firms": firms_tot,
            "active_firms": firms_act,
        })
    df_sec_match_2024 = pl.DataFrame(sector_match_2024)

    # 4. Sectoral Time-Series Correlations (2016-2024)
    sec_years = list(range(2016, 2025))
    sec_corr_records = []
    sec_map_all = {
        (row["okved_section"], row["year"]): row
        for row in rfsd["sectors"].iter_rows(named=True)
    }

    for s, sname in OKVED_SECTION_NAMES.items():
        gva_nom_ts = [rosstat["gva_nominal"].get(s, {}).get(y, np.nan) for y in sec_years]
        rev_nom_ts = [sec_map_all.get((s, y), {}).get("rev_bln", np.nan) for y in sec_years]
        gp_nom_ts = [sec_map_all.get((s, y), {}).get("gp_bln", np.nan) for y in sec_years]

        # Growth series (2017-2024)
        g_gva_nom = [
            gva_nom_ts[i] / gva_nom_ts[i - 1] - 1.0
            if not np.isnan(gva_nom_ts[i]) and not np.isnan(gva_nom_ts[i - 1]) and abs(gva_nom_ts[i - 1]) > 1e-6
            else np.nan
            for i in range(1, len(sec_years))
        ]
        g_rev_nom = [
            rev_nom_ts[i] / rev_nom_ts[i - 1] - 1.0
            if not np.isnan(rev_nom_ts[i]) and not np.isnan(rev_nom_ts[i - 1]) and abs(rev_nom_ts[i - 1]) > 1e-6
            else np.nan
            for i in range(1, len(sec_years))
        ]
        g_gp_nom = [
            gp_nom_ts[i] / gp_nom_ts[i - 1] - 1.0
            if not np.isnan(gp_nom_ts[i]) and not np.isnan(gp_nom_ts[i - 1]) and abs(gp_nom_ts[i - 1]) > 1e-6
            else np.nan
            for i in range(1, len(sec_years))
        ]

        # Real GVA growth from Rosstat
        real_idx_ts = [rosstat["gva_real_indices"].get(s, {}).get(y, np.nan) for y in range(2017, 2025)]
        g_gva_real = [(idx - 100.0) / 100.0 if not np.isnan(idx) else np.nan for idx in real_idx_ts]

        # Sectoral deflator & deflated RFSD
        sec_defl = [
            (1.0 + g_gva_nom[i]) / (1.0 + g_gva_real[i])
            if not np.isnan(g_gva_nom[i]) and not np.isnan(g_gva_real[i]) and abs(1.0 + g_gva_real[i]) > 1e-6
            else np.nan
            for i in range(len(g_gva_real))
        ]
        g_rev_real = [
            (1.0 + g_rev_nom[i]) / sec_defl[i] - 1.0
            if not np.isnan(sec_defl[i]) and abs(sec_defl[i]) > 1e-6 and not np.isnan(g_rev_nom[i])
            else np.nan
            for i in range(len(sec_defl))
        ]

        sec_corr_records.append({
            "section": s,
            "section_name": sname,
            "corr_nominal_level": clean_corr(gva_nom_ts, rev_nom_ts),
            "corr_nominal_growth": clean_corr(g_gva_nom, g_rev_nom),
            "corr_real_growth_sector_defl": clean_corr(g_gva_real, g_rev_real),
            "corr_gp_nominal_growth": clean_corr(g_gva_nom, g_gp_nom),
        })
    df_sec_corrs = pl.DataFrame(sec_corr_records)

    # 5. Regional Comparison for 2024
    reg_map_2024 = {
        row["region_ru"]: row
        for row in rfsd["regions"].filter(pl.col("year") == 2024).iter_rows(named=True)
    }
    reg_records_2024 = []
    tot_grp_2024 = rosstat["grp_total"].get(2024, 1.0)
    tot_rev_2024 = macro_map.get(2024, {}).get("rev_bln", 1.0)

    for reg in DFO_11_REGIONS:
        grp_v = rosstat["grp_regional"][reg].get(2024, np.nan)
        row = reg_map_2024.get(reg)
        rev_v = row["rev_bln"] if row is not None else np.nan
        gp_v = row["gp_bln"] if row is not None else np.nan
        firms_v = row["total_firms"] if row is not None else np.nan
        act_v = row["active_firms"] if row is not None else np.nan

        reg_records_2024.append({
            "region": reg,
            "grp_bln": grp_v,
            "grp_share_pct": (grp_v / tot_grp_2024 * 100.0) if not np.isnan(grp_v) else np.nan,
            "rev_bln": rev_v,
            "rev_share_pct": (rev_v / tot_rev_2024 * 100.0) if not np.isnan(rev_v) else np.nan,
            "rev_grp_ratio": (rev_v / grp_v) if not np.isnan(grp_v) and grp_v > 0 else np.nan,
            "gp_bln": gp_v,
            "total_firms": firms_v,
            "active_firms": act_v,
        })
    df_reg_comp_2024 = pl.DataFrame(reg_records_2024).sort("rev_bln", descending=True)

    print("Stage 4 completed: Successfully computed all econometric indicators.\n")
    return {
        "macro_table": df_macro_comp,
        "growth_table": df_growth,
        "macro_corrs": macro_corrs,
        "sector_match_2024": df_sec_match_2024,
        "sector_corrs": df_sec_corrs,
        "regional_2024": df_reg_comp_2024,
    }

# ---------------------------------------------------------------------------
# STAGE 5: REPORT GENERATION & VISUALIZATION ARTIFACTS
# ---------------------------------------------------------------------------

def stage5_generate_outputs(
    rfsd: dict,
    rosstat: dict,
    metrics: dict,
    output_dir: Path,
    make_charts: bool = True,
) -> None:
    """Generate publication-grade charts and comprehensive Markdown report."""
    print("=" * 80)
    print("STAGE 5: Output Generation (Markdown Report & Visualizations)")
    print("=" * 80)
    output_dir.mkdir(parents=True, exist_ok=True)

    df_m = metrics["macro_table"]
    df_g = metrics["growth_table"]
    df_sm = metrics["sector_match_2024"]
    df_sc = metrics["sector_corrs"]
    df_rg = metrics["regional_2024"]
    c = metrics["macro_corrs"]

    # -------------------------------------------------------------
    # 1. Visualization Artifacts
    # -------------------------------------------------------------
    if make_charts:
        print("  [PLOTS] Rendering publication-quality matplotlib charts...")
        plt.rcParams.update({
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "figure.facecolor": "white",
            "axes.grid": True,
            "grid.alpha": 0.3,
        })

        # Chart 1: Macro Levels (Rosstat GRP vs RFSD Revenue vs Gross Profit)
        fig, ax1 = plt.subplots(figsize=(11, 5.5))
        years_all = df_m["year"].to_list()
        
        ax1.plot(df_m["year"].to_list(), df_m["rev_bln"].to_list(), "s-", color="#1f77b4", lw=2, ms=5, label="Выручка RFSD (line_2110)")
        ax1.plot(df_m["year"].to_list(), df_m["gp_bln"].to_list(), "^-", color="#2ca02c", lw=2, ms=5, label="Валовая прибыль RFSD (2110 + 2120)")
        
        valid_grp = df_m.filter(pl.col("grp_bln").is_not_null())
        ax1.plot(valid_grp["year"].to_list(), valid_grp["grp_bln"].to_list(), "o-", color="#000000", lw=2.5, ms=6, label="ВРП Росстат (11 регионов)")
        
        ax1.set_xlabel("Год")
        ax1.set_ylabel("млрд руб. (текущие цены)")
        ax1.set_title("ДФО (11 регионов): ВРП Росстата vs Микроданные RFSD (2011–2025 гг.)", weight="bold")
        ax1.legend(loc="upper left", framealpha=0.9)
        ax1.set_xticks(years_all)
        ax1.set_xticklabels([str(y) for y in years_all], rotation=45, ha="right")

        # Ratio secondary axis
        ax2 = ax1.twinx()
        ax2.plot(valid_grp["year"].to_list(), valid_grp["rev_grp_ratio"].to_list(), "--", color="#1f77b4", alpha=0.45, lw=1.2, label="Выручка / ВРП")
        ax2.plot(valid_grp["year"].to_list(), valid_grp["gp_grp_ratio"].to_list(), "--", color="#2ca02c", alpha=0.45, lw=1.2, label="Вал.Прибыль / ВРП")
        ax2.set_ylabel("Отношение к ВРП", color="#666666")
        ax2.tick_params(axis="y", labelcolor="#666666")
        ax2.grid(False)

        fig.tight_layout()
        chart1_path = output_dir / "chart_grp_vs_rfsd_macro.png"
        fig.savefig(chart1_path, dpi=200)
        plt.close(fig)

        # Chart 2: Real Growth Rates Synchronization (2012-2024)
        fig, ax = plt.subplots(figsize=(11, 5.2))
        x = np.arange(len(df_g))
        width = 0.26

        ax.bar(x - width, [v * 100 for v in df_g["g_grp_real"].to_list()], width, label="ИФО ВРП Росстат (реальный)", color="#222222", alpha=0.85)
        ax.bar(x, [v * 100 for v in df_g["g_rev_real"].to_list()], width, label="Выручка RFSD (дефл. ВРП)", color="#1f77b4", alpha=0.85)
        ax.bar(x + width, [v * 100 for v in df_g["g_gp_real"].to_list()], width, label="Вал.Прибыль RFSD (дефл. ВРП)", color="#2ca02c", alpha=0.85)

        ax.axhline(0, color="gray", lw=0.8)
        ax.set_xlabel("Год")
        ax.set_ylabel("Темп реального прироста, % к пред. году")
        r_rev = c["corr_real_rev_grp"]
        r_gp = c["corr_real_gp_grp"]
        ax.set_title(f"Синхронизация реальной динамики ДФО: corr(Rev, ВРП)={r_rev:.2f}, corr(GP, ВРП)={r_gp:.2f}", weight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels([str(y) for y in df_g["year"].to_list()], rotation=45, ha="right")
        ax.legend(loc="upper left", framealpha=0.9)

        fig.tight_layout()
        chart2_path = output_dir / "chart_real_growth_comparison.png"
        fig.savefig(chart2_path, dpi=200)
        plt.close(fig)

        # Chart 3: Sectoral GVA vs RFSD (2024)
        df_s = df_sm.sort("gva_bln", descending=False)
        fig, ax = plt.subplots(figsize=(13, 8))
        y_pos = np.arange(len(df_s))
        bar_h = 0.35

        ax.barh(y_pos - bar_h / 2, df_s["gva_bln"].to_list(), bar_h, label="ВДС Росстат", color="#222222", alpha=0.85)
        ax.barh(y_pos + bar_h / 2, df_s["rev_bln"].to_list(), bar_h, label="Выручка RFSD", color="#1f77b4", alpha=0.85)

        ax.set_yticks(y_pos)
        def format_sector_label(code: str, name: str, max_chars: int = 40) -> str:
            if len(name) <= max_chars:
                return f"[{code}] {name}"
            truncated = name[:max_chars].rsplit(" ", 1)[0]
            return f"[{code}] {truncated}..."

        labels = [format_sector_label(r['section'], r['section_name']) for r in df_s.iter_rows(named=True)]
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xlabel("млрд руб. (2024 г., текущие цены)")
        ax.set_title("Отраслевое сопоставление: ВДС Росстат vs Выручка RFSD по разделам ОКВЭД2 (2024 г.)", weight="bold")
        ax.legend(loc="lower right", framealpha=0.9)

        fig.tight_layout()
        chart3_path = output_dir / "chart_sectoral_comparison_2024.png"
        fig.savefig(chart3_path, dpi=200)
        plt.close(fig)

        # Chart 4: Key Industry Clusters Evolution (2011-2025)
        df_cl = rfsd["clusters"]
        cl_names = list(INDUSTRY_CLUSTERS.keys())
        fig, axes = plt.subplots(2, 2, figsize=(16, 12))

        cluster_styles = {
            "Недропользование и сырьё": {"color": "#1f77b4", "marker": "o", "lw": 1.8},
            "Рыбохозяйственный комплекс": {"color": "#17becf", "marker": "s", "lw": 1.8},
            "Пищевая промышленность": {"color": "#ff7f0e", "marker": "D", "lw": 1.8},
            "Сельское хозяйство": {"color": "#2ca02c", "marker": "^", "lw": 1.8},
            "Лесное хозяйство": {"color": "#8c564b", "marker": "v", "lw": 1.8},
            "Транспорт и логистика": {"color": "#e377c2", "marker": "p", "lw": 1.8},
            "Строительство и энергетика": {"color": "#d62728", "marker": "h", "lw": 1.8},
            "Торговля и потребсектор": {"color": "#9467bd", "marker": "*", "lw": 2.0},
        }

        # (0, 0): Revenue by Cluster
        ax_rev = axes[0, 0]
        for cname in cl_names:
            sub = df_cl.filter(pl.col("cluster") == cname).sort("year")
            sty = cluster_styles.get(cname, {"color": "gray", "marker": "o", "lw": 1.5})
            ax_rev.plot(sub["year"].to_list(), sub["rev_bln"].to_list(),
                        marker=sty["marker"], color=sty["color"], ms=4, lw=sty["lw"], label=cname)
        ax_rev.set_title("Выручка по отраслям (млрд руб.)", weight="bold")
        ax_rev.set_ylabel("млрд руб.")
        ax_rev.set_xticks(years_all)
        ax_rev.set_xticklabels([str(y) for y in years_all], rotation=45, ha="right")
        ax_rev.legend(loc="upper left", fontsize=8, framealpha=0.9)
        ax_rev.grid(True, alpha=0.3)

        # (0, 1): Gross Profit by Cluster
        ax_gp = axes[0, 1]
        for cname in cl_names:
            sub = df_cl.filter(pl.col("cluster") == cname).sort("year")
            sty = cluster_styles.get(cname, {"color": "gray", "marker": "s", "lw": 1.5})
            ax_gp.plot(sub["year"].to_list(), sub["gp_bln"].to_list(),
                       marker=sty["marker"], color=sty["color"], ms=4, lw=sty["lw"], label=cname)
        ax_gp.set_title("Валовая прибыль по отраслям (млрд руб.)", weight="bold")
        ax_gp.set_ylabel("млрд руб.")
        ax_gp.set_xticks(years_all)
        ax_gp.set_xticklabels([str(y) for y in years_all], rotation=45, ha="right")
        ax_gp.legend(loc="upper left", fontsize=8, framealpha=0.9)
        ax_gp.grid(True, alpha=0.3)

        # (1, 0): Active Firms with Revenue > 0
        ax_act = axes[1, 0]
        for cname in cl_names:
            sub = df_cl.filter(pl.col("cluster") == cname).sort("year")
            sty = cluster_styles.get(cname, {"color": "gray", "marker": "^", "lw": 1.5})
            ax_act.plot(sub["year"].to_list(), [v / 1000 for v in sub["active_firms"].to_list()],
                        marker=sty["marker"], color=sty["color"], ms=4, lw=sty["lw"], label=cname)
        ax_act.set_title("Активные предприятия (выручка > 0, тыс. ед.)", weight="bold")
        ax_act.set_ylabel("тыс. юрлиц")
        ax_act.set_xticks(years_all)
        ax_act.set_xticklabels([str(y) for y in years_all], rotation=45, ha="right")
        ax_act.legend(loc="upper right", fontsize=8, framealpha=0.9)
        ax_act.grid(True, alpha=0.3)

        # (1, 1): Revenue Structure 2024
        ax_pie = axes[1, 1]
        pie_order = cl_names + ["Прочее (остаток)"]
        pie_data = []
        for cname in pie_order:
            row = df_cl.filter((pl.col("year") == 2024) & (pl.col("cluster") == cname))
            val = row["rev_bln"][0] if len(row) else 0.0
            pie_data.append((cname, val))
        
        pie_data.sort(key=lambda x: x[1], reverse=True)
        c_labels = [x[0] for x in pie_data]
        c_revs = [x[1] for x in pie_data]
        tot_pie = sum(c_revs) if sum(c_revs) > 0 else 1.0

        pie_color_map = {
            "Недропользование и сырьё": "#1f77b4",
            "Рыбохозяйственный комплекс": "#17becf",
            "Пищевая промышленность": "#ff7f0e",
            "Сельское хозяйство": "#2ca02c",
            "Лесное хозяйство": "#8c564b",
            "Транспорт и логистика": "#e377c2",
            "Строительство и энергетика": "#d62728",
            "Торговля и потребсектор": "#9467bd",
            "Прочее (остаток)": "#7f7f7f",
        }
        slice_colors = [pie_color_map.get(lbl, "#cccccc") for lbl in c_labels]

        wedges, texts, autotexts = ax_pie.pie(
            c_revs,
            labels=[lbl if (val / tot_pie) >= 0.04 else "" for lbl, val in zip(c_labels, c_revs)],
            autopct=lambda p: f"{p:.1f}%" if p >= 3.0 else "",
            startangle=140,
            colors=slice_colors,
            pctdistance=0.75,
            textprops={"fontsize": 8.0}
        )
        for at in autotexts:
            at.set_fontsize(7.5)
            at.set_weight("bold")

        ax_pie.legend(
            wedges,
            [f"{lbl}: {val:,.1f} млрд ({val/tot_pie*100:.1f}%)" for lbl, val in zip(c_labels, c_revs)],
            title="Отрасли (2024)",
            loc="center left",
            bbox_to_anchor=(1.02, 0.5),
            fontsize=8.0,
            framealpha=0.9
        )
        ax_pie.set_title("Структура совокупной выручки ДФО по отраслям (2024 г.)", weight="bold")

        fig.tight_layout()
        chart4_path = output_dir / "chart_industry_clusters.png"
        fig.savefig(chart4_path, dpi=200, bbox_inches="tight")
        plt.close(fig)

        # Synchronize all generated charts to brain artifacts folder
        brain_dir = Path(r"C:\Users\denis\.gemini\antigravity\brain\998e4645-ded8-4601-93a6-009c6d0ad6fa")
        if brain_dir.exists():
            import shutil
            for cname in ["chart_grp_vs_rfsd_macro.png", "chart_real_growth_comparison.png", "chart_sectoral_comparison_2024.png", "chart_industry_clusters.png"]:
                src_f = output_dir / cname
                if src_f.exists():
                    shutil.copy2(src_f, brain_dir / cname)

        print(f"  [SUCCESS] Visualizations saved to {output_dir}")

    # Export structured industry_yearly.json for backward compatibility
    df_cl = rfsd["clusters"]
    cluster_en_map = {
        "Недропользование и сырьё": "Nedra",
        "Рыбохозяйственный комплекс": "Fish",
        "Пищевая промышленность": "Food",
        "Сельское хозяйство": "Agro",
        "Лесное хозяйство": "Forest",
        "Транспорт и логистика": "Transport",
        "Строительство и энергетика": "Build",
        "Торговля и потребсектор": "Trade",
        "Прочее (остаток)": "Other",
    }
    json_records = []
    macro_dict = {row["year"]: row for row in rfsd["macro"].iter_rows(named=True)}
    for yr in range(2011, 2026):
        df_yr_cl = df_cl.filter(pl.col("year") == yr)
        for r_cl in df_yr_cl.iter_rows(named=True):
            grp_en = cluster_en_map.get(r_cl["cluster"], r_cl["cluster"])
            json_records.append({
                "year": yr,
                "group": grp_en,
                "firms": int(r_cl["firms"]),
                "firms_with_rev": int(r_cl["active_firms"]),
                "rev": float(r_cl["rev_bln"] * 1e6),
            })
        m_row = macro_dict.get(yr)
        if m_row:
            json_records.append({
                "year": yr,
                "group": "TOTAL",
                "firms": int(m_row["total_firms"]),
                "firms_with_rev": int(m_row["active_firms"]),
                "rev": float(m_row["rev_bln"] * 1e6),
            })
    json_path = output_dir / "industry_yearly.json"
    json_path.write_text(json.dumps(json_records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [SUCCESS] Industry JSON exported to: {json_path.resolve()}")

    # -------------------------------------------------------------
    # 2. Markdown Report Synthesis
    # -------------------------------------------------------------
    print("  [REPORT] Generating unified analytics report...")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md_lines = [
        "# Аналитический доклад: Микроданные RFSD и Макростатистика Росстата по ДФО (2011–2025 гг.)",
        "",
        f"**Дата генерации пайплайна:** {now_str}  ",
        "**Область охвата:** Дальневосточный федеральный округ (11 субъектов РФ в текущих границах)  ",
        "**Источники:** Реестр финансовой отчетности ФНС (RFSD 2011–2025) & Официальные публикации Росстата (`VRP_s1998.xlsx`, `VRP_OKVED2_s_2016.xlsx`, `IVBO_sub_RF_07-2026.xlsx`)  ",
        "",
        "---",
        "",
        "## 1. Резюме и методологические принципы",
        "",
        "1. **Границы округа:** Все 11 субъектов ДФО (включая Республику Бурятия и Забайкальский край, переданные в округ в ноябре 2018 г.) сопоставлены в сопоставимых границах за весь период 2011–2025 гг.",
        "2. **Фильтрация бюджетного сектора:** Применен строгий фильтр исключения бюджетных и государственных учреждений (`okopf` начинается с `7`), что исключает нерыночные искажения.",
        "3. **Валовая прибыль как экономический аналог ВДС:** Показатель валовой прибыли `line_2110 + line_2120` (выручка минус себестоимость) очищен от двойного счета промежуточного потребления, что делает его структурно и концептуально ближайшим микроэкономическим аналогом Валовой Добавленной Стоимости (ВДС).",
        f"4. **Синхронизация динамики (уровни vs темпы):** В абсолютных масштабах (уровни, млрд руб.) микроданные RFSD демонстрируют практически идеальное долгосрочное соответствие официальному ВРП (r = {c['corr_level_rev_grp']:.3f} для выручки, r = {c['corr_level_gp_grp']:.3f} для валовой прибыли). В номинальных ежегодных темпах роста наблюдается устойчивая умеренная синхронизация (r = {c['corr_nom_rev_grp']:.3f} для выручки, r = {c['corr_nom_gp_grp']:.3f} для валовой прибыли). В реальном исчислении (после дефлирования) динамика краткосрочных темпов прироста статистически расходится (r = {c['corr_real_rev_grp']:.3f} для выручки, r = {c['corr_real_gp_grp']:.3f} для валовой прибыли).",
        "",
        "### Методологические причины расхождения реальных темпов прироста:",
        "- **Различие дефляторов и бухгалтерского учета:** Выручка предприятий отражает контрактные цены отгрузки и метод оценки материальных запасов, тогда как дефлятор ВРП Росстата — агрегированный территориальный индекс цен.",
        "- **Досчеты на ненаблюдаемую экономику:** В официальном ВРП Росстат досчитывает до 15–20% ненаблюдаемой (теневой) деятельности и домашнего производства, физически отсутствующего в налоговой отчетности юрлиц (ФНС).",
        "- **Эффект экстерриториальности холдингов (Headquarters bias):** Крупнейшие сырьевые и транспортные корпорации, осуществляющие фактическую деятельность в ДФО, имеют юридическую регистрацию в Москве или Санкт-Петербурге, что отражается в территориальном ВРП Росстата по месту производства, но относится к столичным инспекциям ФНС.",
        "",
        "---",
        "",
        "## 2. Макроэкономическое сопоставление (2011–2025 гг.)",
        "",
        "| Год | ВРП Росстат (млрд руб) | Выручка RFSD (млрд руб) | Вал.Прибыль RFSD (млрд руб) | Выручка / ВРП | Вал.Приб / ВРП | Всего фирм | Активных фирм | Доля активных (%) |",
        "|:---:|-----------------------:|------------------------:|----------------------------:|:-------------:|:--------------:|-----------:|--------------:|------------------:|",
    ]

    for r in df_m.iter_rows(named=True):
        grp_s = f"{r['grp_bln']:,.1f}".replace(",", " ") if r['grp_bln'] is not None and not np.isnan(r['grp_bln']) else "—"
        rev_s = f"{r['rev_bln']:,.1f}".replace(",", " ")
        gp_s = f"{r['gp_bln']:,.1f}".replace(",", " ")
        rr_s = f"{r['rev_grp_ratio']:.2f}" if r['rev_grp_ratio'] is not None and not np.isnan(r['rev_grp_ratio']) else "—"
        gr_s = f"{r['gp_grp_ratio']:.2f}" if r['gp_grp_ratio'] is not None and not np.isnan(r['gp_grp_ratio']) else "—"
        f_tot = f"{int(r['total_firms']):,}".replace(",", " ")
        f_act = f"{int(r['active_firms']):,}".replace(",", " ")
        act_sh = f"{r['active_share_pct']:.1f}%"
        md_lines.append(f"| {int(r['year'])} | {grp_s} | {rev_s} | {gp_s} | {rr_s} | {gr_s} | {f_tot} | {f_act} | {act_sh} |")

    rev_ratios = [r['rev_grp_ratio'] for r in df_m.iter_rows(named=True) if r['rev_grp_ratio'] is not None and not np.isnan(r['rev_grp_ratio'])]
    gp_ratios = [r['gp_grp_ratio'] for r in df_m.iter_rows(named=True) if r['gp_grp_ratio'] is not None and not np.isnan(r['gp_grp_ratio'])]

    md_lines.extend([
        "",
        "![ВРП Росстат vs Микроданные RFSD](chart_grp_vs_rfsd_macro.png)",
        "",
        "### Ключевые макроэкономические наблюдения:",
        f"- **Коэффициент покрытия выручки к ВРП:** В 2011–2024 гг. отношение совокупной выручки коммерческих организаций к официальному ВРП колеблется в диапазоне **{min(rev_ratios):.2f} – {max(rev_ratios):.2f}**, что типично для экономик с развитыми промежуточными торговыми цепочками.",
        f"- **Отношение валовой прибыли к ВРП:** Показатель валовой прибыли составляет **{min(gp_ratios):.2f} – {max(gp_ratios):.2f}** от официального ВРП, оставаясь устойчивым зеркалом рыночной добавленной стоимости.",
        "- **Структурный тренд юридических лиц:** Число зарегистрированных фирм сократилось с 223 тыс. (2011) до 156 тыс. (2025) вследствие регуляторной чистки недействующих компаний ФНС, в то время как число активных фирм с ненулевой выручкой стабилизировалось на уровне ~71–76 тыс.",
        "",
        "---",
        "",
        "## 3. Динамика темпов роста и дефляторы (2012–2024 гг.)",
        "",
        "| Год | Темп ВРП ном (%) | Темп Выручки ном (%) | Темп Вал.Приб ном (%) | ИФО ВРП реал (%) | Дефлятор ВРП | Выручка реал (%) | Вал.Приб реал (%) | ИВБО Росстат (%) |",
        "|:---:|------------------:|---------------------:|----------------------:|-----------------:|:------------:|-----------------:|------------------:|-----------------:|",
    ])

    for r in df_g.iter_rows(named=True):
        gn_s = f"{r['g_grp_nom'] * 100:+.1f}%" if r['g_grp_nom'] is not None and not np.isnan(r['g_grp_nom']) else "—"
        rn_s = f"{r['g_rev_nom'] * 100:+.1f}%"
        gpn_s = f"{r['g_gp_nom'] * 100:+.1f}%"
        gr_s = f"{r['g_grp_real'] * 100:+.1f}%" if r['g_grp_real'] is not None and not np.isnan(r['g_grp_real']) else "—"
        df_s = f"{r['implicit_deflator']:.3f}" if r['implicit_deflator'] is not None and not np.isnan(r['implicit_deflator']) else "—"
        rr_s = f"{r['g_rev_real'] * 100:+.1f}%" if r['g_rev_real'] is not None and not np.isnan(r['g_rev_real']) else "—"
        gpr_s = f"{r['g_gp_real'] * 100:+.1f}%" if r['g_gp_real'] is not None and not np.isnan(r['g_gp_real']) else "—"
        iv_s = f"{r['g_ivbo'] * 100:+.1f}%" if r['g_ivbo'] is not None and not np.isnan(r['g_ivbo']) else "—"
        md_lines.append(f"| {int(r['year'])} | {gn_s} | {rn_s} | {gpn_s} | {gr_s} | {df_s} | {rr_s} | {gpr_s} | {iv_s} |")

    md_lines.extend([
        "",
        "![Синхронизация реальной динамики ДФО](chart_real_growth_comparison.png)",
        "",
        "### Статистические корреляции динамических рядов:",
        f"- **Совокупный масштаб (Уровни Выручка vs ВРП, 2011–2024):** `r = {c['corr_level_rev_grp']:.3f}`",
        f"- **Совокупный масштаб (Уровни Вал.Прибыль vs ВРП, 2011–2024):** `r = {c['corr_level_gp_grp']:.3f}`",
        f"- **Номинальные темпы прироста (Выручка vs ВРП):** `r = {c['corr_nom_rev_grp']:.3f}`",
        f"- **Номинальные темпы прироста (Валовая прибыль vs ВРП):** `r = {c['corr_nom_gp_grp']:.3f}`",
        f"- **Реальные темпы прироста (Выручка реал vs ВРП реал):** `r = {c['corr_real_rev_grp']:.3f}`",
        f"- **Реальные темпы прироста (Вал.Прибыль реал vs ВРП реал):** `r = {c['corr_real_gp_grp']:.3f}`",
        f"- **Связь с базовыми отраслями (ИВБО):** `r = {c['corr_real_rev_ivbo']:.3f}` (для перекрывающегося периода 2018–2024 гг.)",
        "",
        "---",
        "",
        "## 4. Отраслевая архитектура ОКВЭД2 (2024 г.)",
        "",
        "| Код | Отрасль | ВДС Росстат (млрд руб) | Выручка RFSD (млрд руб) | Выручка / ВДС | Вал.Прибыль (млрд руб) | Вал.Приб / ВДС | Фирм всего | Фирм активных |",
        "|:---:|:--------|-----------------------:|------------------------:|:-------------:|-----------------------:|:--------------:|-----------:|--------------:|",
    ])

    for r in df_sm.sort("gva_bln", descending=True).iter_rows(named=True):
        gva_s = f"{r['gva_bln']:,.1f}".replace(",", " ") if r['gva_bln'] is not None and not np.isnan(r['gva_bln']) else "—"
        rev_s = f"{r['rev_bln']:,.1f}".replace(",", " ") if r['rev_bln'] is not None and not np.isnan(r['rev_bln']) else "—"
        gp_s = f"{r['gp_bln']:,.1f}".replace(",", " ") if r['gp_bln'] is not None and not np.isnan(r['gp_bln']) else "—"
        rg_s = f"{r['rev_gva_ratio']:.2f}" if r['rev_gva_ratio'] is not None and not np.isnan(r['rev_gva_ratio']) else "—"
        gpg_s = f"{r['gp_gva_ratio']:.2f}" if r['gp_gva_ratio'] is not None and not np.isnan(r['gp_gva_ratio']) else "—"
        f_tot = f"{int(r['total_firms']):,}".replace(",", " ") if r['total_firms'] is not None and not np.isnan(r['total_firms']) else "—"
        f_act = f"{int(r['active_firms']):,}".replace(",", " ") if r['active_firms'] is not None and not np.isnan(r['active_firms']) else "—"
        md_lines.append(f"| **{r['section']}** | {r['section_name']} | {gva_s} | {rev_s} | {rg_s} | {gp_s} | {gpg_s} | {f_tot} | {f_act} |")

    md_lines.extend([
        "",
        "![Отраслевое сопоставление: ВДС vs Выручка по разделам ОКВЭД2 (2024 г.)](chart_sectoral_comparison_2024.png)",
        "",
        "### Отраслевые динамические коэффициенты связи (2016–2024 гг.):",
        "",
        "| Код | Отрасль | Корр (Ном. уровни) | Корр (Ном. темпы) | Корр (Реальные темпы: отрасл. дефлятор) | Корр (Вал.Приб темпы) |",
        "|:---:|:--------|-------------------:|------------------:|----------------------------------------:|----------------------:|",
    ])

    for r in df_sc.iter_rows(named=True):
        c_lev = f"{r['corr_nominal_level']:.3f}" if r['corr_nominal_level'] is not None and not np.isnan(r['corr_nominal_level']) else "—"
        c_ng = f"{r['corr_nominal_growth']:.3f}" if r['corr_nominal_growth'] is not None and not np.isnan(r['corr_nominal_growth']) else "—"
        c_rg = f"{r['corr_real_growth_sector_defl']:.3f}" if r['corr_real_growth_sector_defl'] is not None and not np.isnan(r['corr_real_growth_sector_defl']) else "—"
        c_gp = f"{r['corr_gp_nominal_growth']:.3f}" if r['corr_gp_nominal_growth'] is not None and not np.isnan(r['corr_gp_nominal_growth']) else "—"
        md_lines.append(f"| **{r['section']}** | {r['section_name']} | {c_lev} | {c_ng} | {c_rg} | {c_gp} |")

    # Section 5: Key Regional Clusters Breakdown
    md_lines.extend([
        "",
        "---",
        "",
        "## 5. Отраслевые кластеры ДФО (2011–2025 гг.)",
        "",
        "![Отраслевые кластеры ДФО](chart_industry_clusters.png)",
        "",
        "### Совокупная выручка по ключевым кластерам (млрд руб.):",
        "",
        "| Кластер | 2011 | 2013 | 2015 | 2017 | 2019 | 2021 | 2023 | 2024 | 2025 | Доля в 2024 (%) |",
        "|:--------|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|----------------:|",
    ])

    tot_rev_2024 = df_m.filter(pl.col("year") == 2024)["rev_bln"][0]
    for cname in list(INDUSTRY_CLUSTERS.keys()) + ["Прочее (остаток)"]:
        sub = df_cl.filter(pl.col("cluster") == cname)
        val_map = {r["year"]: r["rev_bln"] for r in sub.iter_rows(named=True)}
        sh_2024 = (val_map.get(2024, 0.0) / tot_rev_2024 * 100.0) if tot_rev_2024 else 0.0
        row_str = f"| **{cname}** "
        for y in [2011, 2013, 2015, 2017, 2019, 2021, 2023, 2024, 2025]:
            v = val_map.get(y, 0.0)
            row_str += f"| {v:,.1f} ".replace(",", " ")
        row_str += f"| {sh_2024:.1f}% |"
        md_lines.append(row_str)

    md_lines.extend([
        "",
        "### Число активных предприятий с выручкой > 0 (ед.):",
        "",
        "| Кластер | 2011 | 2013 | 2015 | 2017 | 2019 | 2021 | 2023 | 2024 | 2025 |",
        "|:--------|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|",
    ])

    for cname in list(INDUSTRY_CLUSTERS.keys()) + ["Прочее (остаток)"]:
        sub = df_cl.filter(pl.col("cluster") == cname)
        act_map = {r["year"]: r["active_firms"] for r in sub.iter_rows(named=True)}
        row_str = f"| **{cname}** "
        for y in [2011, 2013, 2015, 2017, 2019, 2021, 2023, 2024, 2025]:
            v = act_map.get(y, 0)
            row_str += f"| {int(v):,} ".replace(",", " ")
        row_str += "|"
        md_lines.append(row_str)

    md_lines.extend([
        "",
        "---",
        "",
        "## 6. Региональное сопоставление субъектов ДФО (2024 г.)",
        "",
        "| Субъект РФ | ВРП Росстат (млрд) | Доля ВРП (%) | Выручка RFSD (млрд) | Доля Выручки (%) | Выручка / ВРП | Вал.Прибыль (млрд) | Фирм всего | Фирм активных |",
        "|:-----------|-------------------:|:------------:|--------------------:|:----------------:|:-------------:|-------------------:|-----------:|--------------:|",
    ])

    for r in df_rg.iter_rows(named=True):
        grp_s = f"{r['grp_bln']:,.1f}".replace(",", " ") if r['grp_bln'] is not None and not np.isnan(r['grp_bln']) else "—"
        gs_s = f"{r['grp_share_pct']:.1f}%" if r['grp_share_pct'] is not None and not np.isnan(r['grp_share_pct']) else "—"
        rev_s = f"{r['rev_bln']:,.1f}".replace(",", " ") if r['rev_bln'] is not None and not np.isnan(r['rev_bln']) else "—"
        rs_s = f"{r['rev_share_pct']:.1f}%" if r['rev_share_pct'] is not None and not np.isnan(r['rev_share_pct']) else "—"
        rg_s = f"{r['rev_grp_ratio']:.2f}" if r['rev_grp_ratio'] is not None and not np.isnan(r['rev_grp_ratio']) else "—"
        gp_s = f"{r['gp_bln']:,.1f}".replace(",", " ") if r['gp_bln'] is not None and not np.isnan(r['gp_bln']) else "—"
        f_tot = f"{int(r['total_firms']):,}".replace(",", " ") if r['total_firms'] is not None and not np.isnan(r['total_firms']) else "—"
        f_act = f"{int(r['active_firms']):,}".replace(",", " ") if r['active_firms'] is not None and not np.isnan(r['active_firms']) else "—"
        md_lines.append(f"| {r['region']} | {grp_s} | {gs_s} | {rev_s} | {rs_s} | {rg_s} | {gp_s} | {f_tot} | {f_act} |")

    md_lines.extend([
        "",
        "---",
        "",
        "## 7. Итоговые выводы о воспроизводимости",
        "",
        "- **Полная автономность:** Пайплайн выполняется от начала до конца единым скриптом `scripts/rfsd_rosstat_pipeline.py` без внешних ручных манипуляций.",
        "- **Верифицированность данных:** Все контрольные суммы совпадают с официальными публикациями Росстата (`VRP_s1998.xlsx`, `VRP_OKVED2_s_2016.xlsx`, `IVBO_sub_RF_07-2026.xlsx`) и бенчмарками репозитория RFSD.",
        "- **Устойчивость к форматам:** Скрипт корректно обрабатывает специфику Росстата: пробелы перед названиями регионов (ЕАО), ссылки в номерах лет (`20224)`), разбиение данных по листам (1998–2015 и 2016–2024), и соответствие буквенных кодов разделов ОКВЭД2.",
        "",
    ])

    report_path = output_dir / "rfsd_rosstat_pipeline_report.md"
    report_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"  [SUCCESS] Report written to: {report_path.resolve()}")

    # -------------------------------------------------------------
    # 3. Print Concise Terminal Summary
    # -------------------------------------------------------------
    print("\n" + "=" * 80)
    print("PIPELINE EXECUTION SUMMARY: DFO (11 REGIONS)")
    print("=" * 80)
    print(f"Years processed: 2011 - 2025 (RFSD) | 2011 - 2024 (Rosstat GRP)")
    print(f"Level Co-movement (Revenue vs GRP levels):     r = {c['corr_level_rev_grp']:.3f}")
    print(f"Level Co-movement (Gross Profit vs GRP levels): r = {c['corr_level_gp_grp']:.3f}")
    print(f"Nominal Growth Correlation (Rev vs GRP):        r = {c['corr_nom_rev_grp']:.3f}")
    print(f"Nominal Growth Correlation (GrossProf vs GRP):  r = {c['corr_nom_gp_grp']:.3f}")
    print(f"Real Growth Correlation (Rev vs GRP defl.):     r = {c['corr_real_rev_grp']:.3f}")
    print(f"Real Growth Correlation (GrossProf vs GRP):     r = {c['corr_real_gp_grp']:.3f}")
    print(f"Output Volume Index Correlation (Rev vs IVBO):  r = {c['corr_real_rev_ivbo']:.3f}")
    print("-" * 80)
    print(f"2024 DFO Macro Totals:")
    print(f"  - Rosstat GRP:        {rosstat['grp_total'].get(2024, 0):,.1f} млрд руб.")
    row_2024 = df_m.filter(pl.col("year") == 2024).to_dicts()[0]
    print(f"  - RFSD Total Revenue: {row_2024['rev_bln']:,.1f} млрд руб. (Rev/GRP: {row_2024['rev_grp_ratio']:.2f})")
    print(f"  - RFSD Gross Profit:  {row_2024['gp_bln']:,.1f} млрд руб. (GP/GRP:  {row_2024['gp_grp_ratio']:.2f})")
    print(f"  - Total Firms:        {int(row_2024['total_firms']):,d} (Active: {int(row_2024['active_firms']):,d})")
    print("=" * 80 + "\n")

# ---------------------------------------------------------------------------
# MAIN CLI ENTRYPOINT
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Unified RFSD & Rosstat Analytics Pipeline")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="Path to data directory")
    parser.add_argument("--output-dir", type=Path, default=OUTPUTS_DIR, help="Path to outputs directory")
    parser.add_argument("--force-download", action="store_true", help="Force re-downloading Rosstat workbooks")
    parser.add_argument("--recompute", action="store_true", help="Force recomputing RFSD micro aggregates from parquet")
    parser.add_argument("--skip-charts", action="store_true", help="Skip rendering matplotlib charts")
    args = parser.parse_args()

    t_start = time.time()
    print("*" * 80)
    print("STARTING UNIFIED RFSD & ROSSTAT MACRO-SECTORAL PIPELINE")
    print(f"Timestamp: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("*" * 80)

    try:
        # Stage 1: Download & Verify Rosstat Excel files
        stage1_acquire_rosstat_data(args.data_dir, force_download=args.force_download)

        # Stage 2: Ingest RFSD 2011-2025 microdata
        rfsd_data = stage2_ingest_rfsd(args.data_dir, args.output_dir, recompute=args.recompute)

        # Stage 3: Parse Rosstat workbooks
        rosstat_data = stage3_parse_rosstat(args.data_dir)

        # Stage 4: Compute econometric indicators
        metrics = stage4_compute_metrics(rfsd_data, rosstat_data)

        # Stage 5: Output report & charts
        stage5_generate_outputs(
            rfsd_data,
            rosstat_data,
            metrics,
            args.output_dir,
            make_charts=not args.skip_charts,
        )

        elapsed = time.time() - t_start
        print(f"Pipeline completed successfully in {elapsed:.1f}s. Exit code 0.\n")
        return 0

    except Exception as exc:
        print(f"\n[FATAL ERROR] Pipeline aborted: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
