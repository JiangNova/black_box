/**
 * 黑盒 2.0 · 遥测大屏
 * ─────────────────────────────────────────────
 *   1. UI 结构层
 *   2. WebSocket 通信层
 *   3. ECharts 渲染层
 *   4. 历史复盘层
 */

const LIDAR_MAX_M = 5;
const MAX_LIVE_POINTS = 50;
const WS_URL = "ws://127.0.0.1:8000/ws";
const RECONNECT_MS = 2000;

/* ═══════════════════════════════════════════════════════════
   1. UI 结构层
   ═══════════════════════════════════════════════════════════ */

const UI = {
  cards: {
    steer: document.getElementById("valSteer"),
    throttle: document.getElementById("valThrottle"),
    status: document.getElementById("valStatus"),
  },
  euler: {
    yaw: document.getElementById("valYaw"),
    pitch: document.getElementById("valPitch"),
    roll: document.getElementById("valRoll"),
  },
  conn: {
    dot: document.getElementById("connDot"),
    label: document.getElementById("connLabel"),
  },
  replay: {
    toggle: document.getElementById("replayToggle"),
    toggleLabel: document.getElementById("replayToggleLabel"),
    select: document.getElementById("replaySelect"),
    status: document.getElementById("replayStatus"),
    exportBtn: document.getElementById("exportBtn"),
  },
  controlButtons: {},

  registerControlButton(action, el) {
    this.controlButtons[action] = el;
  },

  formatEuler(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(2) : "0.00";
  },

  updateEuler(imu) {
    this.euler.yaw.textContent = this.formatEuler(imu?.yaw);
    this.euler.pitch.textContent = this.formatEuler(imu?.pitch);
    this.euler.roll.textContent = this.formatEuler(imu?.roll);
  },

  resetEuler() {
    this.updateEuler({});
  },

  updateCards(data) {
    const speed = data.chassis?.speed_mps;
    const lidarFront = data.perception?.lidar_zones_m?.front;
    const runMode = data.run_mode;

    this.cards.steer.textContent =
      speed != null ? `${Number(speed).toFixed(3)} m/s` : "—";
    this.cards.throttle.textContent =
      lidarFront != null ? `${Number(lidarFront).toFixed(2)} m` : "—";

    const mode = runMode ?? "—";
    this.cards.status.textContent = mode;
    this.cards.status.className =
      "value " + (mode === "MANUAL" ? "status-ok" : "status-warn");

    this.updateEuler(data.imu);
  },

  updateFromColumnar(columnar) {
    const i = (columnar.count ?? 0) - 1;
    if (i < 0) return;

    this.cards.steer.textContent = `${Number(columnar.speed_mps[i]).toFixed(3)} m/s`;
    this.cards.throttle.textContent = `${Number(columnar.lidar_front_m[i]).toFixed(2)} m`;

    const mode = columnar.run_modes?.[i] ?? "—";
    this.cards.status.textContent = mode;
    this.cards.status.className =
      "value " + (mode === "MANUAL" ? "status-ok" : "status-warn");

    this.updateEuler({
      yaw: columnar.imu_yaw[i],
      pitch: columnar.imu_pitch[i],
      roll: columnar.imu_roll[i],
    });
  },

  setConnState(state) {
    const { dot, label } = this.conn;
    dot.classList.remove("online", "connecting");

    if (state === "online") {
      dot.classList.add("online");
      label.textContent = Replay.enabled
        ? "已连接 · 复盘模式中"
        : "已连接 · 实时接收";
    } else if (state === "connecting") {
      dot.classList.add("connecting");
      label.textContent = "连接中…";
    } else {
      label.textContent = "未连接";
    }
  },

  setReplayUI(active) {
    this.replay.toggleLabel.classList.toggle("active", active);
    this.replay.select.disabled = !active;
    this.replay.status.classList.toggle("visible", active);
  },

  flashControlButton(action) {
    const btn = this.controlButtons[action];
    if (!btn) return;
    btn.classList.add("active");
    clearTimeout(btn._flashTimer);
    btn._flashTimer = setTimeout(() => btn.classList.remove("active"), 200);
  },

  // 键盘按下/抬起专用：保持 active 直到 keyup，避免与 flash timeout 冲突
  setKeyActive(action, active) {
    const btn = this.controlButtons[action];
    if (!btn) return;
    clearTimeout(btn._flashTimer);
    if (active) {
      btn.classList.add("active");
    } else {
      btn.classList.remove("active");
    }
  },
};

/* ═══════════════════════════════════════════════════════════
   2. WebSocket 通信层
   ═══════════════════════════════════════════════════════════ */

let ws = null;
let reconnectTimer = null;

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  UI.setConnState("connecting");
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    UI.setConnState("online");
    ws.send("ping");
  };

  ws.onmessage = (ev) => {
    if (Replay.enabled) return;

    try {
      const data = JSON.parse(ev.data);
      if (data.timestamp_us == null) return;
      onTelemetry(data);
    } catch {
      console.warn("无效 JSON:", ev.data);
    }
  };

  ws.onclose = () => {
    UI.setConnState("offline");
    scheduleReconnect();
  };

  ws.onerror = () => {
    UI.setConnState("offline");
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_MS);
}

function sendControl(action) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn("WebSocket 未连接，无法发送:", action);
    return;
  }
  ws.send(JSON.stringify({ type: "control", action }));
}

function onTelemetry(data) {
  Charts.pushLivePoint(
    data.timestamp_us,
    Number(data.chassis?.speed_mps),
    Number(data.imu?.gyro_z_rads)
  );
  UI.updateCards(data);
  Charts.updateLidarRadar(
    data.perception?.lidar_zones_m?.front,
    data.perception?.lidar_zones_m?.left,
    data.perception?.lidar_zones_m?.right
  );
  Charts.refreshLive();
}

/* ═══════════════════════════════════════════════════════════
   3. ECharts 渲染层
   ═══════════════════════════════════════════════════════════ */

const Charts = {
  mode: "live",
  labels: [],
  speedData: [],
  gyroData: [],

  chartSteer: null,
  chartGyro: null,
  chartLidar: null,

  formatTsUs(us) {
    const ms = Math.floor(us / 1000);
    const d = new Date(ms);
    const h = String(d.getHours()).padStart(2, "0");
    const m = String(d.getMinutes()).padStart(2, "0");
    const s = String(d.getSeconds()).padStart(2, "0");
    const frac = String(Math.floor((us % 1_000_000) / 1000)).padStart(3, "0");
    return `${h}:${m}:${s}.${frac}`;
  },

  dataZoomConfig() {
    return [
      {
        type: "inside",
        xAxisIndex: 0,
        filterMode: "filter",
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
      },
      {
        type: "slider",
        xAxisIndex: 0,
        height: 18,
        bottom: 6,
        borderColor: "#1e2a3a",
        backgroundColor: "rgba(17, 24, 32, 0.8)",
        fillerColor: "rgba(0, 212, 170, 0.15)",
        handleStyle: { color: "#00d4aa", borderColor: "#00d4aa" },
        textStyle: { color: "#6b7b8c", fontSize: 10 },
      },
    ];
  },

  lineSeries(name, color, extra = {}) {
    return {
      name,
      type: "line",
      smooth: true,
      symbol: "none",
      sampling: "lttb",
      large: true,
      lineStyle: { width: 2, color },
      itemStyle: { color },
      data: [],
      ...extra,
    };
  },

  baseLineOption(bottom = 40) {
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 52, right: 24, top: 36, bottom },
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(17, 24, 32, 0.95)",
        borderColor: "#1e2a3a",
        textStyle: { color: "#e6edf3", fontSize: 12 },
      },
      xAxis: {
        type: "category",
        data: [],
        boundaryGap: false,
        axisLine: { lineStyle: { color: "#1e2a3a" } },
        axisLabel: { color: "#6b7b8c", fontSize: 10, rotate: 25 },
        splitLine: { show: false },
      },
    };
  },

  init() {
    this.chartSteer = echarts.init(document.getElementById("chartControl"));
    this.chartGyro = echarts.init(document.getElementById("chartGyro"));
    this.chartLidar = echarts.init(document.getElementById("chartLidar"));

    this.applyLiveLineOptions();
    this.initLidarRadar();
    this.updateLidarRadar(null, null, null);

    window.addEventListener("resize", () => {
      this.chartSteer.resize();
      this.chartGyro.resize();
      this.chartLidar.resize();
    });
  },

  applyLiveLineOptions() {
    this.mode = "live";

    this.chartSteer.setOption({
      ...this.baseLineOption(40),
      legend: {
        data: ["speed_mps"],
        textStyle: { color: "#6b7b8c" },
        top: 4,
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 2.5,
        axisLine: { show: false },
        axisLabel: { color: "#6b7b8c", fontSize: 11 },
        splitLine: { lineStyle: { color: "#1e2a3a", type: "dashed" } },
      },
      dataZoom: [],
      series: [
        this.lineSeries("speed_mps", "#3b9eff", {
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: "#6b7b8c", type: "dashed", width: 1 },
            label: {
              formatter: "参考 1.25 m/s",
              color: "#6b7b8c",
              fontSize: 10,
            },
            data: [{ yAxis: 1.25 }],
          },
        }),
      ],
    });

    this.chartGyro.setOption({
      ...this.baseLineOption(40),
      legend: {
        data: ["gyro_z_rads"],
        textStyle: { color: "#6b7b8c" },
        top: 4,
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLine: { show: false },
        axisLabel: { color: "#6b7b8c", fontSize: 11 },
        splitLine: { lineStyle: { color: "#1e2a3a", type: "dashed" } },
      },
      dataZoom: [],
      series: [
        this.lineSeries("gyro_z_rads", "#ffb020", {
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: "rgba(255, 176, 32, 0.25)" },
              { offset: 1, color: "rgba(255, 176, 32, 0)" },
            ]),
          },
        }),
      ],
    });
  },

  initLidarRadar() {
    this.chartLidar.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(17, 24, 32, 0.95)",
        borderColor: "#1e2a3a",
        textStyle: { color: "#e6edf3", fontSize: 12 },
      },
      radar: {
        center: ["50%", "55%"],
        radius: "62%",
        indicator: [
          { name: "Front", max: LIDAR_MAX_M },
          { name: "Left", max: LIDAR_MAX_M },
          { name: "Right", max: LIDAR_MAX_M },
        ],
        axisName: { color: "#6b7b8c", fontSize: 11 },
        splitLine: { lineStyle: { color: "#1e2a3a" } },
        splitArea: { show: false },
        axisLine: { lineStyle: { color: "#1e2a3a" } },
      },
      series: [
        {
          type: "radar",
          symbol: "circle",
          symbolSize: 4,
          lineStyle: { width: 2, color: "#00d4aa" },
          itemStyle: { color: "#00d4aa" },
          areaStyle: { color: "rgba(0, 212, 170, 0.18)" },
          data: [{ value: [LIDAR_MAX_M, LIDAR_MAX_M, LIDAR_MAX_M], name: "lidar_zones_m" }],
        },
      ],
    });
  },

  normalizeLidar(value) {
    const n = Number(value);
    return Number.isFinite(n) ? Math.min(Math.max(n, 0), LIDAR_MAX_M) : LIDAR_MAX_M;
  },

  updateLidarRadar(front, left, right) {
    const f = this.normalizeLidar(front);
    const l = this.normalizeLidar(left);
    const r = this.normalizeLidar(right);

    this.chartLidar.setOption({
      series: [{ data: [{ value: [f, l, r], name: "lidar_zones_m" }] }],
    });
  },

  pushLivePoint(tsUs, speedMps, gyroZRads) {
    this.labels.push(this.formatTsUs(tsUs));
    this.speedData.push(speedMps);
    this.gyroData.push(gyroZRads);

    if (this.labels.length > MAX_LIVE_POINTS) {
      this.labels.shift();
      this.speedData.shift();
      this.gyroData.shift();
    }
  },

  refreshLive() {
    if (this.mode !== "live") return;

    this.chartSteer.setOption({
      xAxis: { data: this.labels },
      series: [{ data: this.speedData }],
    });
    this.chartGyro.setOption({
      xAxis: { data: this.labels },
      series: [{ data: this.gyroData }],
    });
  },

  loadHistory(columnar) {
    this.mode = "replay";
    this.labels = (columnar.timestamps ?? []).map((ts) => this.formatTsUs(ts));
    this.speedData = columnar.speed_mps ?? [];
    this.gyroData = columnar.gyro_z_rads ?? [];

    const replayBottom = 56;
    const replayOption = {
      grid: { bottom: replayBottom },
      dataZoom: this.dataZoomConfig(),
      xAxis: { data: this.labels },
      yAxis: { min: "dataMin", max: "dataMax", scale: true },
    };

    this.chartSteer.setOption({
      ...replayOption,
      series: [
        {
          name: "speed_mps",
          type: "line",
          smooth: true,
          symbol: "none",
          sampling: "lttb",
          large: true,
          lineStyle: { width: 2, color: "#3b9eff" },
          itemStyle: { color: "#3b9eff" },
          data: this.speedData,
        },
      ],
    });

    this.chartGyro.setOption({
      ...replayOption,
      series: [
        {
          name: "gyro_z_rads",
          type: "line",
          smooth: true,
          symbol: "none",
          sampling: "lttb",
          large: true,
          lineStyle: { width: 2, color: "#ffb020" },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: "rgba(255, 176, 32, 0.25)" },
              { offset: 1, color: "rgba(255, 176, 32, 0)" },
            ]),
          },
          itemStyle: { color: "#ffb020" },
          data: this.gyroData,
        },
      ],
    });

    const last = (columnar.count ?? 0) - 1;
    if (last >= 0) {
      this.updateLidarRadar(
        columnar.lidar_front_m?.[last],
        columnar.lidar_left_m?.[last],
        columnar.lidar_right_m?.[last]
      );
    }

    UI.updateFromColumnar(columnar);
  },

  resetToLive() {
    this.labels = [];
    this.speedData = [];
    this.gyroData = [];
    this.applyLiveLineOptions();
    this.refreshLive();
    this.updateLidarRadar(null, null, null);
    UI.resetEuler();
  },
};

/* ═══════════════════════════════════════════════════════════
   4. 历史复盘层
   ═══════════════════════════════════════════════════════════ */

const Replay = {
  enabled: false,
  loading: false,
  _currentRunId: null,

  init() {
    UI.replay.toggle.addEventListener("change", () => {
      this.setEnabled(UI.replay.toggle.checked);
    });

    UI.replay.select.addEventListener("change", () => {
      const runId = UI.replay.select.value;
      if (runId) {
        this.loadRun(Number(runId));
      } else {
        this._currentRunId = null;
        UI.replay.exportBtn.disabled = true;
      }
    });

    UI.replay.exportBtn.addEventListener("click", () => {
      if (this._currentRunId == null) return;
      window.location.href = `/api/runs/${this._currentRunId}/export`;
    });
  },

  async setEnabled(on) {
    this.enabled = on;
    UI.setReplayUI(on);

    if (on) {
      UI.setConnState(ws?.readyState === WebSocket.OPEN ? "online" : "offline");
      await this.fetchRuns();
    } else {
      this._currentRunId = null;
      UI.replay.select.value = "";
      UI.replay.exportBtn.disabled = true;
      Charts.resetToLive();
      UI.setConnState(ws?.readyState === WebSocket.OPEN ? "online" : "offline");
    }
  },

  async fetchRuns() {
    UI.replay.select.innerHTML = '<option value="">— 加载批次列表 —</option>';
    UI.replay.select.disabled = true;

    try {
      const res = await fetch("/api/runs");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const body = await res.json();
      const runs = body.runs ?? [];

      UI.replay.select.innerHTML = '<option value="">— 选择实验批次 —</option>';
      for (const run of runs) {
        const opt = document.createElement("option");
        opt.value = run.run_id;
        const mode = run.run_mode ?? "—";
        const count = run.sample_count ?? 0;
        opt.textContent = `#${run.run_id} · ${run.start_time} · ${mode} · ${count} pts`;
        UI.replay.select.appendChild(opt);
      }
    } catch (err) {
      console.error("拉取 runs 失败:", err);
      UI.replay.select.innerHTML = '<option value="">— 加载失败 —</option>';
    } finally {
      UI.replay.select.disabled = !this.enabled;
    }
  },

  async loadRun(runId) {
    if (this.loading) return;
    this.loading = true;

    try {
      const res = await fetch(`/api/runs/${runId}/telemetry`);
      if (res.status === 404) {
        console.warn(`Run ${runId} 不存在`);
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const columnar = await res.json();
      Charts.loadHistory(columnar);
      this._currentRunId = runId;
      UI.replay.exportBtn.disabled = false;
    } catch (err) {
      console.error("拉取 telemetry 失败:", err);
    } finally {
      this.loading = false;
    }
  },
};

/* ═══════════════════════════════════════════════════════════
   线控面板 + 全局键盘盲操
   ═══════════════════════════════════════════════════════════ */

const KEY_MAP = {
  KeyW: "FORWARD",
  KeyS: "BACKWARD",
  KeyA: "LEFT",
  KeyD: "RIGHT",
  KeyE: "E_STOP",
};

const _pressedKeys = new Set();

function _isFormElement(el) {
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

function initControlPanel() {
  // ── 鼠标点击 ──────────────────────────────────────────
  document.querySelectorAll("[data-action]").forEach((btn) => {
    const action = btn.dataset.action;
    UI.registerControlButton(action, btn);
    btn.addEventListener("click", () => {
      UI.flashControlButton(action);
      sendControl(action);
    });
  });

  // ── 键盘盲操 ──────────────────────────────────────────
  document.addEventListener("keydown", (e) => {
    // 焦点在表单元素上时跳过，避免与复盘下拉框等冲突
    if (_isFormElement(e.target)) return;

    const action = KEY_MAP[e.code];
    if (!action) return;

    e.preventDefault();
    if (e.repeat) return;

    _pressedKeys.add(e.code);
    UI.setKeyActive(action, true);
    sendControl(action);
  });

  document.addEventListener("keyup", (e) => {
    if (!_pressedKeys.has(e.code)) return;
    _pressedKeys.delete(e.code);

    const action = KEY_MAP[e.code];
    if (action) UI.setKeyActive(action, false);
  });

  // 窗口失焦时清理所有按键状态，防止按钮"卡住"
  window.addEventListener("blur", () => {
    for (const code of _pressedKeys) {
      const action = KEY_MAP[code];
      if (action) UI.setKeyActive(action, false);
    }
    _pressedKeys.clear();
  });
}

/* ═══════════════════════════════════════════════════════════
   启动入口
   ═══════════════════════════════════════════════════════════ */

Charts.init();
Replay.init();
initControlPanel();
connect();
