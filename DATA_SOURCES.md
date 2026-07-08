# Data Sources

## 한강홍수통제소 표준수문DB

- 목적: 강수량, 수위, 유량, 댐, 보, 홍수예보, 레이더 정보
- 공식 페이지: https://www.data.go.kr/data/3040409/openapi.do
- 기관 레퍼런스: https://www.hrfco.go.kr/web/openapiPage/reference.do
- 키: `HRFCO_API_KEY`
- 스냅샷 위치: `backend/logs/api_snapshots/`

## K-water 수문 운영 정보

- 목적: 댐수위, 강우량, 유입량, 총방류량, 저수량, 저수율
- 공식 페이지: https://www.data.go.kr/data/15099110/openapi.do
- 서비스 URL: `http://apis.data.go.kr/B500001/dam/sluicePresentCondition`
- 주요 기능: `hourlist`, `list`, `daylist`
- 키: `KWATER_API_KEY`
- 스냅샷 위치: `backend/logs/api_snapshots/`

## 시설/지형/GIS 데이터

지도 표시는 아래 조건을 만족한 시설만 허용한다.

- `geometry`가 실제 GIS 또는 사용자가 검증한 좌표에서 왔을 것
- `source_name`, `source_url`, `source_type`이 있을 것
- 시설 임계값도 별도 출처가 있을 것

좌표 또는 출처가 없으면 지도에 표시하지 않고 `unmapped_assets`에 남긴다.

## 검증 라벨

뉴스나 공식 사고자료는 예측 입력이 아니다. 다음 필드를 가진 라벨로만 사용한다.

- `event_id`
- `asset_id`
- `event_type`
- `expected_model_signal`
- `observed_at`
- `source_name`
- `source_url`
- `source_type`

