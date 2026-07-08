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

const state = {
  map: null,
  layer: null,
  lastSimulation: null
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
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.json();
}

async function boot() {
  setJson("simulation-input", emptyPayload);
  setJson("validation-input", emptyValidation);
  bindEvents();
  initMap();
  await loadStatus();
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
    setTimeout(() => state.map.invalidateSize(), 0);
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

function initMap() {
  state.map = L.map("map", { zoomControl: true }).setView([36.8, 127.7], 8);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(state.map);
  state.layer = L.layerGroup().addTo(state.map);
}

async function runSimulation() {
  let payload;
  try {
    payload = parseJson("simulation-input");
  } catch (error) {
    alert("예측 입력 JSON 형식을 확인하세요.");
    return;
  }
  const result = await getJson("/api/simulations/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  state.lastSimulation = result.simulation;
  renderResult(result);
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
  const result = await getJson("/api/validation/compare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prediction_result: state.lastSimulation, validation })
  });
  $("validation-output").textContent = JSON.stringify(result, null, 2);
}

function renderResult(result) {
  const simulation = result.simulation;
  const decision = result.decision_cards;
  renderRisk(simulation.overall_risk);
  renderMap(simulation.predictions || []);
  renderUnmapped(simulation.unmapped_assets || []);
  renderPredictions(simulation.predictions || []);
  renderDecisions(decision.actions || []);
  renderGaps(simulation.data_gaps || []);
  $("raw-output").textContent = JSON.stringify(result, null, 2);
}

function renderRisk(risk) {
  const pill = $("overall-risk");
  pill.textContent = risk || "unknown";
  pill.className = "risk-pill";
  if (["normal", "watch", "danger", "critical"].includes(risk)) {
    pill.classList.add(`risk-${risk}`);
  }
}

function renderMap(predictions) {
  state.layer.clearLayers();
  const bounds = [];
  predictions.forEach((prediction) => {
    if (!prediction.geometry) return;
    const geo = prediction.geometry;
    const color = prediction.map_color || "gray";
    const popup = `<strong>${escapeHtml(prediction.name)}</strong><br>${escapeHtml(prediction.risk_level)}`;
    const layer = L.geoJSON(geo, {
      pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
        radius: 8,
        color,
        fillColor: color,
        fillOpacity: 0.75,
        weight: 2
      }),
      style: { color, weight: 4 }
    }).bindPopup(popup);
    layer.addTo(state.layer);
    layer.eachLayer((item) => {
      if (item.getLatLng) bounds.push(item.getLatLng());
      if (item.getBounds) bounds.push(item.getBounds());
    });
  });
  if (bounds.length > 0) {
    const group = L.featureGroup(state.layer.getLayers());
    state.map.fitBounds(group.getBounds().pad(0.2));
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

