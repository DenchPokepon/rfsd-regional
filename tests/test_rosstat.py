import openpyxl
import pytest

from firm_level.dataset import DFO
from firm_level.rosstat import read_grp, read_gva


def test_grp_sums_regions_and_rejects_an_incomplete_workbook(tmp_path):
    path = tmp_path / "grp.xlsx"
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, years in [("1", range(2011, 2016)), ("2", range(2016, 2025))]:
        ws = wb.create_sheet(title)
        ws.append([])
        ws.append([])
        ws.append([None, *[f"{year}4)" for year in years]])
        ws.append(["Дальневосточный федеральный округ", *[11000] * len(years)])
        for _, label in DFO.values():
            ws.append([f"  {label}  ", *[1000] * len(years)])
    wb.save(path)
    result = read_grp(path)
    assert result.height == 154
    assert result["grp_bln"].sum() == 154
    wb["1"].delete_rows(5)
    wb.save(path)
    with pytest.raises(ValueError, match="11 regions"):
        read_grp(path)


def test_gva_units_sections_and_missing_values(tmp_path):
    path = tmp_path / "gva.xlsx"
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for year in range(2016, 2025):
        ws = wb.create_sheet(f"2. {year}")
        ws.append([])
        ws.append([])
        ws.append([None, "Валовая добавленная стоимость"])
        ws.append([None, None, *[f"Раздел {s}" for s in "ABCDEFGHIJKLMNОPQRS"]])
        ws.append([])
        ws.append(["Дальневосточный федеральный округ", 19e6, *[1e6] * 19])
    wb.save(path)
    result = read_gva(path)
    assert result.height == 171
    assert result["gva_bln"].sum() == 171
    wb["2. 2024"].cell(6, 3, "not a number")
    wb.save(path)
    with pytest.raises(ValueError, match="non-numeric"):
        read_gva(path)
