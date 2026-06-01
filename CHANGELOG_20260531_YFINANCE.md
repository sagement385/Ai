# CHANGELOG 2026-05-31 - yfinance 국내 전체 1년 CSV 다운로드 추가

## 추가

- `notebooks/download_krx_yfinance_1y.ipynb`
  - Jupyter Notebook에서 KOSPI/KOSDAQ 전체 종목 1년 일봉 CSV 생성
- `data_sources/yfinance_kr.py`
  - pykrx 유니버스 생성
  - yfinance 배치 다운로드
  - 엔진 표준 CSV 변환
- `docs/YFINANCE_KRX_1Y_DOWNLOAD.md`
  - 사용법 및 주의사항 문서화

## 변경

- `main.py download --source yfinance` 지원
- 로컬 웹 UI에 yfinance 다운로드 버튼 추가
- `requirements.txt`에 `yfinance` 추가

## 출력

- `data/csv_import/universe.csv`
- `data/csv_import/prices_daily.csv`
- `data/csv_import/yfinance_download_failures.csv`
