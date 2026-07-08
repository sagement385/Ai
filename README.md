# 충북 집중호우 재난 대응 디지털 트윈 지도 시스템

이 MVP는 사고 타임라인을 정답처럼 하드코딩하지 않는다. 실제 API, 공공 GIS, 수문/시설 임계값, 공학 보고서처럼 출처가 있는 입력으로 먼저 예측하고, 공식 사고자료나 뉴스 라벨은 별도 검증 단계에서만 비교한다.

## 실행

```powershell
python backend/app.py
```

브라우저에서 `http://127.0.0.1:8787`을 연다.

## 환경변수

`.env.example`을 `.env`로 복사하고 필요한 키를 넣는다.

```env
HRFCO_API_KEY=
KWATER_API_KEY=
OPENAI_API_KEY=
ENABLE_LLM=false
SAVE_API_SNAPSHOTS=true
STRICT_DATA_MODE=true
```

## 핵심 구조

- `backend/services/simulation_engine.py`: 출처 검증 후 강수, 수위, 시설 임계값 기반 예측
- `backend/services/validation_engine.py`: 뉴스/공식 사고 라벨과 예측 결과 비교
- `backend/services/hrfco_client.py`: 한강홍수통제소 표준수문DB 연결
- `backend/services/kwater_client.py`: K-water 수문 운영 정보 연결
- `frontend/index.html`: 지도, 예측, 검증, 출처 패널

## 데이터 원칙

- 출처 없는 강수, 수위, 유량, 방류량, 좌표, 시설 임계값은 계산에서 제외한다.
- 좌표가 없거나 출처가 불충분한 시설은 지도에 표시하지 않는다.
- 예측 결과는 `confirmed_collapse`를 만들지 않는다.
- 제방은 `월류 위험`, `현장 확인 필요`, `구조 취약도 자료 필요`로 표현한다.
- 공식 사고자료와 뉴스는 `/api/validation/compare`에서만 사용한다.

## API

- `GET /api/config/status`
- `GET /api/hrfco/stations?hydro_type=waterlevel`
- `GET /api/hrfco/rainfall?station_code=...&start_time=...&end_time=...`
- `GET /api/hrfco/water-level?station_code=...&start_time=...&end_time=...`
- `GET /api/kwater/dam-observations?dam_code=...&start_time=YYYY-MM-DD&end_time=YYYY-MM-DD`
- `POST /api/simulations/run`
- `POST /api/validation/compare`

## 검증 흐름

1. 실제 관측/시설 데이터를 수집한다.
2. `/api/simulations/run`으로 예측을 생성한다.
3. 공식 사고자료나 뉴스 라벨을 별도 JSON으로 정리한다.
4. `/api/validation/compare`로 맞은 부분과 틀린 부분을 비교한다.
5. 틀린 지점은 입력 데이터, 임계값, 수문 모델, 지형 자료를 보강한다.

