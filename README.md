# RFSD: локальная аналитика на Polars

Python 3.11, uv, Parquet. `scan_rfsd()` возвращает обычный LazyFrame без скрытых фильтров; можно также использовать `pl.scan_parquet()` напрямую.

[Задание коллегам](docs/audit_task.md) · [Краткий анализ RFSD–ВРП](docs/rfsd_vs_rosstat_brief.md) · [План и статусы](docs/plan.md) · [Правила расчётов](docs/analysis_rules.md) · [Индекс результатов](docs/results.md)

## Установка и данные

Из корня клонированного репозитория, с установленным [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv python install 3.11
uv sync --locked --all-groups
uv run --locked --group download python scripts/download_data.py --source all
uv run --locked python -i explore.py
```

Если Python 3.11 уже установлен, первую команду можно пропустить. При сбое установки или обнаружения версии укажите рабочий интерпретатор явно: `uv sync --locked --all-groups --python "путь/к/python.exe"`.

RFSD занимает около 7,7 ГБ. Загрузчик скачивает отсутствующие файлы, проверяет SHA-256 и использует фиксированную ревизию. Имеющиеся файлы другой версии не перезаписываются молча. Проверка без сети:

```sh
uv run --locked python scripts/download_data.py --source all --verify-only
```

Исходники лежат в `data/`, результаты — в `outputs/runs/`; оба каталога исключены из Git. Каждый успешный аналитический запуск сохраняется отдельно и регистрируется в `outputs/index.json`: параметры, версии источников и SHA-256 файлов. Другой каталог исходников можно задать переменной `RFSD_DATA_DIR` (содержит `RFSD/` и файлы Росстата). Для `scan_rfsd(data_dir=...)` передаётся именно каталог с партициями `year=...`.

Основная зависимость — только Polars. Группа `download` добавляет Hugging Face, `analysis` — чтение Excel, `plots` — Matplotlib, `dev` — тесты. Для одних запросов достаточно `uv sync --locked --no-default-groups`.

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

- `check_data.py`: проверка SHA-256 и ключей, покрытие, влияние очистки, формы, крупные аномалии и динамика на сопоставимых фирмах. Без `--years` проверяет все скачанные годы; несоседние годы не соединяет. Состав таблиц — в [записке](docs/data_audit.md). `--top 20` задаёт число кандидатов на причину и год; `--trace-inn 2526003992` добавляет историю указанного ИНН. Кандидаты сами по себе не меняют выборку.
- `dfo_analysis.py`: агрегаты по классам ОКВЭД, регионам и рабочим группам, крупнейшие фирмы; базовый и очищенный варианты.
- `rfsd_rosstat_pipeline.py`: уровни, темпы и корреляции с ВРП и ВДС, сверка региональных/отраслевых сумм, условное дефлирование. Требует RFSD 2011–2025; расчёт из исходников, без кеша. Несопоставимые отрасли и отсутствующие годы остаются с пропусками. Отношение выручки к ВДС не является процентом покрытия. [Выводы и ограничения](docs/rfsd_vs_rosstat.md).

Три скрипта сохраняют CSV и Parquet в новый каталог `outputs/runs/<время UTC>-<анализ>/`. `--output-dir` задаёт свой новый каталог; существующий каталог не перезаписывается. Все успешные запуски индексируются автоматически. Необязательный `--decisions data/decisions.csv` читает решения `inn,year,action,reason,source`: `review` не меняет суммы, `exclude` исключает ИНН только за указанный год из очищенного варианта. Без файла нет ручных исключений. Перед анализом новых файлов запускайте проверку контрольных сумм и `check_data.py`.

## Задание коллегам

[Короткая инструкция](docs/audit_task.md) · [Распределение, Excel и охват](docs/audit_packet.md).

```sh
uv run --locked python scripts/prepare_audit.py --audit-run outputs/reproduce/audit --figures-run outputs/reproduce/figures --output-dir outputs/reproduce/packet
```

Сначала выполните расчёты и графики по последовательности ниже. Скрипт на Polars воспроизводит списки и шаблоны ответа CSV, историю ИНН, отраслевые таблицы, копии задания и графиков. Отдельные Excel для пяти коллег лежат в сохранённом комплекте; Python-команда создаёт CSV/Parquet. Ответы коллег сохраняйте отдельно от индексированных исходных выгрузок.

## Общие графики

После проверки и сравнения укажите каталоги сохранённых запусков:

```sh
uv run --locked --group plots python scripts/plot_results.py --audit-run outputs/reproduce/audit --comparison-run outputs/reproduce/comparison --output-dir outputs/reproduce/figures
```

Четыре PNG/SVG и исходные таблицы сохраняются в новый индексируемый каталог. Входные хеши, ревизия RFSD, ручные исключения и общие суммы должны совпадать. [Графики и пояснения](docs/figures.md). При ручной очистке сначала пересчитайте оба входных запуска с одним `--decisions`.

## Полный повтор в новом клоне

После установки и загрузки данных выполните команды по порядку. Каталоги `outputs/reproduce/` ниже должны быть новыми; для следующего повтора замените этот префикс во всех командах. Старые результаты не удаляются и не перезаписываются.

```sh
uv run --locked python scripts/download_data.py --source all --verify-only
uv run --locked python scripts/check_data.py --output-dir outputs/reproduce/audit
uv run --locked python scripts/dfo_analysis.py --year 2024 --top-sectors 15 --top-firms 10 --output-dir outputs/reproduce/dfo
uv run --locked --group analysis python scripts/rfsd_rosstat_pipeline.py --output-dir outputs/reproduce/comparison
uv run --locked --group plots python scripts/plot_results.py --audit-run outputs/reproduce/audit --comparison-run outputs/reproduce/comparison --output-dir outputs/reproduce/figures
uv run --locked python scripts/prepare_audit.py --audit-run outputs/reproduce/audit --figures-run outputs/reproduce/figures --output-dir outputs/reproduce/packet
```

Можно использовать проверенную локальную копию источников вместо повторного скачивания: перед запуском задайте `RFSD_DATA_DIR` с путём к каталогу данных. Сохранённые старые запуски для расчёта не требуются. В репозитории Markdown ссылается на исторические результаты: изображения и Excel нужно передать отдельно либо открыть новые файлы в `outputs/reproduce/`. Текст записок не обновляется автоматически при изменении данных или ручных решений.

## Проверки

```sh
uv run --locked pytest -q -m "not integration"
uv run --locked pytest -q -m integration
```

Первый набор использует небольшие искусственные данные и не требует скачивания RFSD. Второй проверяет локальные источники и контрольные значения агрегатов; без данных пропускается. Если Windows блокирует системный temp-каталог, добавьте `--basetemp .pytest-temp`: это служебный каталог в корне проекта, который pytest очищает при запуске.

В `.python-version` задана ветка Python 3.11; patch-версия не закреплена. Зависимости закреплены в `uv.lock`; ревизия RFSD, размеры и SHA-256 — в [sources.json](src/firm_level/sources.json). Росстат обновляет файлы по прежним URL: если хеш изменился, загрузчик остановится. Для точного повтора потребуется сохранённая копия либо отдельное согласованное обновление источников и проверок.

Словарь названий в `src/firm_level/descriptive_names.csv` — метаданные схемы, не отчётность компаний. Он перенесён из прежнего проекта и адаптирован из [словаря RFSD](https://github.com/irlcode/RFSD/blob/main/aux_data/descriptive_names_dict.csv).

Два основных документа — [задание коллегам](docs/audit_task.md) и [краткий анализ RFSD–ВРП](docs/rfsd_vs_rosstat_brief.md). Подробности: [диагностика](docs/data_audit.md), [техническое сравнение с Росстатом](docs/rfsd_vs_rosstat.md), [общие графики](docs/figures.md). Старые документы в `docs/drafts/` сохранены с предупреждением. Сквозной повтор проверен; [комплект передачи и протокол](docs/handoff.md). Следующий шаг — сверка первичной отчётности коллегами. Все комплекты — в [индексе](docs/results.md).

Источник: [RFSD — Bondarkov, Ledenev, Skougarevskiy](https://github.com/irlcode/RFSD), CC BY 4.0. Финансовая отчётность неконсолидированная; юридический адрес не определяет место производства.
