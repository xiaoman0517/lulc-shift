/* 土地覆盖变化检测 demo 前端逻辑 */
(function () {
  "use strict";

  const API = {
    meta: "/api/meta",
    createJob: "/api/jobs",
    job: (id) => `/api/jobs/${id}`,
    download: (id, fmt) => `/api/jobs/${id}/download?fmt=${fmt}`,
    tiles: (id, layer) => `/api/jobs/${id}/tiles/${layer}/{z}/{x}/{y}.png`,
  };

  // 与后端 engine.CLASS_NAMES / CLASS_COLORS 一致
  const CLASS_NAMES = { 1: "水体", 2: "林地", 3: "草地", 4: "洪泛植被", 5: "作物", 6: "灌木", 7: "建设用地", 8: "裸地", 9: "雪/冰" };
  const CLASS_COLORS = {
    1: "#419bdf", 2: "#397d49", 3: "#88b053", 4: "#7a87c6", 5: "#e49635",
    6: "#dfc35a", 7: "#c4281b", 8: "#a59b8f", 9: "#b39fe1",
  };
  const TAB20 = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
    "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5",
  ];

  // ---- 元信息 ----
  let YEARS = [];
  fetch(API.meta)
    .then((r) => r.json())
    .then((m) => {
      YEARS = m.years;
      fillYears();
      buildClassLegend();
    })
    .catch(() => alert("无法获取元信息，请确认后端已启动"));

  function fillYears() {
    const before = document.getElementById("year-before");
    const after = document.getElementById("year-after");
    before.innerHTML = "";
    after.innerHTML = "";
    YEARS.forEach((y) => {
      before.appendChild(new Option(y, y));
      after.appendChild(new Option(y, y));
    });
    if (YEARS.length >= 2) {
      before.value = YEARS[0];
      after.value = YEARS[YEARS.length - 1];
    }
  }

  function buildClassLegend() {
    const el = document.getElementById("class-legend");
    el.innerHTML = Object.keys(CLASS_NAMES)
      .map((k) => `<div class="item"><span class="swatch" style="background:${CLASS_COLORS[k]}"></span>${CLASS_NAMES[k]}</div>`)
      .join("");
  }

  // ---- 地图 ----
  const map = L.map("map").setView([22.54, 113.95], 12);
  const osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "© OpenStreetMap",
  });
  const esri = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "© Esri, Maxar, Earthstar Geographics" }
  );
  osm.addTo(map);

  // before 放默认 overlayPane；after 放独立 pane 以便卷帘裁剪
  map.createPane("afterPane");
  map.getPane("afterPane").style.zIndex = 500;

  const drawnItems = new L.FeatureGroup();
  map.addLayer(drawnItems);
  const drawControl = new L.Control.Draw({
    draw: { rectangle: true, polygon: false, circle: false, marker: false, polyline: false, circlemarker: false },
    edit: { featureGroup: drawnItems, remove: true },
  });
  map.addControl(drawControl);

  map.on(L.Draw.Event.CREATED, (e) => {
    // 保证同一时刻只有一个选区：新矩形替换旧矩形
    drawnItems.clearLayers();
    drawnItems.addLayer(e.layer);
    setBboxInputs(e.layer);
    hideMapTip();
  });
  map.on(L.Draw.Event.EDITED, (e) => {
    e.layers.eachLayer((l) => setBboxInputs(l));
    hideMapTip();
  });
  map.on(L.Draw.Event.DELETED, () => {
    ["bbox-north", "bbox-south", "bbox-west", "bbox-east"].forEach((id) => {
      document.getElementById(id).value = "";
    });
  });

  function setBboxInputs(layer) {
    const b = layer.getBounds();
    document.getElementById("bbox-west").value = b.getWest().toFixed(5);
    document.getElementById("bbox-south").value = b.getSouth().toFixed(5);
    document.getElementById("bbox-east").value = b.getEast().toFixed(5);
    document.getElementById("bbox-north").value = b.getNorth().toFixed(5);
  }

  function readBbox() {
    const w = parseFloat(document.getElementById("bbox-west").value);
    const s = parseFloat(document.getElementById("bbox-south").value);
    const e = parseFloat(document.getElementById("bbox-east").value);
    const n = parseFloat(document.getElementById("bbox-north").value);
    if (![w, s, e, n].every((v) => isFinite(v))) return null;
    if (!(-180 <= w && w < e && e <= 180 && -90 <= s && s < n && n <= 90)) return null;
    if (e - w > 5 || n - s > 5) return null;
    return [w, s, e, n];
  }

  function hideMapTip() {
    const tip = document.getElementById("map-tip");
    if (tip) tip.style.display = "none";
  }

  function setBtnLoading(loading) {
    const btn = document.getElementById("submit-btn");
    const label = btn.querySelector(".btn-label");
    if (loading) {
      btn.disabled = true;
      if (label) label.textContent = "⏳ 处理中...";
    } else {
      btn.disabled = false;
      if (label) label.textContent = "🚀 开始分析";
    }
  }

  // ---- 任务流程 ----
  let pollTimer = null;
  let currentJobId = null;

  document.getElementById("submit-btn").addEventListener("click", async () => {
    const bbox = readBbox();
    if (!bbox) return alert("监测范围非法：请填写北/南/西/东四个数值（范围不超过 5°×5°），或在地图上绘制矩形");
    const year_before = Number(document.getElementById("year-before").value);
    const year_after = Number(document.getElementById("year-after").value);
    if (year_before === year_after) return alert("请选择两个不同的年份");

    setBtnLoading(true);
    showPanel("progress-panel");
    hidePanel("result-panel");
    setProgress(0, "提交任务...");
    clearLogs();

    try {
      const resp = await fetch(API.createJob, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bbox, year_before, year_after }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "创建任务失败");
      currentJobId = data.job_id;
      pollTimer = setInterval(poll, 1500);
      poll();
    } catch (err) {
      showError(err.message);
      setBtnLoading(false);
    }
  });

  async function poll() {
    if (!currentJobId) return;
    try {
      const resp = await fetch(API.job(currentJobId));
      if (!resp.ok) throw new Error("任务不存在或服务已重启");
      const job = await resp.json();
      setProgress(job.percent || 0, "");
      renderLogs(job.logs);
      if (job.status === "done") {
        clearInterval(pollTimer);
        pollTimer = null;
        renderResult(job.result);
        setBtnLoading(false);
      } else if (job.status === "error") {
        clearInterval(pollTimer);
        pollTimer = null;
        showError(job.error || "处理失败");
        setBtnLoading(false);
      }
    } catch (err) {
      clearInterval(pollTimer);
      pollTimer = null;
      showError(err.message);
      setBtnLoading(false);
    }
  }

  // ---- 结果渲染 ----
  let layers = { before: null, after: null, change: null, geojson: null };
  let changeLegendAdded = false;

  function renderResult(result) {
    document.getElementById("result-summary").innerHTML =
      `<p>分类影像：<b>${result.before_item}</b> → <b>${result.after_item}</b>（${result.width}×${result.height} 像素，` +
      `${result.pixel_area_m2} m²/像素，CRS ${result.crs}）</p>` +
      `<p>变化像素 <b>${result.changed_pixels.toLocaleString()}</b>（占 ${(result.change_percent * 100).toFixed(2)}%）` +
      `，约 <b>${(result.changed_pixels * result.pixel_area_m2 / 1e4).toFixed(1)}</b> 公顷。</p>`;

    document.getElementById("dl-tif").href = API.download(currentJobId, "tif");
    document.getElementById("dl-geojson").href = API.download(currentJobId, "geojson");

    const tbody = document.querySelector("#transition-table tbody");
    tbody.innerHTML = "";
    (result.top_transitions || []).forEach((t) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${t.code}</td><td>${t.transition}</td><td>${t.pixels.toLocaleString()}</td>` +
        `<td>${t.area_ha.toFixed(2)}</td><td>${(t.percent_of_change * 100).toFixed(2)}%</td>`;
      tbody.appendChild(tr);
    });

    buildResultLayers();
    showPanel("result-panel");
  }

  function buildResultLayers() {
    // 移除旧图层（重新分析时）
    Object.values(layers).forEach((l) => l && map.removeLayer(l));
    layers = { before: null, after: null, change: null, geojson: null };
    changeLegendAdded = false;
    document.getElementById("change-legend").innerHTML = "";

    layers.before = L.tileLayer(API.tiles(currentJobId, "before"), {
      maxZoom: 17, opacity: 0.95,
    });
    layers.after = L.tileLayer(API.tiles(currentJobId, "after"), {
      maxZoom: 17, opacity: 0.95, pane: "afterPane",
    });
    layers.change = L.tileLayer(API.tiles(currentJobId, "change"), {
      maxZoom: 17, opacity: 0.85,
    });

    const overlayMaps = {
      "🖼 变化前分类（before）": layers.before,
      "🖼 变化后分类（after）": layers.after,
      "🎨 变化栅格": layers.change,
    };
    const baseMaps = { "OpenStreetMap": osm, "卫星影像": esri };

    // 已有 layers control 则更新，否则新建
    if (window.layerControl) {
      window.layerControl.remove();
    }
    window.layerControl = L.control.layers(baseMaps, overlayMaps, { collapsed: false }).addTo(map);

    // 默认显示 before + 变化栅格
    layers.before.addTo(map);
    layers.change.addTo(map);

    // 加载变化矢量 GeoJSON 预览
    fetch(API.download(currentJobId, "geojson"))
      .then((r) => r.json())
      .then((data) => {
        const codes = [...new Set(data.features.map((f) => f.properties.code))].sort((a, b) => a - b);
        const palette = {};
        codes.forEach((c, i) => { palette[c] = TAB20[i % TAB20.length]; });

        layers.geojson = L.geoJSON(data, {
          style: (f) => ({
            color: "#333", weight: 0.6,
            fillColor: palette[f.properties.code], fillOpacity: 0.6,
          }),
          onEachFeature: (f, layer) => {
            const p = f.properties;
            layer.bindPopup(
              `<b>${p.transition}</b><br/>编码：${p.code}<br/>` +
              `面积：${p.area_ha.toFixed(2)} ha（${p.area_m2.toLocaleString()} m²）`
            );
          },
        });
        if (window.layerControl) {
          window.layerControl.addOverlay(layers.geojson, "🔺 变化矢量（点击查看属性）");
        }
        // 自动显示变化矢量，方便立即查看
        map.addLayer(layers.geojson);
        renderChangeLegend(palette, data.features.length);
      })
      .catch(() => console.warn("变化矢量加载失败"));

    if (drawnItems.getLayers().length) {
      map.fitBounds(drawnItems.getBounds().pad(0.3));
    }
  }

  function renderChangeLegend(palette, featureCount) {
    const el = document.getElementById("change-legend");
    el.innerHTML = `<div class="item" style="color:#888">（共 ${featureCount} 个多边形）</div>` +
      Object.keys(palette).sort((a, b) => a - b).map((code) => {
        const b = Math.floor((code - 1) / 10), a = (code - 1) % 10;
        return `<div class="item"><span class="swatch" style="background:${palette[code]}"></span>` +
          `${code} · ${CLASS_NAMES[b]} → ${CLASS_NAMES[a]}</div>`;
      }).join("");
    changeLegendAdded = true;
  }

  // ---- 卷帘对比（before / after）----
  const handle = document.getElementById("swipe-handle");
  let swipeEnabled = false;
  let afterAutoAdded = false;

  const SwipeToggle = L.Control.extend({
    options: { position: "topleft" },
    onAdd() {
      const btn = L.DomUtil.create("div", "swipe-toggle leaflet-bar");
      btn.id = "swipe-toggle-btn";
      btn.title = "卷帘对比 before/after（点击开启/关闭，拖动竖线）";
      btn.innerHTML = "⇔ 卷帘";
      L.DomEvent.on(btn, "click", toggleSwipe);
      return btn;
    },
  });
  map.addControl(new SwipeToggle());

  function toggleSwipe() {
    swipeEnabled = !swipeEnabled;
    const btn = document.getElementById("swipe-toggle-btn");
    btn.classList.toggle("active", swipeEnabled);
    if (swipeEnabled) {
      if (!layers.after) {
        // 尚无结果图层：回滚状态，避免按钮卡在开启态
        btn.classList.remove("active");
        swipeEnabled = false;
        return alert("请先完成一次分析，再进行卷帘对比");
      }
      // 变化前/后分类都保持显示（卷帘左右两侧分别显示 before / after）
      if (!map.hasLayer(layers.before)) map.addLayer(layers.before);
      if (!map.hasLayer(layers.after)) {
        map.addLayer(layers.after);
        afterAutoAdded = true;
      }
      // 自动把检测区域缩放居中到地图中央，留出边距避免与控件重叠
      if (drawnItems.getLayers().length) {
        map.fitBounds(drawnItems.getBounds().pad(0.25), { maxZoom: 16 });
      }
      handle.hidden = false;
      updateSwipe(null);
      document.addEventListener("mousemove", onSwipeMove);
    } else {
      document.removeEventListener("mousemove", onSwipeMove);
      if (afterAutoAdded && layers.after) {
        map.removeLayer(layers.after);
        afterAutoAdded = false;
      }
      handle.hidden = true;
      clearAfterClip();
    }
  }

  function onSwipeMove(e) {
    // 卷帘只在鼠标位于地图（分类图）区域时生效；离开地图范围则保持当前卷帘位置
    const container = map.getContainer();
    const rect = container.getBoundingClientRect();
    const x = e.clientX - rect.left;
    if (x < 0 || x > rect.width || e.clientY < rect.top || e.clientY > rect.bottom) return;
    updateSwipe(x);
  }

  function updateSwipe(x) {
    const paneEl = map.getPane("afterPane");
    const width = map.getContainer().clientWidth;
    const pos = x == null ? Math.floor(width / 2) : Math.max(0, Math.min(width, x));
    handle.style.left = `${pos}px`;
    const clip = `inset(0 ${width - pos}px 0 0)`;
    paneEl.style.clipPath = clip;
    paneEl.style.webkitClipPath = clip;
  }

  function clearAfterClip() {
    const paneEl = map.getPane("afterPane");
    paneEl.style.clipPath = "";
    paneEl.style.webkitClipPath = "";
  }

  // ---- 进度 / 日志 / 提示 ----
  function setProgress(pct, text) {
    document.getElementById("progress-fill").style.width = `${pct}%`;
    document.getElementById("progress-text").textContent = text ? `${pct}% · ${text}` : `${pct}%`;
  }

  function clearLogs() {
    document.getElementById("log-box").innerHTML = "";
  }

  function renderLogs(logs) {
    const box = document.getElementById("log-box");
    box.innerHTML = (logs || []).map((l) => `<div>${l.at}  ${escapeHtml(l.message)}</div>`).join("");
    box.scrollTop = box.scrollHeight;
  }

  function showError(msg) {
    const box = document.getElementById("log-box");
    box.innerHTML = `<div class="err">❌ ${escapeHtml(msg)}</div>`;
    setProgress(0, "失败");
    hidePanel("result-panel");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function showPanel(id) {
    document.getElementById(id).hidden = false;
  }
  function hidePanel(id) {
    document.getElementById(id).hidden = true;
  }
})();

