# 2026-05-31 Advanced Data + Backtest Update

- OpenDART 분기 재무 수집 추가: `download-dart --part/--parts/--years`
- 분기 재무 CSV 저장: `financial_quarterly.csv`, 최신 요약 `financials.csv`
- CANSLIM 점수 강화: 매출/영업이익/순이익/EPS YoY 반영
- 기관/외국인 수급 분할 수집 추가: `download-supply-part`
- 주봉 Weinstein 30주선 파생 데이터 생성: `prices_weekly.csv`
- 섹터 강도 계산: `sector_strength.csv`
- 실전형 4전략 백테스트 추가: 다음날 시가 진입, 손절/익절, 수수료/세금/슬리피지 반영
- UI 개선: DART 키 저장, 분할 수집, 차트 대시보드, 백테스트 리포트 링크 추가
