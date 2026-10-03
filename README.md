# Russian Financial Statements Database (RFSD) - Firm-Level Analytics

Рабочие документы: [план и статусы](docs/plan.md), [правила выборки и показателей](docs/analysis_rules.md).
Правила зафиксированы; аналитические скрипты и прежние отчёты ещё предстоит привести к ним по плану.

Laconic, flexible, state-of-the-art working environment for high-throughput empirical corporate finance and regional macro research based on **[RFSD (Russian Financial Statements Database)](https://github.com/irlcode/RFSD)** (2011–2025).

Built with **`uv`**, **Python 3.11**, and **Polars** as the native lazy query engine. Zero-bloat, direct Apache Parquet scanning with Hive partition pruning, predicate pushdown, and projection pushdown.

---

## ⚡ Quickstart

### 1. Interactive REPL (Fastest Entry Point)
To start analyzing data immediately in an interactive Python shell with the LazyFrames preloaded:
```bash
uv run python -i explore.py
```
In the REPL, the following objects are already in scope:
- `lf`: Raw Polars LazyFrame over all partitions (`hive_partitioning=True`)
- `lf_desc`: LazyFrame with descriptive names (`PL_revenue`, `B_total_assets`, `PL_net_profit`, etc.)
- `dfo`: Predicate expression for Far Eastern Federal District (`dfo_filter()`)
- `pl`: The `polars` module

Example query inside REPL:
```python
>>> # Top 5 companies by revenue in DFO in 2024
>>> res = (
...     lf.filter((pl.col("year") == 2024) & dfo)
...     .select(["inn", "region", "okved", "line_2110", "line_2400"])
...     .sort("line_2110", descending=True)
...     .head(5)
...     .collect()
... )
>>> print(res)
```

### 2. Check Dataset Status
Check downloaded partitions, disk space, and firm counts:
```bash
uv run firm-level status
```

### 3. Run DFO Regional & OKVED Analysis
Replicates the task from Sergey Anufrievich (OKVED `xx.xx` activity, revenue aggregation, identifying the "head" firms):
```bash
uv run python scripts/dfo_analysis.py --year 2024 --top-sectors 15 --top-firms 10
```
Outputs are automatically saved to `outputs/` in CSV format.

---

## 📁 Project Structure

```text
firm-level/
├── pyproject.toml              # uv project specification (Python 3.11, Polars, PyArrow)
├── README.md                   # Complete reference & cookbook
├── explore.py                  # Quick demo & interactive REPL entry point
├── src/
│   └── firm_level/
│       ├── __init__.py         # Exports scan_rfsd, dfo_filter, DFO_REGIONS, etc.
│       ├── dataset.py          # Native Polars scan_parquet helper & Hive loader
│       └── cli.py              # CLI entry point (status, explore, dfo)
├── scripts/
│   ├── download_data.py        # Multi-threaded Hugging Face partition downloader
│   └── dfo_analysis.py         # DFO OKVED & regional activity analysis
├── data/
│   ├── RFSD/                   # Hive-partitioned Parquet files: year=YYYY/part-0.parquet
│   └── descriptive_names_dict.csv  # Mapping from line_XXXX to descriptive names
└── outputs/                    # Exported analysis CSVs and parquet extracts
```

---

## 🧱 Native Polars Idioms (Zero-Bloat Cheatsheet)

### Scanning without Loading into RAM
```python
from firm_level import scan_rfsd, dfo_filter
import polars as pl

# Lazy scan across all years (2011-2025)
lf = scan_rfsd()

# Lazy scan with human-readable financial names
lf_desc = scan_rfsd(descriptive=True)
```

### Filtering DFO (Far East) Companies
```python
from firm_level import scan_rfsd, dfo_filter
import polars as pl

dfo_firms = (
    scan_rfsd(year=2024)
    .filter(dfo_filter())
    .select(["inn", "region", "region_taxcode", "okved", "line_2110"])
    .collect()
)
```

### OKVED `xx.xx` Detail Grouping
```python
okved_activity = (
    scan_rfsd(year=2024)
    .filter(dfo_filter() & pl.col("okved").is_not_null())
    .with_columns(
        okved_2d=pl.col("okved").str.slice(0, 5) # 4 digits: xx.xx
    )
    .group_by(["region", "okved_2d"])
    .agg([
        pl.len().alias("n_firms"),
        (pl.col("line_2110").sum() / 1e6).alias("revenue_bln_rub"),
        (pl.col("line_1600").sum() / 1e6).alias("assets_bln_rub"),
    ])
    .sort("revenue_bln_rub", descending=True)
    .collect()
)
```

---

## 🔬 Research Memo Mapping

### 1. DFO Activity & GRP Proxy (С.А. Ануфриевич)
- **Problem:** Official regional Rosstat stats are aggregated and lagged; need micro-level proxy for GRP.
- **RFSD Implementation:**
  - Aggregated revenue (`line_2110`) by region and sector acts as a bottom-up proxy for gross output.
  - Sector detail at `okved.str.slice(0, 5)` (`xx.xx`) isolates key DFO drivers (07.29 extraction of non-ferrous metals, 03.11 marine fishing, 46.71 wholesale fuels, 41.20 construction, 52.22 transport support).
  - The script `scripts/dfo_analysis.py` extracts the top "head" enterprises driving the bulk of regional turnover.

### 2. Granular Sources of Regional Cycles (Д. Крылов)
- **Design:** Purge firm revenue growth from macroeconomic and sectoral fixed effects:
  $$\Delta \ln Y_{it} = \alpha_t + \beta_{s,t} + \varepsilon_{it}$$
- Residual $\varepsilon_{it}$ is the idiosyncratic firm shock.
- Aggregate granular residual by region: $\Gamma_{rt} = \sum_{i \in r} w_{irt-1} \varepsilon_{it}$.
- Test if $\Gamma_{rt}$ predicts regional GRP and investment fluctuations.

### 3. Bank Branch Closures & Local Real Economy
- Combine bank licensing/office history with RFSD municipal tags (`oktmo`, `lon`, `lat`).
- Event study / DiD on entry, investment, and survival of local SMEs.

### 4. Physical Climate Shocks & Financial Resilience
- Join geocoded firm coordinates (`lon`, `lat`) with ERA5-Land gridded reanalysis (temperature/precipitation extremes) or NASA FIRMS (wildfires).
- Identify vulnerability interaction with pre-shock balance sheet metrics:
  - Liquidity: `line_1250` (cash) / `line_1600` (assets)
  - Leverage: `(line_1400 + line_1500) / line_1600`
  - Current ratio: `line_1200 / line_1500`

### 5. Debt Repricing & Monetary Policy Transmission
- Financial expenses (`line_2330` - interest payable) and debt maturity structure (`line_1410` long-term borrowings vs `line_1510` short-term borrowings).

---

## 🛠️ Data Downloader
To re-run or resume the full 2011–2025 download at any time:
```bash
uv run python scripts/download_data.py
```
Data is downloaded directly into `data/RFSD/year=YYYY/part-0.parquet`.
