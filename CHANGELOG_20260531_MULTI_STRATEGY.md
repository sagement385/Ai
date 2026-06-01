# 2026-05-31 Multi Strategy Portfolio Update

- RL 없이 4개 독립 알고리즘 구현
  - Minervini VCP 변동성 수축
  - O'Neil CANSLIM 성장주 돌파 프록시
  - Weinstein Stage 2 상승단계
  - Darvas Box 박스 돌파
- `configs/multi_strategy_config.json` 추가
- `strategies/` 모듈 추가
- `python main.py analyze` 실행 시 멀티 전략 포트폴리오 파일 생성
  - `reports/output/multi_strategy_signals_all.csv`
  - `reports/output/multi_strategy_portfolio.csv`
  - `reports/output/multi_strategy_portfolio.html`
  - `reports/output/multi_strategy_portfolio.json`
- UI에 `4전략 포트폴리오` 링크 추가
- `universe.csv`/`prices_daily.csv`에서 `name` 컬럼도 `stock_name`으로 인식하도록 보정
