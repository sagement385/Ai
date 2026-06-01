# 2026-05-31 UI / Backtest / Terminal Update

- UI 전략 체크박스 추가
- 선택 전략 분석/백테스트 추가
- 기본 `python main.py analyze`를 4전략 빠른 분석으로 변경
- Bloomberg식 `/terminal` 페이지 추가
- `/stock?code=005930` 종목 상세 페이지 추가
- 빠른 현재 전략 스캐너 `strategies/fast_current_portfolio.py` 추가
- 백테스트 최근 3년 평가구간 기본 적용
- 백테스트 상위 500개 유동성 필터 기본 적용
- 대형 CSV 성능 최적화
  - 주봉 생성 vectorized groupby
  - 백테스트 지표 중복 계산 제거
  - groupby 반복/대형 DataFrame 복사 최소화
