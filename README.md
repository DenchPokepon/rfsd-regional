# RFSD: локальная аналитика на Polars

Python 3.11, uv, Parquet. `scan_rfsd()` возвращает обычный LazyFrame без скрытых фильтров; можно также использовать `pl.scan_parquet()` напрямую.

[План и статусы](docs/plan.md) · [Правила расчётов](docs/analysis_rules.md) · [Проверка данных](docs/data_audit.md)

## Установка и данные

Из корня клонированного репозитория, с установленным [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv python install 3.11
uv sync --locked --all-groups
uv run --locked --group download python scripts/download_data.py --source all
uv run --locked python -i explore.py
```

RFSD занимает около 7,7 ГБ. Загрузчик скачивает отсутствующие файлы, проверяет SHA-256 и использует фиксированную ревизию. Имеющиеся файлы другой версии не перезаписываются молча. Проверка без сети:

```sh
uv run --locked python scripts/download_data.py --source all --verify-only
```

Исходники лежат в `data/`, результаты — в `outputs/`; оба каталога исключены из Git. Другой каталог исходников можно задать переменной `RFSD_DATA_DIR` (содержит `RFSD/` и файлы Росстата). Для `scan_rfsd(data_dir=...)` передаётся именно каталог с партициями `year=...`.

Основная зависимость — только Polars. Группа `download` добавляет Hugging Face, `analysis` — чтение Excel, `dev` — тесты. Для одних запросов достаточно `uv sync --locked --no-default-groups`.

## Обычные запросы

В интерактивной сессии доступны `pl`, `lf`, `dfo`, `base`, `clean`. Запуск не выполняет тяжёлых запросов. `base` — коммерческие нефинансовые организации ДФО; `clean` дополнительно исключает `outlier=1` и неизвестный флаг. Ручные решения автоматически не применяются.

```python
from firm_level import scan_rfsd, dfo_filter
from firm_level.analysis import BASE, CLEAN, REVENUE, observed_sum
import polars as pl

# Сырые данные по всей России, включая непредставивших отчётность.
lf = scan_rfsd(year=2024)

# Фильтры видны и могут быть изменены в самом запросе.
sample = lf.filter(dfo_filter() & BASE & CLEAN)
sample.select(
    pl.len().alias("firms"),
    REVENUE.count().alias("revenue_observed"),
    (observed_sum(REVENUE) / 1e6).alias("revenue_bln"),
).collect()

# Сортируем исходные суммы, не округлённые значения.
sample.filter(REVENUE > 0).select("inn", "okved", "line_2110").sort(
    ["line_2110", "inn"], descending=[True, False]
).head(10).collect()

# Необязательные описательные названия: PL_revenue, B_assets и т. д.
named = scan_rfsd(year=2024, descriptive=True)
```

Суммы RFSD — тыс. руб.; деление на `1e6` даёт млрд руб. Пропуск не равен нулю. Валовая прибыль полной формы и результат упрощённой формы считаются отдельно. ИНН и коды — строки; при открытии CSV в Excel явно выбирайте текстовый тип, либо используйте Parquet.

## Базовые скрипты

```sh
uv run --locked python scripts/check_data.py --years 2024
uv run --locked python scripts/check_data.py
uv run --locked python scripts/dfo_analysis.py --year 2024 --top-sectors 15 --top-firms 10
uv run --locked --group analysis python scripts/rfsd_rosstat_pipeline.py
```

- `check_data.py`: проверка SHA-256 и ключей, покрытие, влияние очистки, формы, крупные аномалии и динамика на сопоставимых фирмах. Без `--years` проверяет все скачанные годы; несоседние годы не соединяет. Результаты в `outputs/checks/` (CSV и Parquet), состав файлов — в [записке](docs/data_audit.md). `--top 20` задаёт число кандидатов на причину и год; `--trace-inn 2526003992` добавляет историю указанного ИНН. Кандидаты сами по себе не меняют выборку.
- `dfo_analysis.py`: агрегаты по классам ОКВЭД, регионам и рабочим группам, крупнейшие фирмы. Выгружает базовый и очищенный варианты в `outputs/dfo/`.
- `rfsd_rosstat_pipeline.py`: номинальные таблицы сравнения с ВРП и ВДС, без кеша и генерации выводов. Требует все годы RFSD 2011–2025, пишет в `outputs/comparison/`. Полное ВРП сохраняется, несопоставимые отрасли и отсутствующие годы остаются с пропусками. Отношение выручки к ВДС не является процентом покрытия.

У всех трёх скриптов есть `--output-dir` и необязательный `--decisions data/decisions.csv`. Формат решений: `inn,year,action,reason,source`; `review` не меняет суммы, `exclude` исключает ИНН только за указанный год из очищенного варианта. Без файла нет ручных исключений. Перед анализом новых файлов запускайте проверку контрольных сумм и `check_data.py`.

## Проверки и воспроизводимость

```sh
uv run --locked pytest -q -m "not integration"
uv run --locked pytest -q -m integration
```

Первый набор использует небольшие искусственные данные и не требует скачивания RFSD. Второй проверяет локальные источники и контрольные суммы агрегатов; без данных пропускается. Если Windows блокирует системный temp-каталог, добавьте `--basetemp outputs/pytest-temp`: это служебный каталог, который pytest очищает при запуске.

Версии Python и зависимостей закреплены `.python-version` и `uv.lock`; ревизия RFSD, размеры и SHA-256 — в [sources.json](src/firm_level/sources.json). Росстат обновляет файлы по прежним URL: если хеш изменился, загрузчик остановится. Для точного повтора потребуется сохранённая копия либо отдельное согласованное обновление источников и проверок.

Словарь названий в `src/firm_level/descriptive_names.csv` — метаданные схемы, не отчётность компаний. Он перенесён из прежнего проекта и адаптирован из [словаря RFSD](https://github.com/irlcode/RFSD/blob/main/aux_data/descriptive_names_dict.csv).

[Задание](docs/drafts/audit_task.md) и [аналитическая записка](docs/drafts/rfsd_vs_rosstat_report.md) пока сохранены как архивные черновики с предупреждением. Автоматическая [диагностика RFSD](docs/data_audit.md) завершена; сверка кандидатов с первичной отчётностью, обновление двух документов и графики — следующие шаги плана. Прежние файлы непосредственно в `outputs/` не являются результатами новых скриптов.

Источник: [RFSD — Bondarkov, Ledenev, Skougarevskiy](https://github.com/irlcode/RFSD), CC BY 4.0. Финансовая отчётность неконсолидированная; юридический адрес не определяет место производства.
