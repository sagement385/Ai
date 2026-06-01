# 2026-05-31 QuantKing 통합 변경사항

## 추가

- `data_sources/kiwoom_openapi.py`
  - Kiwoom OpenAPI+ COM 기반 일봉 가격 수집 `opt10081`
  - 투자자별 수급 수집 `opt10059`
  - 기존 `prices_daily.csv`, `supply_daily.csv` 형식으로 저장

- `data_sources/quantking_sqlite_importer.py`
  - QuantKing SQLite DB 테이블 인벤토리 생성
  - QuantKing DB → `universe.csv`, `prices_daily.csv` 변환
  - `configs/quantking_column_map.example.json`로 c컬럼 매핑 조정 가능

- `backtest/quantking_metrics.py`
  - MDD, CAGR, win rate, profit factor, 월별/연도별 수익률 테이블

- CLI 명령
  - `python main.py download --source kiwoom --days 250`
  - `python main.py inspect-quantking-db --db ...`
  - `python main.py import-quantking-db --db ...`

## 분석 결과 반영

QuantKingSetup은 소스코드가 아니라 Windows 설치 파일이었으므로 실행파일 코드를 복사하지 않았습니다. 내부 구성상 Kiwoom OpenAPI, SQLite, CSV, 백테스트 지표를 쓰는 것으로 보여 해당 기능을 독립 파이썬 모듈로 구현했습니다.
