# Stock Swing Signal Engine - AI Copilot

한국 주식 장기 스윙 후보를 찾기 위한 가격/수급/전략 기반 분석 엔진입니다. AI Copilot 기능을 통해 종목 분석, 백테스트 진단, 전략 개선안, 코드 패치 제안을 보조할 수 있습니다.

## 빠른 시작

```bash
pip install -r requirements.txt
python main.py serve
```

브라우저에서 `http://127.0.0.1:8765` 접속.

## GitHub 업로드 전 주의

- 실제 API 키가 들어간 `.env`는 업로드하지 마세요.
- `.env.example`만 업로드하세요.
- 자세한 업로드 방법은 `GITHUB_UPLOAD_GUIDE.md`를 참고하세요.

---

# stock_swing_signal_engine

테마/뉴스/RL 없이, 가격 구조와 수급 구조만으로 장기 스윙 후보를 찾는 CSV/API 보조 프로그램입니다.

이번 버전은 기존 피보나치/빗각 차트 버전에 **QuantKingSetup 분석 결과**를 반영했습니다.

- Kiwoom OpenAPI+ 가격/수급 수집 브리지 추가
- QuantKing SQLite DB → 엔진 CSV 변환기 추가
- QuantKing식 백테스트 성과지표(MDD, 월별/연도별 성과, profit factor) 추가
- `docs/QUANTKING_INTEGRATION.md`에 분석 내용 정리

## 핵심 판단 우선순위

1. **횡보장**: 박스권 폭, 이동평균 수렴, 박스권 지속성
2. **지지·저항**: 가격 클러스터, 체류 일수, 거래량 밀집, 지지선 근접도
3. **기관/외국인 수급**: 순매수 강도, 순매수 지속성, 거래대금 대비 유입
4. **조용한 매집**: 가격은 횡보하는데 거래량·OBV·수급이 개선되는 구조
5. **변동성 수축**: ATR, Bollinger Band width 수축
6. **피보나치/빗각 보조 확인**: 피보나치 되돌림 zone, 대각 추세선/수렴 구조

피보나치/빗각은 단독 추천 신호가 아니라, 1~5순위가 어느 정도 충족될 때만 소폭 보조 가산됩니다.

## 설치

```bash
pip install -r requirements.txt
```

Windows에서 Kiwoom OpenAPI를 쓸 경우:

```bash
pip install pywin32
```

## 1) 템플릿 생성

```bash
python main.py init-templates
```

## 2) CSV 자동 다운로드

API 없이 시작하려면:

```bash
python main.py download --source naver --days 250
```

KIS API 키를 `.env`에 넣은 뒤 가격 데이터를 받으려면:

```bash
python main.py download --source kis --days 250
```

Windows + Kiwoom OpenAPI+ 환경이면:

```bash
python main.py download --source kiwoom --days 250
```

수급/재무는 pykrx가 설치되어 있으면 자동으로 받습니다. API 키가 없거나 다운로드가 실패해도, 직접 CSV를 넣으면 분석할 수 있습니다.


### yfinance로 국내 전체 주식 1년 데이터 받기

주피터 노트북 방식으로 받으려면:

```bash
jupyter notebook notebooks/download_krx_yfinance_1y.ipynb
```

터미널에서 바로 받으려면:

```bash
python main.py download --source yfinance --days 250 --skip-supply --skip-financials
```

이 방식은 `pykrx`로 KOSPI/KOSDAQ 종목 목록을 만들고, yfinance에서 `.KS`, `.KQ` 티커로 최근 1년 일봉을 받아 `data/csv_import/prices_daily.csv`로 저장합니다. 자세한 내용은 `docs/YFINANCE_KRX_1Y_DOWNLOAD.md`를 참고하세요.

## 3) QuantKing DB를 CSV로 변환

업로드된 `QuantKingSetup.zip` 자체에는 데이터 DB가 없었습니다. QuantKing을 실행한 PC에서 `.db` 또는 `.sqlite` 파일을 찾은 뒤 사용하세요.

테이블/컬럼 확인:

```bash
python main.py inspect-quantking-db --db "C:/path/to/quantking.db"
```

엔진 CSV로 변환:

```bash
python main.py import-quantking-db --db "C:/path/to/quantking.db" --output-dir data/csv_import
```

컬럼 매핑을 바꿔야 하면 `configs/quantking_column_map.example.json`을 복사해서 수정한 뒤:

```bash
python main.py import-quantking-db --db "C:/path/to/quantking.db" --column-map configs/my_quantking_map.json
```

## 4) 분석 실행

```bash
python main.py analyze
```

결과:

- `reports/output/daily_signal_report.csv`
- `reports/output/daily_signal_report.md`
- `reports/output/daily_signal_report.html`
- `reports/output/daily_signal_report.json`

HTML 리포트에는 외부 차트 사이트 없이 `prices_daily.csv` 기준 최근 가격/거래량 SVG 차트가 자동 표시됩니다.

## 5) 로컬 사이트 실행

```bash
python main.py serve
```

브라우저에서 `http://127.0.0.1:8765` 접속.

로컬 사이트 기능:

- API 키 저장
- 네이버/pykrx CSV 자동 다운로드
- KIS 가격 + pykrx 수급/재무 다운로드
- 분석 실행
- 차트 포함 리포트 보기

## 필요한 CSV

`data/csv_import/` 폴더에 아래 파일을 둡니다.

- `universe.csv`: `stock_code, stock_name, market`
- `prices_daily.csv`: `stock_code, stock_name, date, open, high, low, close, volume, trading_value`
- `supply_daily.csv`: `stock_code, date, institution_net_buy_value, foreign_net_buy_value, pension_net_buy_value, individual_net_buy_value, trading_value`
- `financials.csv`: `stock_code, stock_name, PER, PBR, ROE, debt_ratio, operating_profit, net_income, market_cap`

## 주의

이 프로그램은 투자 추천·매수 지시가 아니라, 장기 스윙 관찰 후보를 줄여주는 리서치 도구입니다.
QuantKingSetup의 DLL/EXE/OCX는 새 프로젝트에 복사하지 않았습니다. Kiwoom OpenAPI는 공식 설치 프로그램을 통해 Windows에 설치해서 사용하세요.

## 4개 독립 알고리즘 포트폴리오

이번 버전은 RL 없이 아래 4개 전략을 독립적으로 스캔한 뒤 포트폴리오 비중을 배정합니다.

- VCP 변동성 수축: 20%
- CANSLIM 성장주 돌파: 30%
- Weinstein Stage 2 상승단계: 40%
- Darvas Box 박스 돌파: 10%

비중과 전략별 최대 종목 수는 `configs/multi_strategy_config.json`에서 수정합니다.

실행:

```bash
python main.py analyze
python main.py serve
```

UI에서 `4전략 포트폴리오` 링크를 누르거나 아래 파일을 직접 열면 됩니다.

```text
reports/output/multi_strategy_portfolio.html
reports/output/multi_strategy_portfolio.csv
reports/output/multi_strategy_signals_all.csv
```

주의: 이 구현은 공개적으로 알려진 핵심 원칙을 OHLCV 데이터로 근사한 규칙 기반 스캐너입니다. 원저자의 재량 판단, 비공개 세부 규칙, 실적 성장 데이터가 완전히 복제된 것은 아닙니다. 실전 사용 전에는 백테스트와 워크포워드 검증이 필요합니다.
