# v10 Capital Injection 10x

- 고위험 추가투입 10x 백테스트 프로필 추가
- `configs/backtest_config_capital_injection_10x.json` 추가
- 손절 대신 조건부 외부자금 투입 로직 추가
- 추가투입 안전장치 추가: falling knife, no-base, extreme distress, health score
- 백테스트 요약에 총투입원금 기준 수익률 추가
- `backtest_capital_injections.csv` 저장
- 웹 UI 백테스트 프로필에 `고위험 추가투입 10x` 추가
- 백테스트 검증 API에 추가투입 횟수/기록 추가
