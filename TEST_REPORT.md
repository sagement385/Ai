# Test Report

## 1. API Connection Test

- 한강홍수통제소 API: 키가 없으면 `api_key_missing`을 반환한다.
- K-water API: 키가 없으면 `api_key_missing`을 반환한다.
- 응답 스냅샷: API 호출 시 `backend/logs/api_snapshots/`에 저장하도록 구현했다.
- 실패 처리: `external_api_failed` 또는 명시적 오류를 반환하고 임의 데이터로 대체하지 않는다.

## 2. Prediction/Validation Separation Test

- 공식 사고자료/뉴스 라벨은 `/api/validation/compare`에서만 사용한다.
- `/api/simulations/run`은 validation label을 입력으로 요구하지 않는다.
- 제방 붕괴 확정 표현은 예측 엔진에서 생성하지 않는다.

## 3. No Hardcoding Check

- 임의 강수량 없음: 출처 없는 강수 관측값은 제외된다.
- 임의 수위 없음: 출처 없는 수위 관측값은 제외된다.
- 임의 유량 없음: API 또는 출처 있는 입력만 사용한다.
- 임의 방류량 없음: K-water 응답 또는 출처 있는 입력만 사용한다.
- 임의 좌표 없음: 출처 검증 없는 geometry는 지도에서 제외된다.
- 임의 제방 붕괴 없음: 예측 결과는 `collapse_not_determined`까지만 표시한다.

## 4. LLM Guardrail Test

- LLM 역할은 요약과 우선순위 카드 생성으로 제한했다.
- allowed_actions 외 조치는 필터링한다.
- source_basis 없는 action은 제거한다.
- 댐, 수문, 배수문 작동 명령은 생성하지 않는다.

## 5. Map Rendering Test

- 좌표와 출처가 있는 시설만 지도에 표시한다.
- 좌표 없는 시설은 `좌표 없음` 목록에 표시한다.
- 출처 없는 시설은 위험도 계산에서 제외한다.

## 6. Local Test Command

```powershell
python -m unittest discover -s tests
```

