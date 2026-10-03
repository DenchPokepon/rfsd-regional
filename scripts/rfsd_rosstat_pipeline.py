"""RFSD vs Rosstat: nominal levels, growth and conditional deflation diagnostics."""

import argparse
from pathlib import Path

import polars as pl

from firm_level import dfo_filter, region_code, scan_rfsd
from firm_level.analysis import (
    BASE, CLEAN, SECTION, SUMMARY, REVENUE, read_exclusions,
)
from firm_level.dataset import DATA_DIR
from firm_level.diagnostics import adjacent_panel
from firm_level.comparison import add_growth, add_deflated_growth, correlations
from firm_level.rosstat import read_grp, read_gva, read_comparison_details
from firm_level.sources import SOURCES, verify_file
from firm_level.results import create_run, finish_run

METRICS = ['revenue_bln', 'gross_profit_full_bln', 'simplified_result_bln']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=DATA_DIR)
    parser.add_argument('--output-dir', type=Path, help='New directory; existing runs are never overwritten')
    parser.add_argument('--decisions', type=Path)
    args = parser.parse_args()
    output = create_run('comparison', args.output_dir)
    for name in ['VRP_s1998.xlsx', 'VRP_OKVED2_s_2016.xlsx']:
        verify_file(args.data_dir / name, SOURCES['rosstat'][name]['sha256'])
    grp = read_grp(args.data_dir / 'VRP_s1998.xlsx')
    gva = read_gva(args.data_dir / 'VRP_OKVED2_s_2016.xlsx')
    details = read_comparison_details(args.data_dir / 'VRP_s1998.xlsx', args.data_dir / 'VRP_OKVED2_s_2016.xlsx')
    grp_total = grp.group_by('year').agg(pl.col('grp_bln').sum())
    regional_gva = details['regional_gva'].filter(pl.col('region_code') != 'DFO')
    controls = regional_gva.group_by('year', 'region_code').agg(pl.col('gva_bln').sum()).join(
        grp, on=['year', 'region_code'], validate='1:1'
    ).with_columns((pl.col('gva_bln') - pl.col('grp_bln')).alias('difference_bln'))
    sector_controls = regional_gva.group_by('year', 'section').agg(
        pl.col('gva_bln').sum().alias('sum_regions_bln')
    ).join(gva, on=['year', 'section'], validate='1:1').with_columns(
        (pl.col('sum_regions_bln') - pl.col('gva_bln')).alias('difference_bln')
    )
    if any(table.filter(pl.col('difference_bln').abs() > .001).height for table in [controls, sector_controls]):
        raise ValueError('Rosstat regional/sector totals disagree')
    exclusions = read_exclusions(args.decisions).collect()
    aggregates, panels = [], []
    previous = None
    for year in range(2011, 2026):
        for path in sorted((args.data_dir / 'RFSD' / f'year={year}').glob('*.parquet')):
            key = f'RFSD/year={year}/{path.name}'
            verify_file(path, SOURCES['rfsd']['files'][key]['sha256'])
        lf = scan_rfsd(year, data_dir=args.data_dir / 'RFSD').filter(dfo_filter() & BASE).with_columns(
            SECTION.alias('section'), region_code().alias('region_code'), REVENUE.alias('revenue'),
        )
        needed = {'inn', 'year', 'section', 'region_code', 'revenue', 'outlier', 'imputed'}
        for expr in SUMMARY:
            needed.update(expr.meta.root_names())
        df = lf.select(sorted(needed)).collect()
        if df['inn'].null_count() or df['inn'].n_unique() != df.height:
            raise ValueError(f'Invalid DFO keys in {year}; run check_data.py')
        samples = {'base': df, 'clean': df.filter(CLEAN).join(exclusions, on=['inn', 'year'], how='anti')}
        for sample, frame in samples.items():
            for dimension in ['total', 'section', 'region_code']:
                aggregated = frame.with_columns(pl.lit('DFO').alias('total')).group_by('year', dimension).agg(SUMMARY)
                aggregates.append(aggregated.rename({dimension: 'group'}).with_columns(
                    pl.lit(dimension).alias('dimension'), pl.lit(sample).alias('sample')))
                if previous is not None and dimension in ['total', 'section']:
                    panels.append(adjacent_panel(previous[sample], frame, dimension).with_columns(
                        pl.lit(sample).alias('sample'), pl.lit(year).alias('year')))
        previous = samples
        print(f'Aggregated {year}', flush=True)
    aggregates = pl.concat(aggregates, how='vertical_relaxed')
    official = pl.concat([
        grp_total.rename({'grp_bln': 'official_bln'}).with_columns(pl.lit('total').alias('dimension'), pl.lit('DFO').alias('group')),
        gva.rename({'gva_bln': 'official_bln', 'section': 'group'}).with_columns(pl.lit('section').alias('dimension')),
        grp.rename({'grp_bln': 'official_bln', 'region_code': 'group'}).with_columns(pl.lit('region_code').alias('dimension')),
    ], how='diagonal_relaxed').select('year', 'dimension', 'group', 'official_bln')
    official = pl.concat([official.with_columns(pl.lit(s).alias('sample')) for s in ['base','clean']])
    levels = aggregates.join(official, on=['year','dimension','group','sample'], how='full', coalesce=True, validate='1:1')
    long_levels = levels.unpivot(METRICS, index=['year','dimension','group','sample','official_bln'],
                                variable_name='metric', value_name='rfsd_bln').with_columns(
        pl.when(pl.col('official_bln') > 0).then(pl.col('rfsd_bln') / pl.col('official_bln')).alias('rfsd_official_ratio'))
    growth = add_growth(long_levels, details['volume_indices'])
    panel = pl.concat(panels, how='vertical_relaxed').rename({'panel_growth':'rfsd_growth'})
    official_growth = growth.filter(pl.col('metric')=='revenue_bln').select(
        'year','sample','dimension','group','official_growth','volume_factor')
    panel = panel.join(official_growth,on=['year','sample','dimension','group'],how='left',validate='1:1')
    panel = add_deflated_growth(panel).with_columns(pl.lit('revenue_panel').alias('metric'))
    corr = correlations(pl.concat([growth,panel],how='diagonal_relaxed'))
    corr_full = corr.filter(pl.col('scenario')=='all')
    loo = corr.filter(pl.col('scenario').str.contains(r'^without_\d{4}$')).group_by(
        'sample','dimension','group','metric','mode'
    ).agg(pl.col('pearson_r').min().alias('leave_one_out_min'),pl.col('pearson_r').max().alias('leave_one_out_max'))
    corr_summary = corr_full.join(loo,on=['sample','dimension','group','metric','mode'],how='left',validate='1:1')
    tables = {'levels':levels,'levels_long':long_levels,'growth':growth,'adjacent_growth':panel,
              'correlations':corr,'correlation_summary':corr_summary,'regional_reconciliation':controls,
              'sector_reconciliation':sector_controls,**details}
    for name,table in tables.items():
        table=table.sort([c for c in ['sample','dimension','group','metric','mode','scenario','year','region_code','section'] if c in table.columns])
        table.write_csv(output/f'{name}.csv')
        table.write_parquet(output/f'{name}.parquet')
    finish_run(output,'batch4-comparison',{'hashes_verified':True,'years':list(range(2011,2026)),
               'correlation_years':[2017,2024],'manual_exclusions':exclusions.to_dicts(),
               'deflation':'conditional: implicit Rosstat GRP/GVA deflator, not independent validation'})
    print(f'Wrote and indexed {output}')


if __name__=='__main__':
    main()
