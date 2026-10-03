"""Nominal GRP and GVA from the pinned Rosstat workbooks (billion RUB)."""

import math
import re
from pathlib import Path

import openpyxl
import polars as pl

from .dataset import DFO

CYRILLIC = str.maketrans({"А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H",
                         "К": "K", "М": "M", "О": "O", "Р": "P", "Т": "T"})


def number(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"Missing or non-numeric Rosstat value: {value!r}")
    return float(value)


def read_grp(path: Path) -> pl.DataFrame:
    records, official = [], {}
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        for sheet in ["1", "2"]:
            rows = list(workbook[sheet].values)
            years = {}
            for c, value in enumerate(rows[2]):
                match = re.search(r"(?:19|20)\d{2}", str(value))
                if match and 2011 <= int(match[0]) <= 2024:
                    years[c] = int(match[0])
            for row in rows[3:]:
                name = " ".join(str(row[0] or "").split())
                if name.startswith("Дальневосточный федеральный округ"):
                    official.update({y: number(row[c]) / 1000 for c, y in years.items()})
                for code, (_, label) in DFO.items():
                    if name.startswith(label):
                        records.extend({"year": y, "region_code": code, "grp_bln": number(row[c]) / 1000}
                                       for c, y in years.items())
    finally:
        workbook.close()
    result = pl.DataFrame(records)
    keys = {(r["year"], r["region_code"]) for r in records}
    expected = {(year, code) for year in range(2011, 2025) for code in DFO}
    if keys != expected or len(records) != len(expected):
        raise ValueError("GRP must contain exactly 11 regions x 14 years")
    for year, total in result.group_by("year").agg(pl.col("grp_bln").sum()).iter_rows():
        if year not in official or abs(total - official[year]) > 0.001:
            raise ValueError(f"Regional GRP sum does not match DFO in {year}")
    return result.sort("year", "region_code")


def read_gva(path: Path) -> pl.DataFrame:
    records = []
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        sheets = {s.replace(" ", ""): s for s in workbook.sheetnames}
        for year in range(2016, 2025):
            rows = list(workbook[sheets[f"2.{year}"]].values)
            matches = [row for row in rows if "Дальневосточный федеральный округ" in str(row[0])]
            if len(matches) != 1:
                raise ValueError(f"Expected one DFO row in GVA {year}")
            row = matches[0]
            values = {}
            for c in range(1, len(row)):
                header = " ".join(str(r[c] or "") for r in rows[1:5])
                match = re.search(r"Раздел\s+([A-ZА-Я])", header)
                if match:
                    section = match[1].translate(CYRILLIC)
                    if section in "ABCDEFGHIJKLMNOPQRS":
                        if section in values:
                            raise ValueError(f"Duplicate GVA section {section} in {year}")
                        values[section] = number(row[c]) / 1e6
            if set(values) != set("ABCDEFGHIJKLMNOPQRS"):
                raise ValueError(f"Incomplete GVA sections in {year}")
            if abs(sum(values.values()) - number(row[1]) / 1e6) > 0.001:
                raise ValueError(f"GVA section sum does not match total in {year}")
            records.extend({"year": year, "section": s, "gva_bln": v} for s, v in values.items())
    finally:
        workbook.close()
    return pl.DataFrame(records).sort("year", "section")


def read_comparison_details(grp_path: Path, gva_path: Path) -> dict[str, pl.DataFrame]:
    """Published volume indices, regional GVA, housing and source footnotes."""
    volumes, regional, housing, notes = [], [], [], []
    wb = openpyxl.load_workbook(grp_path, data_only=True, read_only=True)
    try:
        rows = list(wb["6"].values)
        columns = {c: int(re.search(r"20\d{2}", str(value))[0]) for c, value in enumerate(rows[2])
                   if re.search(r"20\d{2}", str(value))}
        for i, row in enumerate(rows, 1):
            label = " ".join(str(row[0] or "").split())
            group = "DFO" if label.startswith("Дальневосточный федеральный округ") else next(
                (code for code, (_, name) in DFO.items() if label.startswith(name)), None
            )
            if group:
                for col, year in columns.items():
                    volumes.append({"year": year, "dimension": "total" if group == "DFO" else "region_code",
                                    "group": group, "volume_factor": number(row[col]) / 100,
                                    "source_file": grp_path.name, "sheet": "6",
                                    "cell": f"{openpyxl.utils.get_column_letter(col + 1)}{i}"})
        for sheet in ["1", "2", "6", "13"]:
            for i, row in enumerate(wb[sheet].values, 1):
                value = str(row[0] or "").strip()
                if re.match(r"\d\)", value):
                    notes.append({"source_file": grp_path.name, "sheet": sheet, "cell": f"A{i}", "text": value})
    finally:
        wb.close()
    wb = openpyxl.load_workbook(gva_path, data_only=True, read_only=True)
    try:
        sheets = {s.replace(" ", ""): s for s in wb.sheetnames}
        for year in range(2016, 2025):
            for prefix in (["2", "1"] if year >= 2017 else ["2"]):
                sheet = sheets[f"{prefix}.{year}"]
                rows = list(wb[sheet].values)
                columns = {}
                housing_column = None
                for c in range(2, len(rows[3])):
                    header = " ".join(str(r[c] or "") for r in rows[:7])
                    match = re.search(r"Раздел\s+([A-ZА-Я])", header)
                    if match and match[1].translate(CYRILLIC) in "ABCDEFGHIJKLMNOPQRS":
                        section = match[1].translate(CYRILLIC)
                        if section in columns:
                            raise ValueError(f"Duplicate section {section}: {sheet}")
                        columns[section] = c
                    if "Услуги по проживанию в собственном жилище" in header:
                        housing_column = c
                if set(columns) != set("ABCDEFGHIJKLMNOPQRS") or housing_column is None:
                    raise ValueError(f"Missing sections or housing column: {sheet}")
                found = set()
                for i, row in enumerate(rows, 1):
                    label = " ".join(str(row[0] or "").split())
                    group = "DFO" if label.startswith("Дальневосточный федеральный округ") else next(
                        (code for code, (_, name) in DFO.items() if label.startswith(name)), None
                    )
                    if group is None or (prefix == "1" and group != "DFO"):
                        continue
                    if group in found:
                        raise ValueError(f"Duplicate region: {sheet}, {group}")
                    found.add(group)
                    if prefix == "1":
                        for section, col in columns.items():
                            volumes.append({"year": year, "dimension": "section", "group": section,
                                            "volume_factor": number(row[col]) / 100,
                                            "source_file": gva_path.name, "sheet": sheet,
                                            "cell": f"{openpyxl.utils.get_column_letter(col + 1)}{i}"})
                    else:
                        values = {s: number(row[c]) / 1e6 for s, c in columns.items()}
                        if abs(sum(values.values()) - number(row[1]) / 1e6) > 0.001:
                            raise ValueError(f"GVA sections do not sum to total: {year}, {group}")
                        for s, value in values.items():
                            regional.append({"year": year, "region_code": group, "section": s, "gva_bln": value})
                        housing.append({"year": year, "region_code": group,
                                        "housing_bln": number(row[housing_column]) / 1e6,
                                        "real_estate_total_bln": values["L"],
                                        "source_file": gva_path.name, "sheet": sheet,
                                        "cell": f"{openpyxl.utils.get_column_letter(housing_column + 1)}{i}"})
                expected = {"DFO"} if prefix == "1" else {"DFO", *DFO}
                if found != expected:
                    raise ValueError(f"Missing regional rows: {sheet}, {expected - found}")
                if year == 2024:
                    for i, row in enumerate(rows, 1):
                        value = str(row[0] or "").strip()
                        if re.match(r"\d\)", value):
                            notes.append({"source_file": gva_path.name, "sheet": sheet, "cell": f"A{i}", "text": value})
    finally:
        wb.close()
    volume = pl.DataFrame(volumes)
    if volume.height != 8 * (12 + 19) or volume.filter(pl.col("volume_factor") < 0).height:
        raise ValueError("Expected 248 nonnegative volume indices for 2017–2024")
    if volume.unique(["year", "dimension", "group"]).height != volume.height:
        raise ValueError("Duplicate volume keys")
    return {"volume_indices": volume.sort("dimension", "group", "year"),
            "regional_gva": pl.DataFrame(regional).sort("year", "region_code", "section"),
            "housing": pl.DataFrame(housing).sort("year", "region_code"),
            "source_notes": pl.DataFrame(notes)}
