# 2026-05-31 Web Terminal v6

- 링크형 터미널을 인터랙티브 종목 선택 UI로 교체
- `/api/terminal`, `/api/stock`, `/api/backtest-audit` 추가
- 종목 선택 시 캔들 차트, 진입가, 손절가, 목표가, 피벗 즉시 표시
- 백테스트 거래내역을 종목 상세에 연결
- `/backtest-explorer` 추가
- `backtest_audit.csv` 저장
- 백테스트가 실제 과거 구간에서 진행됐는지 확인 가능한 연도별 거래/신호 통계 추가
- 분석 기본 유니버스 필터를 최근 거래대금 상위 500개로 조정해 웹 응답성 개선
