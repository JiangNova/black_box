/**
 * 黑盒 2.0 · 遥测大屏
 * ─────────────────────────────────────────────
 *   1. UI 结构层
 *   2. WebSocket 通信层
 *   3. ECharts 渲染层
 *   4. 历史复盘层（WebSocket 推流回放引擎）
 */
const MAX_LIVE_POINTS = 50;
const MAX_REPLAY_POINTS = 50000;  // 回放期间缓冲区上限（足够容纳 ~40 分钟 20Hz 数据）
const WS_URL = "ws://127.0.0.1:8000/ws";
const RECONNECT_MS = 2000;

/* ── 运行模式状态 ──────────────────────────────────── */
let _currentMode = "MANUAL";
let isPaused = false;
let _chartMaxPoints = MAX_LIVE_POINTS;  // 动态上限：实时 50 / 回放 50000

function _applyMode(mode) {
  if (_currentMode === mode) return;
  _currentMode = mode;
  UI.setModeUI(mode);
  UI.setControlMasked(mode === "AUTO");
}

function sendModeSwitch(target) {
  if (Replay.enabled) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn("WebSocket 未连接，无法切换模式:", target);
    return;
  }
  ws.send(JSON.stringify({ type: "mode_switch", target }));
}

/* ═══════════════════════════════════════════════════════════
   1. UI 结构层
   ═══════════════════════════════════════════════════════════ */

const UI = {
  cards: {
    steer: document.getElementById("valSteer"),
    throttle: document.getElementById("valThrottle"),
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
  pause: {
    btn: document.getElementById("pauseToggle"),
    dot: document.getElementById("pauseDot"),
    label: document.getElementById("pauseLabel"),
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
    const lidarFront = data.lidar?.front_m;
    const runMode = data.chassis?.run_mode;

    this.cards.steer.textContent =
      speed != null ? `${Number(speed).toFixed(3)} m/s` : "—";
    this.cards.throttle.textContent =
      lidarFront != null ? `${Number(lidarFront).toFixed(2)} m` : "—";

    const mode = runMode ?? "MANUAL";
    if (mode !== _currentMode) _applyMode(mode);

    this.updateEuler(data.chassis);
  },

  updateFromColumnar(columnar) {
    const i = (columnar.count ?? 0) - 1;
    if (i < 0) return;

    this.cards.steer.textContent = `${Number(columnar.speed_mps[i]).toFixed(3)} m/s`;
    this.cards.throttle.textContent = `${Number(columnar.lidar_front_m[i]).toFixed(2)} m`;

    const mode = columnar.run_modes?.[i] ?? "MANUAL";
    if (mode !== _currentMode) _applyMode(mode);

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
      label.textContent = Replay.active
        ? "已连接 · 回放推流中"
        : Replay.enabled
          ? "已连接 · 复盘就绪"
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

  // ── 画布暂停 / 回放态 ──────────────────────────────

  setPauseUI(paused) {
    if (paused) {
      this.pause.btn.classList.add("paused");
      this.pause.dot.classList.add("paused");
      this.pause.label.textContent = "画布渲染：已暂停";
    } else {
      this.pause.btn.classList.remove("paused");
      this.pause.dot.classList.remove("paused");
      this.pause.label.textContent = "画布渲染：实时";
    }
  },

  setReplayPlayingUI(active) {
    if (active) {
      this.pause.btn.classList.add("paused");
      this.pause.dot.classList.add("paused");
      this.pause.label.textContent = "正在回放...";
      // 回放期间 pause 按钮不可点击
      this.pause.btn.style.pointerEvents = "none";
    } else {
      this.pause.btn.classList.remove("paused");
      this.pause.dot.classList.remove("paused");
      this.pause.btn.style.pointerEvents = "";
      this.pause.label.textContent = isPaused
        ? "画布渲染：已暂停"
        : "画布渲染：实时";
    }
  },

  flashControlButton(action) {
    const btn = this.controlButtons[action];
    if (!btn) return;
    btn.classList.add("active");
    clearTimeout(btn._flashTimer);
    btn._flashTimer = setTimeout(() => btn.classList.remove("active"), 200);
  },

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

  setModeUI(mode) {
    document.querySelectorAll(".mode-btn").forEach((btn) => {
      btn.classList.toggle("mode-active", btn.dataset.mode === mode);
    });
  },

  setControlMasked(masked) {
    const el = document.getElementById("controlMask");
    if (el) el.classList.toggle("visible", masked);
  },

  setAebAlert(active) {
    document.body.classList.toggle("aeb-alert", active);
    const banner = document.getElementById("aebBanner");
    if (banner) banner.classList.toggle("visible", active);
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
    try {
      const data = JSON.parse(ev.data);

      // ── 回放控制信令 ──────────────────────────────
      if (data.type != null && data.timestamp == null) {
        handleReplaySignal(data);
        return;
      }

      // ── 遥测帧（实时或回放，三通道格式完全一致）───
      if (data.timestamp != null) {
        // 实时模式下，复盘 checkbox 未勾选 → 正常渲染
        // 回放模式下，Replay.active → 正常渲染
        // 复盘模式但未激活 → 过渡态，跳过（防止混杂数据）
        if (Replay.active || !Replay.enabled) {
          onTelemetry(data);
        }
        return;
      }
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

function handleReplaySignal(data) {
  switch (data.type) {
    case "replay_started":
      Replay._onStarted(data);
      break;
    case "replay_complete":
      Replay._onComplete(data);
      break;
    case "replay_stopped":
      Replay._onStopped();
      break;
    case "replay_error":
      console.error("回放错误:", data.message);
      Replay._onStopped();
      break;
    default:
      break;
  }
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
  // 数据缓冲区始终更新
  Charts.pushLivePoint(
    data.timestamp,
    Number(data.chassis?.speed_mps),
    Number(data.chassis?.gyro_z_rads)
  );

  // 画布暂停：底层数据仍在流转，但冻结 setOption 重绘
  if (isPaused) return;

  UI.updateCards(data);
  UI.setAebAlert(!!data.chassis?.aeb_active);
  const lidarData = data.lidar?.lidar_360;
  Charts.updateLidarPolar(lidarData);
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
    this.initLidarPolar();

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

  initLidarPolar() {
    this.chartLidar.setOption({
      backgroundColor: "transparent",
      animation: false,
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(17, 24, 32, 0.95)",
        borderColor: "#1e2a3a",
        textStyle: { color: "#e6edf3", fontSize: 12 },
        formatter: (params) => {
          const d = params.data;
          if (!d) return "";
          return `angle: ${d[0]}°<br/>dist: ${d[1]} m`;
        },
      },
      polar: {
        center: ["50%", "52%"],
        radius: "78%",
      },
      angleAxis: {
        type: "value",
        min: 0,
        max: 360,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
      },
      radiusAxis: {
        type: "value",
        min: 0,
        max: 3.0,
        axisLine: { lineStyle: { color: "#1e2a3a" } },
        axisTick: { show: false },
        splitLine: {
          lineStyle: { color: "#1e2a3a", type: "dashed", width: 1 },
        },
        axisLabel: { color: "#6b7b8c", fontSize: 10 },
      },
      series: [
        {
          id: "lidarScatter",
          type: "scatter",
          coordinateSystem: "polar",
          symbolSize: 3,
          silent: true,
          progressive: 0,
          itemStyle: { color: "#00d4aa" },
          data: [],
        },
      ],
    });
  },

  updateLidarPolar(lidar360) {
    const data = lidar360 && Array.isArray(lidar360) ? lidar360 : [];
    this.chartLidar.setOption(
      {
        series: [
          {
            id: "lidarScatter",
            data,
          },
        ],
      },
      false, false
    );
  },

  pushLivePoint(tsUs, speedMps, gyroZRads) {
    this.labels.push(this.formatTsUs(tsUs));
    this.speedData.push(speedMps);
    this.gyroData.push(gyroZRads);

    // 按当前动态上限裁剪缓冲区
    while (this.labels.length > _chartMaxPoints) {
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

  /**
   * 清空图表缓冲区，为回放推流做好准备。
   * 保持 live 模式不变，让逐帧推流走 refreshLive 路径。
   */
  prepareForReplay() {
    this.labels = [];
    this.speedData = [];
    this.gyroData = [];
    _chartMaxPoints = MAX_REPLAY_POINTS;

    // 扩展 y 轴范围以适应历史数据可能的波动
    this.chartSteer.setOption({
      yAxis: { min: "dataMin", max: "dataMax", scale: true },
      dataZoom: [],
    });
    this.chartGyro.setOption({
      yAxis: { min: "dataMin", max: "dataMax", scale: true },
      dataZoom: [],
    });

    this.updateLidarPolar([]);
    UI.resetEuler();
  },

  /**
   * 回放完成后，冻结数据不再接收新点，添加 dataZoom 供用户探索。
   */
  finalizeReplay() {
    _chartMaxPoints = MAX_REPLAY_POINTS;  // 保持大缓冲

    const replayBottom = 56;
    const finalizeOption = {
      grid: { bottom: replayBottom },
      dataZoom: this.dataZoomConfig(),
    };

    this.chartSteer.setOption(finalizeOption);
    this.chartGyro.setOption(finalizeOption);
  },

  /**
   * 回放结束回到实时：重置缓冲区上限，恢复实时选项。
   */
  restoreLive() {
    _chartMaxPoints = MAX_LIVE_POINTS;
    this.labels = [];
    this.speedData = [];
    this.gyroData = [];
    this.applyLiveLineOptions();
    this.refreshLive();
    this.updateLidarPolar([]);
    UI.resetEuler();
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

    // lidar_360 点云在历史数据中按帧渲染
    const lastIdx = (columnar.lidar_360?.length ?? 0) - 1;
    if (lastIdx >= 0) {
      this.updateLidarPolar(columnar.lidar_360[lastIdx]);
    } else {
      this.updateLidarPolar([]);
    }

    UI.updateFromColumnar(columnar);
  },

  resetToLive() {
    this.mode = "live";
    _chartMaxPoints = MAX_LIVE_POINTS;
    this.labels = [];
    this.speedData = [];
    this.gyroData = [];
    this.applyLiveLineOptions();
    this.refreshLive();
    this.updateLidarPolar([]);
    UI.resetEuler();
  },
};

/* ═══════════════════════════════════════════════════════════
   4. 历史复盘层（WebSocket 推流回放引擎）
   ═══════════════════════════════════════════════════════════ */

const Replay = {
  enabled: false,       // 复盘 checkbox 是否勾选
  active: false,        // 是否正在接收回放帧
  _currentRunId: null,
  _frameCount: 0,

  init() {
    // ── 页面加载时自动拉取会话列表 ────────────────────
    this.fetchSessions();

    // ── 复盘 checkbox ─────────────────────────────────
    UI.replay.toggle.addEventListener("change", () => {
      this.setEnabled(UI.replay.toggle.checked);
    });

    // ── 下拉菜单 change → 触发 / 停止回放 ────────────
    UI.replay.select.addEventListener("change", () => {
      const runId = UI.replay.select.value;
      if (runId) {
        this._requestReplay(Number(runId));
      } else {
        this._cancelReplay();
      }
    });

    // ── 导出按钮 ─────────────────────────────────────
    UI.replay.exportBtn.addEventListener("click", async () => {
      // ── 复盘模式：导出当前选定批次 ──────────────────
      if (this._currentRunId != null) {
        window.location.href = `/api/runs/${this._currentRunId}/export`;
        return;
      }
      // ── 实时模式：自动导出最新批次 ──────────────────
      try {
        const res = await fetch("/api/sessions");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        const sessions = body.sessions ?? [];
        if (sessions.length === 0) {
          alert("暂无数据批次可导出，请先采集遥测数据。");
          return;
        }
        const latestId = sessions[0].session_id;
        window.location.href = `/api/runs/${latestId}/export`;
      } catch (err) {
        console.error("导出失败:", err);
        alert("导出请求失败，请检查后端服务是否正常运行。");
      }
    });
  },

  // ── 复盘模式开关 ────────────────────────────────────

  async setEnabled(on) {
    this.enabled = on;
    UI.setReplayUI(on);

    if (on) {
      UI.setConnState(ws?.readyState === WebSocket.OPEN ? "online" : "offline");
      // 刷新一次会话列表，确保最新
      await this.fetchSessions();
    } else {
      // 关闭复盘：若正在回放则停止，恢复实时
      if (this.active) {
        this._sendStop();
      }
      this._resetState();
      Charts.restoreLive();
      UI.setConnState(ws?.readyState === WebSocket.OPEN ? "online" : "offline");
    }
  },

  // ── 会话列表 ────────────────────────────────────────

  async fetchSessions() {
    UI.replay.select.innerHTML = '<option value="">— 加载批次列表 —</option>';
    UI.replay.select.disabled = true;

    try {
      const res = await fetch("/api/sessions");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const body = await res.json();
      const sessions = body.sessions ?? [];

      UI.replay.select.innerHTML = '<option value="">— 选择实验批次 —</option>';
      for (const s of sessions) {
        const opt = document.createElement("option");
        opt.value = s.session_id;
        const mode = s.run_mode ?? "—";
        const count = s.sample_count ?? 0;
        opt.textContent = `#${s.session_id} · ${s.start_time} · ${mode} · ${count} pts`;
        UI.replay.select.appendChild(opt);
      }
    } catch (err) {
      console.error("拉取 sessions 失败:", err);
      UI.replay.select.innerHTML = '<option value="">— 加载失败 —</option>';
    } finally {
      UI.replay.select.disabled = !this.enabled;
    }
  },

  // ── 回放控制（WebSocket 信令）────────────────────────

  _requestReplay(runId) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn("WebSocket 未连接，无法发起回放");
      return;
    }

    // 清空图表缓冲区，准备接收回放帧
    Charts.prepareForReplay();
    this._frameCount = 0;
    this._currentRunId = runId;

    ws.send(JSON.stringify({
      type: "replay_start",
      run_id: runId,
      speed: "1x",
    }));

    UI.replay.status.textContent = `⏳ 请求回放 #${runId}...`;
    UI.replay.status.classList.add("visible");
  },

  _cancelReplay() {
    this._sendStop();
    this._resetState();

    if (this.enabled) {
      // 复盘模式下取消回放：回到就绪态，保留图表数据
      Charts.finalizeReplay();
    } else {
      // 非复盘模式：完全恢复实时
      Charts.restoreLive();
    }
  },

  _sendStop() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "replay_stop" }));
    }
  },

  _resetState() {
    this.active = false;
    this._currentRunId = null;
    this._frameCount = 0;
    UI.replay.status.classList.remove("visible");
    UI.setReplayPlayingUI(false);
  },

  // ── 回放信令回调（由 handleReplaySignal 调用）───────

  _onStarted(data) {
    this.active = true;
    this._currentRunId = data.run_id;
    UI.setReplayPlayingUI(true);
    UI.replay.status.textContent = `⏳ 回放中 · #${data.run_id}`;
    UI.replay.status.classList.add("visible");
    UI.setConnState("online");
    console.log("🚀 回放开始: run_id=%s speed=%s", data.run_id, data.speed);
  },

  _onComplete(data) {
    this.active = false;
    this._frameCount = data.total_frames ?? 0;
    UI.setReplayPlayingUI(false);
    UI.replay.status.textContent = `✅ 回放完毕 · ${this._frameCount} 帧`;
    UI.replay.status.classList.add("visible");
    UI.setConnState("online");

    // 添加 dataZoom，方便用户探索历史数据
    Charts.finalizeReplay();
    console.log("✅ 回放完成: %s 帧", this._frameCount);
  },

  _onStopped() {
    this._resetState();
    UI.setConnState(ws?.readyState === WebSocket.OPEN ? "online" : "offline");

    if (this.enabled) {
      Charts.finalizeReplay();
    } else {
      Charts.restoreLive();
    }
    console.log("⏹ 回放已停止");
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

function initModeSwitcher() {
  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode;
      if (mode === _currentMode) return;
      _applyMode(mode);
      sendModeSwitch(mode);
    });
  });
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

  // ── 键盘盲操 + 人类接管 ─────────────────────────────
  document.addEventListener("keydown", (e) => {
    if (_isFormElement(e.target)) return;

    const action = KEY_MAP[e.code];
    if (!action) return;

    e.preventDefault();
    if (e.repeat) return;

    const isDirection = action !== "E_STOP";
    if (isDirection && (_currentMode === "SEMI_AUTO" || _currentMode === "AUTO")) {
      _applyMode("MANUAL");
      sendModeSwitch("MANUAL");
    }

    _pressedKeys.add(e.code);
    UI.setKeyActive(action, true);
    sendControl(action);
  });

  document.addEventListener("keyup", (e) => {
    if (!_pressedKeys.has(e.code)) return;
    _pressedKeys.delete(e.code);

    const action = KEY_MAP[e.code];
    if (action) {
      UI.setKeyActive(action, false);
      // 通知后端按键已释放 → 物理引擎进入滑行衰减
      sendControl(action + "_UP");
    }
  });

  window.addEventListener("blur", () => {
    for (const code of _pressedKeys) {
      const action = KEY_MAP[code];
      if (action) {
        UI.setKeyActive(action, false);
        sendControl(action + "_UP");
      }
    }
    _pressedKeys.clear();
  });
}

/* ═══════════════════════════════════════════════════════════
   画布暂停 / 汇报模式
   ═══════════════════════════════════════════════════════════ */

function initPauseToggle() {
  const btn = document.getElementById("pauseToggle");
  const dot = document.getElementById("pauseDot");
  const label = document.getElementById("pauseLabel");

  btn.addEventListener("click", () => {
    // 回放进行中时，暂停按钮不可操作（已通过 pointerEvents 禁用）
    if (Replay.active) return;

    isPaused = !isPaused;

    if (isPaused) {
      btn.classList.add("paused");
      dot.classList.add("paused");
      label.textContent = "画布渲染：已暂停";
    } else {
      btn.classList.remove("paused");
      dot.classList.remove("paused");
      label.textContent = "画布渲染：实时";
    }
  });
}

/* ═══════════════════════════════════════════════════════════
   启动入口
   ═══════════════════════════════════════════════════════════ */

Charts.init();
Replay.init();
initModeSwitcher();
initControlPanel();
initPauseToggle();
connect();
