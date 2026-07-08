const emptyPayload = {
  scenario_id: "chungbuk_observed_event",
  mode: "observed",
  rainfall_observations: [],
  water_level_observations: [],
  hydraulic_assets: []
};

const emptyValidation = {
  validation_events: []
};

const riskColors = {
  normal: [31, 122, 77, 170],
  watch: [198, 155, 33, 190],
  danger: [199, 102, 49, 210],
  critical: [180, 35, 54, 220],
  not_available: [123, 133, 128, 130],
  unknown: [123, 133, 128, 130]
};

const state = {
  map: null,
  deck: null,
  catalog: null,
  loadedLayers: {},
  activeLayerIds: new Set(),
  lastSimulation: null,
  popup: null
};

function $(id) {
  return document.getElementById(id);
}

function setJson(id, value) {
  $(id).value = JSON.stringify(value, null, 2);
}

function parseJson(id) {
  return JSON.parse($(id).value);
}

async function getJson(url, options) {
  if (typeof window.fetch === "function") {
    const response = await window.fetch(url, options);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
  }

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(options?.method || "GET", url);
    Object.entries(options?.headers || {}).forEach(([name, value]) => xhr.setRequestHeader(name, value));
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(`HTTP ${xhr.status}`));
        return;
      }
      try {
        resolve(JSON.parse(xhr.responseText || "null"));
      } catch (error) {
        reject(error);
      }
    };
    xhr.onerror = () => reject(new Error("network_error"));
    xhr.send(options?.body || null);
  });
}

async function boot() {
  setJson("simulation-input", emptyPayload);
  setJson("validation-input", emptyValidation);
  bindEvents();
  await Promise.all([loadStatus().catch(reportStatusError), loadGisCatalog().catch(reportCatalogError)]);
  initMap();
  renderLegend();
  renderDeckLayers();
  syncDeckToMap();
}

function bindEvents() {
  $("run-simulation").addEventListener("click", runSimulation);
  $("run-validation").addEventListener("click", runValidation);
  $("load-empty").addEventListener("click", () => setJson("simulation-input", emptyPayload));
  $("format-input").addEventListener("click", () => {
    try {
      setJson("simulation-input", parseJson("simulation-input"));
    } catch (error) {
      alert("JSON 형식을 확인하세요.");
    }
  });
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
}

function switchView(id) {
  document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item.dataset.view === id));
  document.querySelectorAll(".view").forEach((item) => item.classList.toggle("active", item.id === id));
  if (id === "map-view" && state.map) {
    setTimeout(() => {
      state.map.resize();
      syncDeckToMap();
    }, 0);
  }
}

async function loadStatus() {
  const status = await getJson("/api/config/status");
  $("runtime-status").textContent = status.strict_data_mode ? "STRICT_DATA_MODE=true" : "STRICT_DATA_MODE=false";
  const list = $("api-status");
  list.innerHTML = "";
  Object.entries(status.keys).forEach(([name, value]) => {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = name;
    dd.textContent = value;
    list.append(dt, dd);
  });
}

async function loadGisCatalog() {
  const catalog = await getJson("/api/gis/catalog");
  state.catalog = catalog;
  renderLayerControls(catalog.layers || []);
  renderExternalCatalog(catalog.external_catalog || []);
  for (const layer of catalog.layers || []) {
    if (layer.visible_by_default && layer.file_exists && layer.valid_source) {
      state.activeLayerIds.add(layer.id);
      await loadLayerData(layer);
    }
  }
  renderDeckLayers();
}

function reportStatusError(error) {
  console.error(error);
  $("runtime-status").textContent = `상태 API 오류: ${error.message}`;
}

function reportCatalogError(error) {
  console.error(error);
  const root = $("layer-controls");
  root.innerHTML = "";
  root.append(emptyRecord("GIS 카탈로그 오류", error.message));
  renderExternalCatalog([]);
}

function initMap() {
  const missingEngines = [];
  if (!window.maplibregl) missingEngines.push("MapLibre GL JS");
  if (!window.deck) missingEngines.push("deck.gl");
  if (missingEngines.length) {
    showMapWarning(`${missingEngines.join(", ")}를 불러오지 못했습니다. CDN 연결을 확인하세요. API 상태와 데이터 카탈로그는 계속 확인할 수 있습니다.`);
    return false;
  }

  const style = {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "&copy; OpenStreetMap contributors"
      }
    },
    layers: [
      {
        id: "osm",
        type: "raster",
        source: "osm"
      }
    ]
  };

  state.map = new window.maplibregl.Map({
    container: "map",
    style,
    center: [127.7, 36.8],
    zoom: 8,
    pitch: 0,
    bearing: 0,
    attributionControl: true
  });
  state.map.addControl(new window.maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
  state.map.on("move", syncDeckToMap);
  state.map.on("resize", syncDeckToMap);
  state.map.on("click", handleMapClick);

  state.deck = new window.deck.Deck({
    canvas: "deck-canvas",
    controller: false,
    initialViewState: deckViewState(),
    layers: [],
    getTooltip: ({ object }) => object && tooltipForObject(object)
  });
  hideMapWarning();
  return true;
}

function showMapWarning(message) {
  const warning = $("map-engine-warning");
  warning.textContent = message;
  warning.hidden = false;
}

function hideMapWarning() {
  const warning = $("map-engine-warning");
  warning.textContent = "";
  warning.hidden = true;
}

function deckViewState() {
  const center = state.map?.getCenter?.() || { lng: 127.7, lat: 36.8 };
  return {
    longitude: center.lng,
    latitude: center.lat,
    zoom: state.map?.getZoom?.() || 8,
    bearing: state.map?.getBearing?.() || 0,
    pitch: state.map?.getPitch?.() || 0
  };
}

function syncDeckToMap() {
  if (!state.map || !state.deck) return;
  const canvas = $("deck-canvas");
  const mapCanvas = state.map.getCanvas();
  canvas.width = mapCanvas.clientWidth * window.devicePixelRatio;
  canvas.height = mapCanvas.clientHeight * window.devicePixelRatio;
  canvas.style.width = `${mapCanvas.clientWidth}px`;
  canvas.style.height = `${mapCanvas.clientHeight}px`;
  state.deck.setProps({ viewState: deckViewState() });
}

function renderLayerControls(layers) {
  const root = $("layer-controls");
  root.innerHTML = "";
  if (!layers.length) {
    const item = document.createElement("div");
    item.className = "layer-item";
    item.innerHTML = `
      <div class="layer-title">등록된 GIS 레이어 없음</div>
      <div class="layer-meta">backend/data/gis/layers_manifest.json에 실제 GeoJSON 레이어를 등록하세요.</div>
    `;
    root.append(item);
    return;
  }

  layers.forEach((layer) => {
    const disabled = !layer.file_exists || !layer.valid_source;
    const item = document.createElement("div");
    item.className = "layer-item";
    item.innerHTML = `
      <label class="layer-title">
        <input type="checkbox" ${layer.visible_by_default ? "checked" : ""} ${disabled ? "disabled" : ""} />
        <span>${escapeHtml(layer.name || layer.id)}</span>
      </label>
      <div class="layer-meta">
        ${escapeHtml(layer.type || "unknown")} · ${layer.file_exists ? "파일 있음" : "파일 없음"} · ${layer.valid_source ? "출처 확인" : layer.validation_reason}
      </div>
    `;
    const input = item.querySelector("input");
    input.addEventListener("change", async () => {
      if (input.checked) {
        state.activeLayerIds.add(layer.id);
        await loadLayerData(layer);
      } else {
        state.activeLayerIds.delete(layer.id);
      }
      renderDeckLayers();
    });
    root.append(item);
  });
}

async function loadLayerData(layer) {
  if (state.loadedLayers[layer.id]) return state.loadedLayers[layer.id];
  const data = await getJson(layer.url);
  state.loadedLayers[layer.id] = { meta: layer, data };
  return state.loadedLayers[layer.id];
}

function renderDeckLayers() {
  if (!state.deck || !window.deck) return;
  const deckLayers = [];

  for (const layerId of state.activeLayerIds) {
    const loaded = state.loadedLayers[layerId];
    if (!loaded) continue;
    deckLayers.push(makeGeoJsonLayer(loaded.meta, loaded.data));
  }

  deckLayers.push(...predictionLayers(state.lastSimulation?.predictions || []));
  state.deck.setProps({ layers: deckLayers.filter(Boolean) });
}

function makeGeoJsonLayer(meta, data) {
  if (!window.deck) return null;
  const style = meta.style || {};
  const type = meta.type || "polygon";
  return new window.deck.GeoJsonLayer({
    id: `gis-${meta.id}`,
    data,
    pickable: true,
    stroked: true,
    filled: type !== "line",
    pointType: "circle",
    getFillColor: style.fill || [49, 90, 125, 45],
    getLineColor: style.line || [49, 90, 125, 190],
    getPointRadius: style.point_radius || 70,
    pointRadiusMinPixels: 4,
    pointRadiusMaxPixels: 18,
    getLineWidth: style.line_width || 2,
    lineWidthMinPixels: 1,
    autoHighlight: true,
    highlightColor: [255, 255, 255, 80],
    updateTriggers: {
      getFillColor: [JSON.stringify(style.fill || [])],
      getLineColor: [JSON.stringify(style.line || [])]
    }
  });
}

function predictionLayers(predictions) {
  if (!window.deck) return [];
  const features = [];
  predictions.forEach((prediction) => {
    if (!prediction.geometry) return;
    features.push({
      type: "Feature",
      geometry: prediction.geometry,
      properties: prediction
    });
  });
  if (!features.length) return [];
  return [
    new window.deck.GeoJsonLayer({
      id: "prediction-risk",
      data: { type: "FeatureCollection", features },
      pickable: true,
      stroked: true,
      filled: true,
      pointType: "circle",
      getFillColor: (feature) => riskColors[feature.properties?.risk_level] || riskColors.unknown,
      getLineColor: (feature) => riskColors[feature.properties?.risk_level] || riskColors.unknown,
      getPointRadius: 130,
      pointRadiusMinPixels: 7,
      pointRadiusMaxPixels: 26,
      getLineWidth: 5,
      lineWidthMinPixels: 3,
      autoHighlight: true,
      highlightColor: [255, 255, 255, 90]
    })
  ];
}

function handleMapClick(event) {
  if (!state.deck || !state.map || !window.maplibregl) return;
  const picked = state.deck.pickObject({ x: event.point.x, y: event.point.y, radius: 8 });
  if (!picked?.object) return;
  const html = tooltipForObject(picked.object);
  if (!html) return;
  if (state.popup) state.popup.remove();
  state.popup = new window.maplibregl.Popup({ closeButton: true, closeOnClick: true })
    .setLngLat(event.lngLat)
    .setHTML(html)
    .addTo(state.map);
}

function tooltipForObject(object) {
  const props = object.properties || object;
  const title = props.name || props.asset_id || props.adm_nm || props.adm_nm_full || props.layer_name || "GIS feature";
  const risk = props.risk_level ? `<br><b>위험도</b> ${escapeHtml(props.risk_level)}` : "";
  const source = props.source_basis?.length ? `<br><b>근거</b> ${escapeHtml(props.source_basis.join(", "))}` : "";
  return `<div><b>${escapeHtml(title)}</b>${risk}${source}</div>`;
}

async function runSimulation() {
  let payload;
  try {
    payload = parseJson("simulation-input");
  } catch (error) {
    alert("예측 입력 JSON 형식을 확인하세요.");
    return;
  }
  try {
    const result = await getJson("/api/simulations/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    state.lastSimulation = result.simulation;
    renderResult(result);
  } catch (error) {
    alert(`시뮬레이션 요청 실패: ${error.message}`);
  }
}

async function runValidation() {
  if (!state.lastSimulation) {
    alert("먼저 시뮬레이션을 실행하세요.");
    return;
  }
  let validation;
  try {
    validation = parseJson("validation-input");
  } catch (error) {
    alert("검증 라벨 JSON 형식을 확인하세요.");
    return;
  }
  try {
    const result = await getJson("/api/validation/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prediction_result: state.lastSimulation, validation })
    });
    $("validation-output").textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    alert(`검증 요청 실패: ${error.message}`);
  }
}

function renderResult(result) {
  const simulation = result.simulation;
  const decision = result.decision_cards;
  renderRisk(simulation.overall_risk);
  renderDeckLayers();
  fitPredictionBounds(simulation.predictions || []);
  renderUnmapped(simulation.unmapped_assets || []);
  renderPredictions(simulation.predictions || []);
  renderDecisions(decision.actions || []);
  renderGaps(simulation.data_gaps || []);
  $("raw-output").textContent = JSON.stringify(result, null, 2);
}

function fitPredictionBounds(predictions) {
  if (!state.map || !window.maplibregl) return;
  const coords = [];
  predictions.forEach((prediction) => collectCoordinates(prediction.geometry, coords));
  if (!coords.length) return;
  const bounds = coords.reduce((box, coord) => box.extend(coord), new window.maplibregl.LngLatBounds(coords[0], coords[0]));
  state.map.fitBounds(bounds, { padding: 80, duration: 700, maxZoom: 15 });
}

function collectCoordinates(geometry, coords) {
  if (!geometry) return;
  const value = geometry.coordinates;
  if (!value) return;
  walkCoordinates(value, coords);
}

function walkCoordinates(value, coords) {
  if (!Array.isArray(value)) return;
  if (typeof value[0] === "number" && typeof value[1] === "number") {
    coords.push([value[0], value[1]]);
    return;
  }
  value.forEach((child) => walkCoordinates(child, coords));
}

function renderRisk(risk) {
  const pill = $("overall-risk");
  pill.textContent = risk || "unknown";
  pill.className = "risk-pill";
  if (["normal", "watch", "danger", "critical"].includes(risk)) {
    pill.classList.add(`risk-${risk}`);
  }
}

function renderUnmapped(items) {
  const list = $("unmapped-assets");
  list.innerHTML = "";
  if (!items.length) {
    list.append(emptyItem("없음"));
    return;
  }
  items.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = `${item.name || item.asset_id}: ${item.reason}`;
    list.append(li);
  });
}

function renderPredictions(items) {
  const list = $("prediction-list");
  list.innerHTML = "";
  if (!items.length) {
    list.append(emptyRecord("예측 결과 없음", "출처 있는 강수·시설 데이터가 필요합니다."));
    return;
  }
  items.forEach((item) => {
    list.append(record(item.name || item.asset_id, item.risk_level, [
      `유형: ${item.asset_type}`,
      `제방 판단: ${item.collapse_assessment?.status || "not_available"}`,
      `지도 표시: ${item.geometry_available ? "가능" : "불가"}`,
      `공백: ${(item.data_gaps || []).length}`
    ]));
  });
}

function renderDecisions(items) {
  const list = $("decision-list");
  list.innerHTML = "";
  if (!items.length) {
    list.append(emptyRecord("조치 카드 없음", "source_basis 없는 조치는 제거됩니다."));
    return;
  }
  items.forEach((item) => {
    list.append(record(item.action, item.target, [
      `우선순위: ${item.priority}`,
      item.reason,
      `근거: ${(item.source_basis || []).join(", ")}`
    ]));
  });
}

function renderGaps(items) {
  const list = $("data-gaps");
  list.innerHTML = "";
  if (!items.length) {
    list.append(emptyItem("없음"));
    return;
  }
  items.forEach((gap) => {
    const li = document.createElement("li");
    li.textContent = gap;
    list.append(li);
  });
}

function renderExternalCatalog(items) {
  const root = $("external-catalog");
  if (!root) return;
  root.innerHTML = "";
  if (!items.length) {
    root.append(emptyRecord("외부 카탈로그 없음", "GIS_LAYER_GUIDE.md를 확인하세요."));
    return;
  }
  items.forEach((item) => {
    const card = record(item.name, item.id, [
      item.recommended_use,
      `출처: ${item.source_name}`,
      item.source_url
    ]);
    root.append(card);
  });
}

function renderLegend() {
  $("map-legend").innerHTML = `
    <div class="legend-row"><span class="legend-swatch" style="background: rgba(31,122,77,.75)"></span>normal</div>
    <div class="legend-row"><span class="legend-swatch" style="background: rgba(198,155,33,.75)"></span>watch</div>
    <div class="legend-row"><span class="legend-swatch" style="background: rgba(199,102,49,.75)"></span>danger</div>
    <div class="legend-row"><span class="legend-swatch" style="background: rgba(180,35,54,.75)"></span>critical</div>
    <div class="legend-row"><span class="legend-swatch" style="background: rgba(49,90,125,.25)"></span>GIS layer</div>
  `;
}

function record(title, badge, lines) {
  const div = document.createElement("div");
  div.className = "record";
  div.innerHTML = `
    <div class="record-title"><span>${escapeHtml(title)}</span><span>${escapeHtml(badge || "")}</span></div>
    <div class="record-meta">${lines.map(escapeHtml).join("<br>")}</div>
  `;
  return div;
}

function emptyRecord(title, message) {
  return record(title, "", [message]);
}

function emptyItem(text) {
  const li = document.createElement("li");
  li.textContent = text;
  return li;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

boot().catch((error) => {
  console.error(error);
  $("runtime-status").textContent = "오류";
});
