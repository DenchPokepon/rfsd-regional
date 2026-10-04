# RFSD: данные и код для исследования экономики ДФО

Мы используем отчётность компаний для анализа отраслей и региональной динамики. Здесь собраны локальная база, простые Polars-запросы и первые расчёты.

[С чего предлагаем начать](docs/audit_task.md) · [Сравнение с ВРП](docs/rfsd_vs_rosstat_brief.md) · [Excel для разбора компаний](docs/audit_packet.md) · [Открытые вопросы](docs/plan.md)

## Установка и данные

Из корня репозитория, с установленным [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv python install 3.11
uv sync --locked --all-groups
uv run --locked --group download python scripts/download_data.py --source all
uv run --locked python -i explore.py
```

Если Python 3.11 уже установлен, первую команду можно пропустить. При проблемах с обнаружением версии можно указать интерпретатор явно: `uv sync --locked --all-groups --python "путь/к/python.exe"`.

RFSD занимает около 7,7 ГБ. Загрузчик использует фиксированную ревизию и проверяет SHA-256. Проверка имеющихся файлов без сети:

```sh
uv run --locked python scripts/download_data.py --source all --verify-only
```

Данные лежат в `data/`, результаты — в `outputs/runs/`; оба каталога вне Git. Другой каталог данных можно задать через `RFSD_DATA_DIR`: он должен содержать `RFSD/` и файлы Росстата. Аргумент `scan_rfsd(data_dir=...)` принимает сам каталог с партициями `year=...`.

Для одних запросов достаточно Polars: `uv sync --locked --no-default-groups`. Группы `download`, `analysis`, `plots` и `dev` добавляют соответственно загрузку из Hugging Face, чтение Excel, Matplotlib и тесты.

## Запросы

В `explore.py` доступны `pl`, `lf`, `dfo`, `base`, `clean`. `lf` содержит полную RFSD; `base` — коммерческие нефинансовые организации ДФО; `clean` дополнительно оставляет только `outlier=0`. Ручные решения автоматически не применяются. Сам вход в сессию тяжёлых запросов не выполняет.

`scan_rfsd()` возвращает обычный LazyFrame без скрытых фильтров. Можно использовать и `pl.scan_parquet()` напрямую.

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

Суммы RFSD — тыс. руб.; деление на `1e6` даёт млрд руб. ИНН и коды храним строками. Для импорта CSV в Excel нужен текстовый тип этих колонок. Определения выборки, пропусков и показателей прибыли — в [методическом приложении](docs/analysis_rules.md).

## Готовые расчёты

| Скрипт | Что получаем |
|---|---|
| `check_data.py` | Покрытие, влияние отбора, аномалии, формы отчётности и динамика сопоставимых фирм |
| `dfo_analysis.py` | Отраслевые и региональные суммы за выбранный год, крупнейшие компании |
| `rfsd_rosstat_pipeline.py` | Уровни, темпы и корреляции с ВРП и отраслевой ВДС |
| `plot_results.py` | Четыре общих рисунка из сохранённых расчётов |
| `prepare_audit.py` | Списки компаний, история, отраслевые таблицы и шаблоны замечаний |

Например, для быстрой проверки 2024 года:

```sh
uv run --locked python scripts/check_data.py --years 2024
uv run --locked python scripts/dfo_analysis.py --year 2024 --top-sectors 15 --top-firms 10
```

Каждый запуск сохраняется в новый каталог и попадает в `outputs/index.json`. `--output-dir` позволяет задать имя каталога; существующие результаты не перезаписываются. Подтверждённые решения можно передать через `--decisions data/decisions.csv` с полями `inn,year,action,reason,source`. `review` сохраняет запись для разбора, `exclude` исключает ИНН–год из очищенного варианта.

## Полный повтор в новом клоне

После установки окружения и загрузки данных:

```sh
uv run --locked python scripts/download_data.py --source all --verify-only
uv run --locked python scripts/check_data.py --output-dir outputs/reproduce/audit
uv run --locked python scripts/dfo_analysis.py --year 2024 --top-sectors 15 --top-firms 10 --output-dir outputs/reproduce/dfo
uv run --locked --group analysis python scripts/rfsd_rosstat_pipeline.py --output-dir outputs/reproduce/comparison
uv run --locked --group plots python scripts/plot_results.py --audit-run outputs/reproduce/audit --comparison-run outputs/reproduce/comparison --output-dir outputs/reproduce/figures
uv run --locked python scripts/prepare_audit.py --audit-run outputs/reproduce/audit --figures-run outputs/reproduce/figures --output-dir outputs/reproduce/packet
```

Для следующего повтора меняем префикс `outputs/reproduce/` во всех командах. Можно использовать проверенную локальную копию источников через `RFSD_DATA_DIR`, без повторного скачивания. При ручных исключениях проверку RFSD и сравнение с Росстатом выполняем с одним файлом `--decisions`, затем строим графики.

Python-команды создают CSV/Parquet, а генератор графиков — PNG/SVG. Готовые XLSX экспортированы отдельно и доступны в [материалах для коллег](docs/audit_packet.md). Для работы с ними весь набор расчётов повторять не нужно.

Числа в записках и ссылки на графики относятся к сохранённым результатам. Новый запуск не переписывает документы автоматически. Текущие материалы перечислены в [индексе](docs/results.md).

## Проверки и версии

```sh
uv run --locked pytest -q -m "not integration"
uv run --locked pytest -q -m integration
```

Первый набор работает на небольших искусственных данных, второй проверяет локальные источники и контрольные значения; без источников он пропускается. Если Windows блокирует системный temp-каталог, можно добавить `--basetemp .pytest-temp`. Pytest очищает этот служебный каталог при запуске.

Python 3.11 указан в `.python-version`, зависимости — в `uv.lock`. Ревизия RFSD и SHA-256 исходников сохранены в [sources.json](src/firm_level/sources.json). Файлы Росстата могут меняться по прежним URL; при несовпадении хеша загрузчик останавливается. Для точного повтора используем сохранённые копии.

[Словарь названий](src/firm_level/descriptive_names.csv) адаптирован из [словаря RFSD](https://github.com/irlcode/RFSD/blob/main/aux_data/descriptive_names_dict.csv). Результаты нашего повтора расчётов в отдельном окружении описаны [здесь](docs/handoff.md#воспроизводимость).
