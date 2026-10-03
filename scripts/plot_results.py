"""Four shared figures from verified audit/comparison runs; no hidden recalculation."""

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter
import polars as pl

from firm_level.results import create_run, file_hash, finish_run

BLUE, TEAL, ORANGE, GREY = "#244C70", "#187E80", "#C96E32", "#82909D"
SECTORS = {
    "A": "Сельское, лесное хозяйство, рыболовство", "B": "Добыча", "C": "Обработка",
    "D": "Энергетика", "E": "Вода, отходы", "F": "Строительство", "G": "Торговля и ремонт авто",
    "H": "Транспорт и хранение", "I": "Гостиницы и общепит", "J": "Информация и связь",
    "K": "Финансы и страхование", "L": "Недвижимость", "M": "Профессиональная и научная",
    "N": "Административные услуги", "O": "Госуправление", "P": "Образование",
    "Q": "Здравоохранение", "R": "Культура и спорт", "S": "Прочие услуги",
}
CLUSTERS = ["Недропользование и сырьё", "Рыбный комплекс", "Пищепром", "Сельское хозяйство",
            "Лесное хозяйство", "Транспорт и логистика", "Строительство, энергетика и коммунальные виды",
            "Торговля и потребсектор", "Прочее"]


def verified_run(path: Path) -> dict:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for name, info in manifest["files"].items():
        if file_hash(path / name) != info["sha256"]:
            raise ValueError(f"Changed result: {path / name}")
    return manifest


def number(value, decimals=0):
    return f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")


def setup_style():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 11, "axes.titlesize": 12,
        "axes.titleweight": "bold", "axes.labelcolor": "#394B59", "text.color": "#243440",
        "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#CBD2D8",
        "axes.labelsize": 10, "xtick.color": "#52616B", "ytick.color": "#52616B",
        "legend.frameon": False, "legend.fontsize": 9, "svg.fonttype": "none",
        "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    })


def decorate(ax, title, xlabel=None):
    ax.set_title(title, loc="left", pad=14)
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#E8EDF0", linewidth=.7)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, pos: number(value)))
    if xlabel:
        ax.set_xlabel(xlabel)


def save(fig, directory, stem, title, note):
    fig.suptitle(title, x=.06, y=.97, ha="left", fontsize=17, fontweight="bold")
    fig.text(.06, .035, note, va="bottom", fontsize=9, color="#52616B", linespacing=1.5)
    fig.savefig(directory / f"{stem}.png", dpi=160)
    fig.savefig(directory / f"{stem}.svg")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-run", type=Path, required=True)
    parser.add_argument("--comparison-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    audit_meta, comparison_meta = [verified_run(p) for p in [args.audit_run, args.comparison_run]]
    if audit_meta["sources"]["rfsd"] != comparison_meta["sources"]["rfsd"]:
        raise ValueError("RFSD source versions differ")
    audit_run = json.loads((args.audit_run / "run.json").read_text(encoding="utf-8"))
    audit_exclusions = {(r["inn"], r["year"]) for r in audit_run["manual_exclusions"]}
    comparison_exclusions = {(r["inn"], r["year"]) for r in comparison_meta["metadata"]["manual_exclusions"]}
    if audit_exclusions != comparison_exclusions:
        raise ValueError("Manual exclusions differ between runs")
    if audit_run["years"] != list(range(2011, 2026)):
        raise ValueError("Coverage figures require all 2011–2025 years")
    manual = audit_run["manual_exclusions"]
    manual_note = "есть ручные исключения ИНН–год" if manual else "ручных исключений нет"
    cleaning = "outlier и ручных решений" if manual else "outlier"
    coverage = pl.read_parquet(args.audit_run / "coverage.parquet")
    groups = pl.read_parquet(args.audit_run / "groups.parquet")
    levels = pl.read_parquet(args.comparison_run / "levels_long.parquet")
    growth = pl.read_parquet(args.comparison_run / "growth.parquet")
    panel = pl.read_parquet(args.comparison_run / "adjacent_growth.parquet")
    housing = pl.read_parquet(args.comparison_run / "housing.parquet")
    macro = levels.filter((pl.col("dimension") == "total") & (pl.col("metric") == "revenue_bln"))
    check = macro.join(coverage, on=["year", "sample"], validate="1:1")
    if check.height != 30 or check.filter((pl.col("rfsd_bln") - pl.col("revenue_bln")).abs() > 1e-7).height:
        raise ValueError("Audit and comparison totals differ")
    output = create_run("figures", args.output_dir)
    setup_style()
    clean = coverage.filter(pl.col("sample") == "clean").sort("year").with_columns(
        (100 * pl.col("revenue_observed") / pl.col("firms")).alias("observed_pct"),
        pl.when(pl.col("imputed") == 0).then(0.)
        .otherwise(100 * pl.col("imputed_revenue_bln") / pl.col("revenue_bln")).alias("imputed_revenue_pct"),
    )
    all_dfo = coverage.filter(pl.col("sample") == "all_dfo").sort("year")
    fig, axes = plt.subplots(1, 2, figsize=(13, 6.6))
    fig.subplots_adjust(left=.075, right=.97, top=.81, bottom=.23, wspace=.25)
    for ax in axes:
        ax.axvspan(2011, 2013, color="#F2F4F5")
        ax.set_xticks([2011, 2013, 2015, 2017, 2019, 2021, 2023, 2025])
    axes[0].plot(all_dfo["year"], all_dfo["firms"] / 1000, color=GREY, label="Все юрлица ДФО")
    axes[0].plot(clean["year"], clean["firms"] / 1000, color=BLUE, lw=2, label="Основной срез")
    axes[0].plot(clean["year"], clean["revenue_observed"] / 1000, color=TEAL, lw=2, label="В нём наблюдается выручка")
    decorate(axes[0], "Число организаций, тыс.")
    axes[0].set_ylim(0, 270)
    axes[0].legend(loc="upper right")
    axes[1].plot(clean["year"], clean["observed_pct"], color=TEAL, lw=2, marker="o", ms=3,
                 label="С выручкой / юрлица основного среза")
    axes[1].plot(clean["year"], clean["imputed_revenue_pct"], color=ORANGE, lw=2, marker="o", ms=3,
                 label="Восстановленная / суммарная выручка")
    decorate(axes[1], "Покрытие и восстановление, %")
    axes[1].set_ylim(0, 115)
    axes[1].legend(loc="upper right")
    axes[1].annotate("2011: вся выручка\nвосстановлена", (2011, 100), xytext=(2013, 82), fontsize=10,
                     arrowprops={"arrowstyle": "-", "color": ORANGE})
    save(fig, output, "01_coverage", "RFSD в ДФО: охват меняется во времени",
         f"Основной срез: коммерческие нефинансовые организации, outlier=0; {manual_note}.\n"
         "Выручка включает наблюдаемые нули. 2011–2013 выделены: низкое покрытие; рост числа наблюдений не равен росту бизнеса.")

    fig, axes = plt.subplots(1, 2, figsize=(13, 6.6))
    fig.subplots_adjust(left=.075, right=.97, top=.81, bottom=.23, wspace=.25)
    for sample, color, label, style in [("base", GREY, "До дополнительной очистки", "--"),
                                        ("clean", BLUE, "Основной срез", "-")]:
        df = macro.filter(pl.col("sample") == sample).sort("year")
        axes[0].plot(df["year"], df["rfsd_bln"] / 1000, style, color=color, lw=2, label=label)
    df = macro.filter((pl.col("sample") == "clean") & pl.col("official_bln").is_not_null()).sort("year")
    axes[0].plot(df["year"], df["official_bln"] / 1000, color=ORANGE, lw=2, label="ВРП Росстата")
    axes[0].set_xticks([2011, 2013, 2015, 2017, 2019, 2021, 2023, 2025])
    axes[0].set_ylim(0, 16)
    decorate(axes[0], "Номинальные уровни, трлн руб.")
    axes[0].legend(loc="upper left")
    macro_growth = growth.filter((pl.col("dimension") == "total") & (pl.col("metric") == "revenue_bln")
                       & (pl.col("sample") == "clean") & (pl.col("year") >= 2017)).sort("year")
    axes[1].plot(macro_growth["year"], macro_growth["rfsd_growth"] * 100, color=BLUE, marker="o", ms=4, label="Выручка: основной срез")
    official = macro_growth.filter(pl.col("official_growth").is_not_null())
    axes[1].plot(official["year"], official["official_growth"] * 100, color=ORANGE, marker="o", ms=4, label="ВРП")
    pair = panel.filter((pl.col("dimension") == "total") & (pl.col("sample") == "clean")
                        & (pl.col("year") >= 2017)).sort("year")
    axes[1].plot(pair["year"], pair["rfsd_growth"] * 100, "--", color=TEAL, label="Выручка: сопоставимые фирмы")
    axes[1].axhline(0, color=GREY, lw=.8)
    axes[1].set_xticks(list(range(2017, 2026)), labels=[str(y)[2:] for y in range(2017, 2026)])
    axes[1].set_ylim(-6, 43)
    decorate(axes[1], "Номинальный рост, % к предыдущему году", "Год: 2017–2025")
    axes[1].legend(loc="upper left")
    save(fig, output, "02_macro", "Выручка RFSD и ВРП: близость сумм не означает сопоставимость",
         "ДФО в постоянных границах 11 регионов. Выручка и ВРП — разные экономические показатели.\n"
         "ВРП за 2025 год в источнике отсутствует. Панель: одинаковые фирмы с выручкой и неизменным регионом в каждой паре лет.")

    sectors = levels.filter((pl.col("dimension") == "section") & (pl.col("metric") == "revenue_bln")
                            & (pl.col("sample") == "clean") & (pl.col("year") == 2024)
                            & pl.col("group").is_in(list(SECTORS))).sort("group")
    if sectors.height != 19:
        raise ValueError("Expected 19 Rosstat sections")
    fig, ax = plt.subplots(figsize=(13, 10.8))
    fig.subplots_adjust(left=.34, right=.94, top=.86, bottom=.14)
    for i, row in enumerate(sectors.iter_rows(named=True)):
        for offset, field, color in [(-.18, "rfsd_bln", BLUE), (.18, "official_bln", ORANGE)]:
            value = row[field]
            if value is not None:
                ax.barh(i + offset, value, height=.30, color=color)
                ax.text(value + 35, i + offset, number(value, 1), va="center", fontsize=9)
            else:
                ax.text(35, i + offset, "вне выборки", va="center", fontsize=9, color=GREY)
    ax.set_yticks(range(19), [f"{s}  {SECTORS[s]}" for s in sectors["group"]], fontsize=10)
    ax.invert_yaxis()
    ax.set_xlim(0, 5100)
    ax.set_xlabel("Млрд руб., текущие цены")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: number(value)))
    ax.set_axisbelow(True)
    ax.grid(axis="x", color="#E8EDF0")
    ax.plot([], [], color=BLUE, lw=6, label="Выручка RFSD: основной срез")
    ax.plot([], [], color=ORANGE, lw=6, label="ВДС Росстата")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.02), ncol=2)
    housing_2024 = housing.filter((pl.col("year") == 2024) & (pl.col("region_code") == "DFO"))["housing_bln"].item()
    save(fig, output, "03_sectors", "Отрасли ДФО, 2024: различия скрываются за общим итогом",
         "Разделы ОКВЭД2 A–S; не рабочие группы аудиторов. Банки, бюджетный сектор и жилищные услуги входят в ВДС.\n"
         f"K исключён из RFSD по определению выборки. В ВДС раздела L учтено {number(housing_2024, 1)} млрд руб. жилищных услуг.\n"
         "Выручка / ВДС не является долей покрытия. Раздел T вне рисунка, но сохранён в общем итоге RFSD.")

    cluster = groups.filter(pl.col("dimension") == "cluster")
    before = cluster.filter(pl.col("sample") == "base").select("year", "group", pl.col("revenue_bln").alias("base_bln"))
    after = cluster.filter(pl.col("sample") == "clean").select(
        "year", "group", pl.col("revenue_bln").alias("clean_bln"), "top5_revenue_share", "top10_revenue_share")
    effects = after.join(before, on=["year", "group"], validate="1:1").with_columns(
        pl.when(pl.col("base_bln") > 0).then(100 * (pl.col("base_bln") - pl.col("clean_bln")) / pl.col("base_bln"))
        .alias("removed_pct"))
    fig, axes = plt.subplots(1, 2, figsize=(13, 7.8), sharey=True)
    fig.subplots_adjust(left=.27, right=.92, top=.78, bottom=.22, wspace=.32)
    labels = [s.replace("Строительство, энергетика и коммунальные виды", "Строительство, энергетика\nи коммунальные виды") for s in CLUSTERS]
    for i, name in enumerate(CLUSTERS):
        current = effects.filter((pl.col("year") == 2024) & (pl.col("group") == name)).row(0, named=True)
        history = effects.filter(pl.col("group") == name).sort(["removed_pct", "year"], descending=[True, False])
        peak = history.row(0, named=True)
        five, ten = current["top5_revenue_share"] * 100, current["top10_revenue_share"] * 100
        axes[0].barh(i, ten, color="#B9D5E4", height=.48)
        axes[0].barh(i, five, color=BLUE, height=.48)
        axes[0].text(ten + 1, i, number(ten, 1), va="center", fontsize=10)
        value, maximum = current["removed_pct"], peak["removed_pct"]
        axes[1].plot([value, maximum], [i, i], color="#CCD4DA", lw=2)
        axes[1].scatter(maximum, i, marker="D", color=ORANGE, s=35, zorder=3)
        axes[1].scatter(value, i, marker="o", color=BLUE, s=35, zorder=4)
        label = f"{number(maximum, 1)} · {peak['year']}" if maximum > 1e-8 else "0 во все годы"
        axes[1].text(maximum + 1, i - .17, label, va="center", fontsize=9, color=ORANGE if maximum > 1e-8 else GREY)
        if value > 1e-8:
            axes[1].text(value + 1, i + .20, f"{number(value, 1)} · 2024", va="center", fontsize=9, color=BLUE)
    axes[0].set_yticks(range(len(CLUSTERS)), labels, fontsize=10)
    axes[0].invert_yaxis()
    axes[0].set_xlim(0, 57)
    axes[1].set_xlim(-2, 57)
    for ax in axes:
        ax.set_axisbelow(True)
        ax.grid(axis="x", color="#E8EDF0")
    axes[0].set_title("Концентрация, 2024", loc="left", pad=43)
    axes[0].set_xlabel("Доля в положительной выручке, %")
    axes[0].legend(handles=[Patch(color=BLUE, label="Топ-5"), Patch(color="#B9D5E4", label="Топ-10")],
                   loc="lower left", bbox_to_anchor=(0, 1.00), ncol=2)
    axes[1].set_title("Исключённая выручка", loc="left", pad=43)
    axes[1].set_xlabel("Снижение относительно базовой выборки, %")
    axes[1].scatter([], [], color=BLUE, label="2024")
    axes[1].scatter([], [], color=ORANGE, marker="D", label="Максимум 2011–2025")
    axes[1].legend(loc="lower left", bbox_to_anchor=(0, 1.00), ncol=1)
    save(fig, output, "04_concentration_cleaning", "Очистка: небольшой общий эффект может скрывать отраслевой",
         f"Слева — основной срез. Справа — эффект {cleaning} внутри коммерческой нефинансовой выборки.\n"
         f"Исключение НКО и финансовых организаций сюда не входит. Непомеченные аномалии остаются; {manual_note}.")

    for name, table in [("coverage", coverage), ("coverage_plot", clean), ("macro_levels", macro),
                        ("macro_growth", macro_growth), ("macro_panel", pair), ("sectors_2024", sectors),
                        ("cleaning_effects", effects), ("housing", housing)]:
        table.write_parquet(output / f"{name}.parquet")
        table.write_csv(output / f"{name}.csv")
    finish_run(output, "batch5-figures", {
        "audit_run": args.audit_run.as_posix(), "comparison_run": args.comparison_run.as_posix(),
        "input_manifest_sha256": {str(p): file_hash(p / "manifest.json") for p in [args.audit_run, args.comparison_run]},
        "manual_exclusions": audit_run["manual_exclusions"], "matplotlib": matplotlib.__version__,
        "figures": ["01_coverage", "02_macro", "03_sectors", "04_concentration_cleaning"],
    })
    print(f"Saved and indexed four PNG/SVG figures and their source tables: {output}")


if __name__ == "__main__":
    main()
