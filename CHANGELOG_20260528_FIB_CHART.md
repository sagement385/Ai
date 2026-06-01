# 2026-05-28 Update: Fibonacci/Angle + Chart Report

## Added
- `features/fibonacci_angle_features.py`
- `scoring/fibonacci_angle_score.py`
- Final ranking priority updated:
  1. Sideways base
  2. Support / resistance
  3. Institution / foreign supply
  4. Quiet accumulation
  5. Volatility contraction
  6. Fibonacci / diagonal trendline supplement
- HTML report now draws inline SVG charts from `prices_daily.csv`; no external chart API required.

## Changed
- `scoring/final_ranker.py` now includes `volatility_bonus`, `fibonacci_angle_score`, `fibonacci_angle_bonus`, and `priority_summary`.
- `reports/daily_signal_report.py` now creates card-style readable reports with recent price and volume mini charts.
- Local web UI in `main.py` improved for readability and provides links to report/CSV/JSON.

## Design rule
Fibonacci and diagonal trendlines are not main signals. They are only supplementary confirmation and cannot overcome weak sideways/support/supply conditions.
