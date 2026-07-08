# GIS Layer Drop Zone

이 폴더는 지도 시각화에 쓰는 실제 GIS 파일을 넣는 곳이다.

## 원칙

- 임의 좌표와 임의 경계를 만들지 않는다.
- GeoJSON, PMTiles URL, raster-dem tile URL처럼 출처가 명확한 자료만 연결한다.
- 파일을 넣은 뒤 `layers_manifest.json`에 `source_name`, `source_url`, `source_type`, `format`, `path`를 기록한다.
- 좌표계는 웹 지도 표시용으로 WGS84/EPSG:4326 GeoJSON을 우선 사용한다.
- EPSG:5179, EPSG:5186, IMG DEM 등은 QGIS/GDAL에서 변환한 산출물과 변환 절차를 문서화한 뒤 사용한다.

## 권장 하위 폴더

- `admin/`: 행정동, 시군구, 시도 경계
- `hydro/`: 하천, 홍수흔적, 침수예상도, 수위관측소
- `transport/`: 도로, 지하차도, 통제구간
- `terrain/`: DEM 변환 산출물 또는 terrain tile 메타데이터
- `facilities/`: 배수장, 펌프장, 제방, 댐, 보 등 검증 시설

