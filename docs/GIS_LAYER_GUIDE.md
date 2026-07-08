# GIS/API Layer Guide

지도는 예측 엔진과 분리된 GIS 레이어 카탈로그로 확장한다. 실제 데이터 파일이나 API 키가 들어오면 `backend/data/gis/layers_manifest.json`에 등록하고, 프론트엔드에서 레이어를 켜고 끌 수 있다.

## 1. 행정 경계

### vuski/admdongkor

- 위치: https://github.com/vuski/admdongkor
- 용도: 행정동/시군구/시도 경계, 행정 코드 매칭, choropleth
- 특징: 저장소 README 기준 GeoJSON은 WGS84(EPSG:4326), UTF-8이며 행정동 변경 이력을 반영한다.
- 넣는 곳: `backend/data/gis/admin/`
- manifest 예시:

```json
{
  "id": "admdong_chungbuk",
  "name": "충북 행정동 경계",
  "type": "polygon",
  "format": "geojson",
  "path": "admin/admdong_chungbuk.geojson",
  "source_name": "vuski/admdongkor",
  "source_url": "https://github.com/vuski/admdongkor",
  "source_type": "public_dataset",
  "visible_by_default": true,
  "style": {
    "fill": [49, 90, 125, 28],
    "line": [49, 90, 125, 180]
  }
}
```

## 2. 한국 지도 시각화 참고

### bennykim/geo-korea

- 위치: https://github.com/bennykim/geo-korea
- 용도: TopoJSON 지도, hover/click, region highlight, custom marker, 테마 참고
- 적용: 이 MVP는 빌드 없는 정적 앱이라 라이브러리를 직접 설치하지 않고 MapLibre/deck.gl 레이어 스타일에 패턴을 반영한다.

### YeonjuRyu/react-korea-map-visualization

- 위치: https://github.com/YeonjuRyu/react-korea-map-visualization
- 용도: Choropleth, Bubble, Point, Pie 스타일 참고
- 적용: 좌표 또는 지역 코드가 있는 데이터만 bubble/point로 표시한다.

## 3. DEM/고도

### 국토지리정보원 DEM

- 공공데이터포털: https://www.data.go.kr/data/15059920/fileData.do
- 국토정보플랫폼: http://map.ngii.go.kr/ms/map/NlipMap.do?tabGb=total
- 형식: IMG, 대용량 다운로드 프로그램이 필요할 수 있음
- 넣는 곳: 원본은 `backend/data/gis/terrain/raw/`, 변환 산출물은 `backend/data/gis/terrain/`

권장 변환 흐름:

1. 국토정보플랫폼에서 대상 지역의 공개 DEM을 다운로드한다.
2. QGIS 또는 GDAL로 좌표계를 확인한다.
3. 웹 시각화용으로 둘 중 하나를 만든다.
   - hillshade GeoTIFF/PNG tile
   - MapLibre `raster-dem` 타일셋
4. `.env`에 tile URL을 넣는다.

```env
DEM_TERRAIN_TILE_URL=https://example.com/tiles/{z}/{x}/{y}.png
DEM_TERRAIN_ATTRIBUTION=NGII DEM, converted by project pipeline
```

## 4. VWorld/API 후보

추가 API 키를 받을 수 있으면 아래 순서로 붙인다.

- VWorld API: 배경지도, WMS/WFS, 일부 국가공간정보 레이어
  - 신청: https://www.vworld.kr/
  - 키 위치: `.env`의 `VWORLD_API_KEY`
- 공공데이터포털 서비스키: 한강홍수통제소, K-water, 침수흔적도/재해위험지구 등
  - 키 위치: 각 API별 환경변수
- 지자체/공공 GIS: 도로, 지하차도, 배수시설, 펌프장, 하천 시설
  - GeoJSON으로 변환 후 `backend/data/gis/`에 저장

## 5. 절대 금지

- 좌표가 없는 시설을 대충 찍지 않는다.
- DEM에서 파생한 저지대/유역 분석을 홍수 발생 사실로 표현하지 않는다.
- 뉴스/사고자료 위치를 예측 입력에 섞지 않는다.
- 출처 없는 행정경계, 시설물, 침수구역을 지도에 표시하지 않는다.

