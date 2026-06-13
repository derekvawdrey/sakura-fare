/* 桜旅 Sakura Fare — upload, live agent progress, map + city guide rendering. */
"use strict";

const $ = (id) => document.getElementById(id);
const yen = (n) => "¥" + Number(n || 0).toLocaleString("en-US");
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

/* ---------- falling sakura petals ---------- */
(() => {
  const canvas = $("petals");
  const ctx = canvas.getContext("2d");
  let petals = [];

  const newPetal = (anywhere) => ({
    x: Math.random() * canvas.width,
    y: anywhere ? Math.random() * canvas.height : -20,
    size: 5 + Math.random() * 7,
    vy: 0.5 + Math.random() * 1.1,
    drift: 0.3 + Math.random() * 0.9,
    phase: Math.random() * Math.PI * 2,
    spin: (Math.random() - 0.5) * 0.04,
    angle: Math.random() * Math.PI * 2,
    hue: 340 + Math.random() * 15,
  });

  const resize = () => {
    canvas.width = innerWidth;
    canvas.height = innerHeight;
    const count = Math.min(48, Math.floor(innerWidth / 28));
    while (petals.length < count) petals.push(newPetal(true));
    petals.length = count;
  };

  function drawPetal(p) {
    ctx.save();
    ctx.translate(p.x, p.y);
    ctx.rotate(p.angle);
    ctx.beginPath();
    ctx.moveTo(0, -p.size);
    ctx.bezierCurveTo(p.size * 0.9, -p.size * 0.6, p.size * 0.7, p.size * 0.6, 0, p.size);
    ctx.bezierCurveTo(-p.size * 0.7, p.size * 0.6, -p.size * 0.9, -p.size * 0.6, 0, -p.size);
    ctx.fillStyle = `hsla(${p.hue}, 75%, 84%, .8)`;
    ctx.fill();
    ctx.restore();
  }

  let t = 0;
  (function tick() {
    t += 0.01;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const p of petals) {
      p.y += p.vy;
      p.x += Math.sin(t * 2 + p.phase) * p.drift;
      p.angle += p.spin;
      if (p.y > canvas.height + 20) Object.assign(p, newPetal(false));
      drawPetal(p);
    }
    requestAnimationFrame(tick);
  })();
  addEventListener("resize", resize);
  resize();
})();

/* ---------- health check ---------- */
(async () => {
  const elH = $("health");
  try {
    const h = await (await fetch("/api/health")).json();
    if (h.llm.reachable) {
      elH.classList.add("ok");
      const ws = h.web_search || {};
      const provider = ws.searxng ? "SearXNG" : (ws.active_provider || "fallback");
      elH.innerHTML = `<span class="dot"></span> local model ready · ${h.llm.model} · search: ${provider}`;
    } else {
      elH.classList.add("bad");
      elH.innerHTML = `<span class="dot"></span> local model unreachable at ${h.llm.base_url}`;
    }
    $("ds-version").textContent = h.fares_dataset;
  } catch {
    elH.classList.add("bad");
    elH.innerHTML = '<span class="dot"></span> backend unreachable';
  }
})();

/* ---------- upload form ---------- */
const dropzone = $("dropzone");
const fileInput = $("file-input");
let chosenFile = null;

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") fileInput.click(); });
fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
["dragover", "dragenter"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("drag"); }));
dropzone.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));

function setFile(f) {
  chosenFile = f || null;
  const elF = $("file-chosen");
  elF.hidden = !chosenFile;
  if (chosenFile) elF.textContent = `🌸 ${chosenFile.name} (${(chosenFile.size / 1024).toFixed(0)} KB)`;
}

/* ---------- run analysis ---------- */
let map = null;

$("analyze-btn").addEventListener("click", startAnalysis);
$("again-btn").addEventListener("click", () => {
  $("result-card").hidden = true;
  $("progress-card").hidden = true;
  $("upload-card").hidden = false;
  if (map) { map.remove(); map = null; }
  setFile(null);
  fileInput.value = "";
});

async function startAnalysis() {
  const errEl = $("form-error");
  errEl.hidden = true;

  const form = new FormData();
  const pasted = $("text-input").value.trim();
  if (chosenFile) form.append("file", chosenFile);
  else if (pasted) form.append("text", pasted);
  else {
    errEl.textContent = "Choose a file or paste your travel plan first.";
    errEl.hidden = false;
    return;
  }
  const travelers = $("travelers").value;
  if (travelers) form.append("travelers", travelers);
  form.append("depth", document.querySelector('input[name="depth"]:checked').value);

  $("analyze-btn").disabled = true;
  try {
    const resp = await fetch("/api/analyses", { method: "POST", body: form });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || `Upload failed (HTTP ${resp.status})`);
    }
    const { id } = await resp.json();
    $("upload-card").hidden = true;
    $("progress-card").hidden = false;
    $("spinner").hidden = false;
    $("timeline").innerHTML = "";
    $("phase-strip").innerHTML = "";
    $("live-stat").hidden = true;
    $("progress-doc").textContent = chosenFile ? chosenFile.name : "pasted text";
    poll(id);
  } catch (e) {
    errEl.textContent = e.message;
    errEl.hidden = false;
  } finally {
    $("analyze-btn").disabled = false;
  }
}

async function poll(id) {
  let rendered = 0;
  const timer = setInterval(async () => {
    let job;
    try {
      job = await (await fetch(`/api/analyses/${id}`)).json();
    } catch { return; /* transient blip; keep polling */ }

    renderPhases(job.events);
    renderLiveStat(job.partial);

    const tl = $("timeline");
    const stick = tl.scrollTop + tl.clientHeight >= tl.scrollHeight - 30;
    for (; rendered < job.events.length; rendered++) {
      const ev = job.events[rendered];
      const li = el("li", ev.kind);
      li.appendChild(el("span", "ev-title", labelFor(ev)));
      if (ev.detail) li.appendChild(el("span", "ev-detail", ev.detail));
      tl.appendChild(li);
    }
    if (stick) tl.scrollTop = tl.scrollHeight;

    if (job.status === "done") {
      clearInterval(timer);
      $("spinner").hidden = true;
      renderResult(job.result);
    } else if (job.status === "error") {
      clearInterval(timer);
      $("spinner").hidden = true;
      const li = el("li", "error");
      li.appendChild(el("span", "ev-title", "⚠ Failed"));
      li.appendChild(el("span", "ev-detail", job.error || "unknown error"));
      tl.appendChild(li);
    }
  }, 1500);
}

function renderPhases(events) {
  const strip = $("phase-strip");
  const phases = events.filter((e) => e.kind === "phase");
  const doneAll = events.some((e) => e.kind === "done");
  strip.innerHTML = "";
  phases.forEach((p, i) => {
    const isLast = i === phases.length - 1;
    const chip = el("span",
      "phase-chip " + (isLast && !doneAll ? "active" : "done"),
      (isLast && !doneAll ? "⏳ " : "✓ ") + p.title);
    strip.appendChild(chip);
  });
}

function renderLiveStat(partial) {
  const parts = [];
  if (partial.rail_segments) {
    const total = partial.rail_segments.reduce((a, s) => a + s.fare_jpy, 0);
    parts.push(`rail so far: ${yen(total)}/person · ${partial.rail_segments.length} segments`);
  }
  if (partial.city_plans && partial.city_plans.length)
    parts.push(`${partial.city_plans.length} city guide(s) ready`);
  $("live-stat").hidden = parts.length === 0;
  $("live-stat").textContent = "🌸 " + parts.join(" · ");
}

function labelFor(ev) {
  const names = {
    search_station: "🔎 Searching stations",
    lookup_route_fare: "🚄 Looking up fare",
    city_transit_info: "🚇 City transit info",
    city_guide: "🏮 Curated city guide",
    food_cost_reference: "🍜 Food cost reference",
    web_search: "🌐 Web search",
    fetch_page: "📄 Reading page",
  };
  if (ev.kind === "phase") return "⛩️ " + ev.title;
  if (ev.kind === "tool_call") return names[ev.title] || `🔧 ${ev.title}`;
  if (ev.kind === "tool_result") return "↪ result";
  if (ev.kind === "done") return "🌸 " + ev.title;
  if (ev.kind === "error") return "⚠ " + ev.title;
  return ev.title;
}

/* ---------- render result ---------- */
function renderResult(r) {
  $("progress-card").hidden = true;
  $("result-card").hidden = false;

  $("total-group").textContent = yen(r.totals.total_group_jpy);
  $("total-label").textContent =
    (r.travelers > 1 ? `total · ${r.travelers} travelers` : "total · solo") +
    (r.depth === "quick" ? " · rail only" : " · rail + transit + food");
  $("total-person").textContent = yen(r.totals.total_per_person_jpy);

  renderBreakdown(r.totals);

  const badge = $("confidence-badge");
  badge.className = `badge ${r.confidence}`;
  badge.textContent = `${r.confidence} confidence · ${r.published_fare_count} published / ${r.estimated_fare_count} estimated fares`;

  $("exec-summary").textContent = r.executive_summary || "";
  $("exec-summary").hidden = !r.executive_summary;
  $("trip-summary").textContent = r.trip_summary + (r.season ? ` · ${r.season}` : "");

  renderMap(r.map);
  renderSegments(r.rail_segments);
  renderCities(r.city_plans);

  const al = $("assumptions");
  al.innerHTML = "";
  (r.assumptions || []).forEach((a) => al.appendChild(el("li", null, a)));

  $("disclaimer").textContent = (r.disclaimers || []).join(" — ");
}

function renderBreakdown(t) {
  const bar = $("breakdown-bar");
  const legend = $("breakdown-legend");
  bar.innerHTML = "";
  legend.innerHTML = "";
  const parts = [
    ["seg-rail", "rail", t.rail_jpy],
    ["seg-transit", "local transit", t.local_transit_jpy],
    ["seg-food", "food", t.food_jpy],
  ].filter(([, , v]) => v > 0);
  const sum = parts.reduce((a, [, , v]) => a + v, 0) || 1;
  for (const [cls, label, value] of parts) {
    const seg = el("div", cls);
    seg.style.width = `${(value / sum) * 100}%`;
    seg.title = `${label}: ${yen(value)}`;
    bar.appendChild(seg);
    const li = el("li");
    li.appendChild(el("span", `swatch ${cls}`));
    li.appendChild(document.createTextNode(`${label} ${yen(value)}`));
    legend.appendChild(li);
  }
}

function renderMap(payload) {
  const block = $("map-block");
  if (!payload || !payload.cities || payload.cities.length === 0) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  if (map) { map.remove(); map = null; }
  map = L.map("map", { scrollWheelZoom: false });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  if (payload.route && payload.route.length > 1) {
    L.polyline(payload.route, {
      color: "#c0526f", weight: 3, dashArray: "8 8", opacity: 0.75,
    }).addTo(map);
  }

  const bounds = [];
  for (const c of payload.cities) {
    bounds.push([c.lat, c.lon]);
    L.marker([c.lat, c.lon], {
      icon: L.divIcon({ className: "city-marker", html: String(c.order), iconSize: [28, 28] }),
      zIndexOffset: 500,
    }).addTo(map).bindPopup(`<b>${c.order}. ${c.name}</b><br>${c.days} day(s)`);
  }
  for (const p of payload.pois || []) {
    const isGem = p.kind === "gem";
    L.circleMarker([p.lat, p.lon], {
      radius: 7,
      color: "#fff", weight: 1.5,
      fillColor: isGem ? "#b8945f" : "#e87a9a",
      fillOpacity: 0.95,
    }).addTo(map).bindPopup(
      `<b>${isGem ? "💎 " : "🌸 "}${p.name}</b> <i>(${p.city})</i><br>${p.why}` +
      (p.cost_jpy ? `<br>admission ~${yen(p.cost_jpy)}` : "")
    );
  }
  map.fitBounds(bounds, { padding: [40, 40] });
  // Tiles can misrender inside a freshly-unhidden container.
  setTimeout(() => map.invalidateSize(), 120);
}

function renderSegments(segments) {
  const tbody = $("segments-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const s of segments) {
    const tr = el("tr");
    tr.appendChild(el("td", null, s.day || "—"));
    const route = el("td");
    route.appendChild(el("strong", null, `${s.from} → ${s.to}`));
    route.appendChild(el("span", "route-sub", s.line + (s.notes ? ` · ${s.notes}` : "")));
    tr.appendChild(route);
    tr.appendChild(el("td", null, s.train));
    tr.appendChild(el("td", "fare", yen(s.fare_jpy)));
    const basis = el("td");
    basis.appendChild(el("span", `basis-pill ${s.basis}`, s.basis));
    tr.appendChild(basis);
    tbody.appendChild(tr);
  }
}

function renderCities(plans) {
  const block = $("cities-block");
  const wrap = $("city-cards");
  wrap.innerHTML = "";
  block.hidden = !plans || plans.length === 0;
  if (block.hidden) return;

  plans.forEach((p, i) => {
    const card = document.createElement("details");
    card.className = "city-card";
    if (i === 0) card.open = true;

    const summary = el("summary");
    summary.appendChild(el("span", "city-name", `${i + 1}. ${p.city}`));
    summary.appendChild(el("span", "city-days", `${p.days} day(s)`));
    summary.appendChild(el("span", "city-cost",
      `food ${yen(p.food_total_jpy)} + transit ${yen(p.transit_total_jpy)} /person`));
    card.appendChild(summary);

    const body = el("div", "city-body");

    if (p.seasonal_note) body.appendChild(el("p", "seasonal-note", `🌸 ${p.seasonal_note}`));

    const grid = el("div", "city-grid");

    // food + transit column
    const colA = el("div", "city-section");
    colA.appendChild(el("h4", null, "Food budget 食費"));
    const foodLine = el("p", "food-line");
    foodLine.innerHTML = `<b>${p.food_tier}</b> tier · ${yen(p.food_daily_jpy)}/day × ${p.days} = <b>${yen(p.food_total_jpy)}</b> per person`;
    colA.appendChild(foodLine);
    if (p.food_notes && p.food_notes.length) {
      const chips = el("div", "chip-row");
      p.food_notes.forEach((n) => chips.appendChild(el("span", "food-chip", n)));
      colA.appendChild(chips);
    }
    colA.appendChild(el("h4", null, "Getting around 市内交通"));
    const transitLine = el("p", "food-line");
    transitLine.innerHTML = `${p.transit_recommendation} — <b>${yen(p.transit_total_jpy)}</b> per person`;
    colA.appendChild(transitLine);
    grid.appendChild(colA);

    // highlights + gems column
    const colB = el("div", "city-section");
    colB.appendChild(el("h4", null, "Highlights 見どころ"));
    colB.appendChild(poiList(p.highlights, "🌸"));
    if (p.hidden_gems && p.hidden_gems.length) {
      colB.appendChild(el("h4", null, "Hidden gems 隠れた名所"));
      colB.appendChild(poiList(p.hidden_gems, "💎"));
    }
    grid.appendChild(colB);
    body.appendChild(grid);

    if (p.day_plan && p.day_plan.length) {
      const sec = el("div", "city-section");
      sec.appendChild(el("h4", null, "Suggested days 一日の流れ"));
      const table = el("table", "day-plan-table");
      table.innerHTML = "<thead><tr><th></th><th>Morning</th><th>Afternoon</th><th>Evening</th></tr></thead>";
      const tb = el("tbody");
      for (const d of p.day_plan) {
        const tr = el("tr");
        [d.label, d.morning, d.afternoon, d.evening].forEach((v) => tr.appendChild(el("td", null, v)));
        tb.appendChild(tr);
      }
      table.appendChild(tb);
      sec.appendChild(table);
      body.appendChild(sec);
    }

    if (p.sources && p.sources.length) {
      body.appendChild(el("p", "src-list", "sources: " + p.sources.join(" · ")));
    }

    card.appendChild(body);
    wrap.appendChild(card);
  });
}

function poiList(pois, icon) {
  const ul = el("ul", "poi-list");
  for (const p of pois || []) {
    const li = el("li");
    li.appendChild(el("span", "gem-ico", icon));
    li.appendChild(el("span", "poi-name", p.name));
    if (p.cost_jpy) li.appendChild(el("span", "poi-cost", yen(p.cost_jpy)));
    else if (p.cost_jpy === 0) li.appendChild(el("span", "poi-cost", "free"));
    li.appendChild(el("span", "poi-why", p.why));
    ul.appendChild(li);
  }
  return ul;
}
