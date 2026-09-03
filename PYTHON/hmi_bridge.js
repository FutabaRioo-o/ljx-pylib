(() => {
  "use strict";

  const bound = new WeakSet();
  let lastAcquisitionStatus = "idle";

  function normalizedText(element) {
    return (element?.textContent || "").replace(/\s+/g, "").trim();
  }

  function toast(message, type = "success") {
    const colors = { success: "#20a86b", error: "#df4b4f", warning: "#ec9c16", info: "#2878df" };
    const node = document.createElement("div");
    node.textContent = message;
    Object.assign(node.style, {
      position: "fixed", top: "24px", left: "50%", transform: "translateX(-50%)",
      zIndex: "99999", padding: "12px 18px", borderRadius: "8px", color: "white",
      background: colors[type] || colors.info, boxShadow: "0 8px 24px rgba(13,43,67,.18)",
      font: '14px "Microsoft YaHei UI", sans-serif', maxWidth: "70vw", whiteSpace: "pre-wrap",
    });
    document.body.appendChild(node);
    setTimeout(() => node.remove(), type === "error" ? 5000 : 2600);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    let payload;
    try { payload = await response.json(); } catch { payload = { ok: false, error: `HTTP ${response.status}` }; }
    if (!response.ok) throw new Error(payload.error || payload.message || `HTTP ${response.status}`);
    return payload;
  }

  function findCard(title) {
    return [...document.querySelectorAll(".card")].find((card) =>
      [...card.querySelectorAll(".card-title")].some((node) => normalizedText(node) === normalizedText({ textContent: title }))
    );
  }

  function findButton(container, label) {
    return [...(container || document).querySelectorAll("button")].find((button) => normalizedText(button) === label.replace(/\s+/g, ""));
  }

  function bindButton(button, handler) {
    if (!button || bound.has(button)) return;
    bound.add(button);
    button.addEventListener("click", handler, true);
  }

  function setButtonBusy(button, busy, busyText) {
    if (!button) return;
    if (busy) {
      button.dataset.originalText = button.textContent;
      button.disabled = true;
      button.textContent = busyText;
    } else {
      button.disabled = false;
      if (button.dataset.originalText) button.textContent = button.dataset.originalText;
    }
  }

  function setKv(card, label, value) {
    if (!card) return;
    const row = [...card.querySelectorAll(".kv")].find((item) => normalizedText(item).startsWith(label));
    const target = row?.querySelector("strong");
    if (target) target.textContent = value;
  }

  function updateOverviewLaser(status) {
    const row = [...document.querySelectorAll(".el-table__row")].find((item) => normalizedText(item).includes("激光控制器"));
    if (!row) return;
    const cells = row.querySelectorAll("td");
    if (cells.length < 4) return;
    cells[1].textContent = status.online ? (status.model || "在线") : "未检测到";
    cells[2].textContent = status.online ? "正常" : "异常";
    cells[2].style.color = status.online ? "#20a86b" : "#df4b4f";
    cells[3].textContent = status.error_code || (status.online ? "—" : "LJX_OFFLINE");
    cells[3].style.color = status.online ? "#617487" : "#df4b4f";
  }

  function updateLaserCard(status) {
    const card = findCard("激光扫描模块");
    if (!card) return;
    const temperature = status.sensor_temperature == null ? "—" : `${Number(status.sensor_temperature).toFixed(2)} °C`;
    setKv(card, "温度", temperature);
    setKv(card, "错误码", status.online ? (status.error_code || "0") : (status.error_code || "LJX_OFFLINE"));
    const errorTarget = [...card.querySelectorAll(".kv")].find((item) => normalizedText(item).startsWith("错误码"))?.querySelector("strong");
    if (errorTarget) errorTarget.style.color = status.online ? "#20a86b" : "#df4b4f";
  }

  async function refreshLaserStatus() {
    try {
      const status = await api("/api/laser/status");
      updateOverviewLaser(status);
      updateLaserCard(status);
    } catch (error) {
      const status = { online: false, model: "未检测到", error_code: "LJX_OFFLINE" };
      updateOverviewLaser(status);
      updateLaserCard(status);
    }
  }

  async function singleProfile(button) {
    setButtonBusy(button, true, "读取中…");
    try {
      const result = await api("/api/laser/profile/one", { method: "POST", body: "{}" });
      const card = findCard("激光扫描模块");
      setKv(card, "有效点", `${result.valid_count} / ${result.point_count}`);
      setKv(card, "采集状态", "单帧完成");
      toast(`单帧读取完成：${result.valid_count}/${result.point_count} 个有效点`);
    } catch (error) {
      setKv(findCard("激光扫描模块"), "采集状态", "读取失败");
      toast(`单帧读取失败：${error.message}`, "error");
    } finally {
      setButtonBusy(button, false);
      refreshLaserStatus();
    }
  }

  async function startContinuous(button) {
    setButtonBusy(button, true, "启动中…");
    try {
      const result = await api("/api/laser/acquisition/start", {
        method: "POST",
        body: JSON.stringify({ profiles: 100, timeout: 15, sample_interval: 0.1 }),
      });
      lastAcquisitionStatus = result.status;
      setKv(findCard("激光扫描模块"), "采集状态", `连续采集 0/${result.requested}`);
      toast("连续采集已启动");
    } catch (error) {
      toast(`连续采集启动失败：${error.message}`, "error");
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function stopContinuous(button) {
    setButtonBusy(button, true, "停止中…");
    try {
      const result = await api("/api/laser/acquisition/stop", { method: "POST", body: "{}" });
      setKv(findCard("激光扫描模块"), "采集状态", result.stopped ? "已停止" : "未在采集");
      toast(result.stopped ? "连续采集已停止" : "当前没有运行中的连续采集", "warning");
    } catch (error) {
      toast(`停止采集失败：${error.message}`, "error");
    } finally {
      setButtonBusy(button, false);
    }
  }

  function bindLaserControls() {
    const card = findCard("激光扫描模块");
    if (!card) return;
    const one = document.getElementById("laser-single-read") || findButton(card, "单帧读取");
    const start = document.getElementById("laser-continuous-start") || findButton(card, "连续采集");
    const stop = document.getElementById("laser-continuous-stop") || findButton(card, "停止采集");
    bindButton(one, () => singleProfile(one));
    bindButton(start, () => startContinuous(start));
    bindButton(stop, () => stopContinuous(stop));
  }

  async function refreshAcquisition() {
    try {
      const result = await api("/api/laser/acquisition");
      const card = findCard("激光扫描模块");
      if (result.running) setKv(card, "采集状态", `连续采集 ${result.received}/${result.requested}`);
      if (lastAcquisitionStatus === "running" && result.status !== "running") {
        if (result.status === "succeeded") {
          setKv(card, "采集状态", "采集完成");
          toast(`连续采集完成：${result.received}/${result.requested}`);
        } else if (result.status === "failed") {
          setKv(card, "采集状态", "采集失败");
          toast(`连续采集失败：${result.error || "未知错误"}`, "error");
        } else if (result.status === "stopped") {
          setKv(card, "采集状态", "已停止");
        }
      }
      lastAcquisitionStatus = result.status;
      if (workflowState?.phase === "scanning" && ["succeeded", "failed", "stopped"].includes(result.status)) {
        const endpoint = result.status === "succeeded" ? "/api/workflow/scan/complete" : "/api/workflow/stop";
        const payload = result.status === "succeeded" ? {} : { reason: `激光采集${result.status}` };
        const workflow = await api(endpoint, { method: "POST", body: JSON.stringify(payload) });
        workflowState = workflow;
        updateWorkflowView(workflow);
      }
    } catch { /* server may be starting */ }
  }

  // PC_MAIN migration -------------------------------------------------------
  // These bindings deliberately reuse the rendered controls in the existing
  // Vue page.  There is no secondary control panel or parallel UI.
  let controllerConfig = null;
  let controllerState = { relay_1: false, relay_2: false, connected: false };
  let workflowState = { phase: "idle" };
  let lastWorkflowPhase = "idle";

  function findButtons(container, label) {
    return [...(container || document).querySelectorAll("button")]
      .filter((button) => normalizedText(button) === label.replace(/\s+/g, ""));
  }

  function bindControllerButton(button, handler) {
    if (!button || bound.has(button)) return;
    bound.add(button);
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      await handler(button);
    }, true);
  }

  function cardInputValues(card) {
    return [...(card?.querySelectorAll("input") || [])]
      .filter((input) => !input.disabled)
      .map((input) => Number(input.value));
  }

  function requiredNumber(value, label) {
    if (!Number.isFinite(value)) throw new Error(`${label}必须是数字`);
    return value;
  }

  function scaledInteger(value, scale, label, { allowZero = false } = {}) {
    const input = requiredNumber(value, label);
    const multiplier = requiredNumber(Number(scale), `${label}换算系数`);
    const result = Math.round(Math.abs(input) * multiplier);
    if (!allowZero && result === 0) throw new Error(`${label}不能为 0`);
    return result;
  }

  function setMotorStatus(card, text, type = "info") {
    const tag = card?.querySelector(".motor-head .el-tag");
    if (!tag) return;
    tag.textContent = text;
    tag.style.color = type === "error" ? "#df4b4f" : type === "running" ? "#20a86b" : "";
  }

  async function controllerApi(path, body, button, busyText) {
    setButtonBusy(button, true, busyText);
    try {
      const result = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
      controllerState = result;
      updateMainControllerView(result);
      return result;
    } finally {
      setButtonBusy(button, false);
    }
  }

  function updateWorkflowView(state) {
    const phase = state?.phase || "idle";
    document.body.dataset.workflowPhase = phase;
    if (phase !== lastWorkflowPhase) {
      const labels = {
        ready: "自检完成，可以开始任务",
        scan_waiting: "扫描任务等待启动",
        scan_positioning: "扫描任务正在定位",
        scanning: "扫描任务正在运行",
        scan_complete: "扫描任务完成",
        stress_positioning: "应力消除正在定位",
        stress_running: "应力消除正在运行",
        stopped: "自动任务已安全停止",
        fault: state.last_error || "自动任务异常",
      };
      if (labels[phase]) toast(labels[phase], phase === "fault" ? "error" : phase === "stopped" ? "warning" : "info");
      lastWorkflowPhase = phase;
    }
  }

  async function workflowApi(path, body, button, busyText) {
    setButtonBusy(button, true, busyText);
    try {
      const result = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
      workflowState = result;
      updateWorkflowView(result);
      await refreshMainController();
      return result;
    } finally {
      setButtonBusy(button, false);
    }
  }

  function updateTableRow(name, value, status, code = "—") {
    const row = [...document.querySelectorAll(".el-table__row")]
      .find((item) => normalizedText(item).startsWith(name.replace(/\s+/g, "")));
    const cells = row?.querySelectorAll("td");
    if (!cells || cells.length < 4) return;
    cells[1].textContent = value;
    cells[2].textContent = status;
    cells[2].style.color = status === "正常" ? "#20a86b" : "#df4b4f";
    cells[3].textContent = code;
  }

  function updateImuCard(telemetry) {
    const card = findCard("姿态传感器 IMU");
    if (!card || !telemetry) return;
    if (telemetry.roll != null) setKv(card, "Roll", `${Number(telemetry.roll).toFixed(2)}°`);
    if (telemetry.pitch != null) setKv(card, "Pitch", `${Number(telemetry.pitch).toFixed(2)}°`);
    if (telemetry.yaw != null) setKv(card, "Yaw", `${Number(telemetry.yaw).toFixed(2)}°`);
  }

  function updateRelayRow(card, index, enabled) {
    const row = card?.querySelectorAll(".relay-row")[index];
    if (!row) return;
    const tag = row.querySelector(".el-tag");
    if (tag) {
      tag.textContent = `反馈 ${enabled ? "开启" : "关闭"}`;
      tag.style.color = enabled ? "#20a86b" : "";
    }
    const switchButton = row.querySelector("button.el-switch, [role='switch'], .el-switch");
    if (switchButton) {
      switchButton.setAttribute("aria-checked", String(enabled));
      switchButton.classList.toggle("is-checked", enabled);
    }
  }

  function updateMainControllerView(state) {
    const configured = Boolean(controllerConfig?.main_controller?.enabled);
    const online = Boolean(state.connected);
    const endpoint = state.host ? `${state.host}:${state.port}` : "未配置";
    const code = state.last_error || (configured ? "—" : "未启用");
    updateTableRow("PCB 通信", endpoint, online ? "正常" : "异常", code);
    updateTableRow("A / B / C 电机", online ? "控制器在线" : "控制器离线", online ? "正常" : "异常", code);
    const telemetry = state.telemetry || {};
    const battery = telemetry.battery_adc == null ? "—" : `${telemetry.battery_adc} ADC`;
    updateTableRow("电池与急停", battery, online ? "正常" : "异常", code);
    updateTableRow(
      "姿态传感器",
      telemetry.received_at ? "数据已更新" : "等待上报",
      telemetry.received_at ? "正常" : "异常",
      telemetry.received_at ? "—" : code,
    );
    updateImuCard(telemetry);
    setMotorStatus(findCard("环向运动电机 A"), state.rs485_running ? "运行" : "停止", state.rs485_running ? "running" : "info");
    const relayCard = findCard("外部模块与继电器");
    // PC_MAIN only has two relay outputs.  They map to the two shock-gun rows.
    updateRelayRow(relayCard, 1, Boolean(state.relay_1));
    updateRelayRow(relayCard, 2, Boolean(state.relay_2));
  }

  async function refreshMainController() {
    try {
      if (!controllerConfig) controllerConfig = await api("/api/main-controller/config");
      const result = await api("/api/main-controller/status");
      controllerState = result;
      updateMainControllerView(result);
    } catch { /* the server may still be starting */ }
  }

  async function refreshWorkflow() {
    try {
      const result = await api("/api/workflow");
      workflowState = result;
      updateWorkflowView(result);
    } catch { /* server may still be starting */ }
  }

  async function connectMainController(button) {
    try {
      await controllerApi("/api/main-controller/connect", {}, button, "连接中…");
      toast("整机控制器已连接");
    } catch (error) {
      toast(`整机控制器连接失败：${error.message}`, "error");
    }
  }

  async function moveAxisA(card, direction, button) {
    try {
      const [target, speed] = cardInputValues(card);
      const scale = controllerConfig?.motion?.a_position_scale ?? 1;
      const position = scaledInteger(target, scale, "A 轴目标位置") * direction;
      if (Number.isFinite(speed)) {
        await controllerApi("/api/main-controller/motors/rs485/speed", { speed: Math.round(speed) }, button, "设置速度…");
      }
      await controllerApi("/api/main-controller/motors/rs485/position", { position }, button, "发送中…");
      setMotorStatus(card, "运行", "running");
      toast(`A 轴位置指令已发送：${position}`);
    } catch (error) {
      setMotorStatus(card, "指令失败", "error");
      toast(`A 轴指令失败：${error.message}`, "error");
    }
  }

  async function moveAxisBC(card, motor, direction, button) {
    try {
      const [target] = cardInputValues(card);
      const prefix = motor === 1 ? "b" : "c";
      const steps = scaledInteger(target, controllerConfig?.motion?.[`${prefix}_steps_per_unit`] ?? 1, `${prefix.toUpperCase()} 轴目标位置`) * direction;
      const speed = Number(controllerConfig?.motion?.[`${prefix}_default_speed`] ?? 8000);
      await controllerApi(`/api/main-controller/motors/${motor}`, { steps, speed }, button, "发送中…");
      setMotorStatus(card, "运行", "running");
      toast(`${prefix.toUpperCase()} 轴步进指令已发送：${steps} 脉冲`);
    } catch (error) {
      setMotorStatus(card, "指令失败", "error");
      toast(`电机指令失败：${error.message}`, "error");
    }
  }

  async function stopRs485(button, label = "RS485 电机") {
    try {
      await controllerApi("/api/main-controller/motors/rs485/run", { running: false }, button, "停止中…");
      setMotorStatus(findCard("环向运动电机 A"), "停止");
      toast(`${label}已发送停止指令`, "warning");
    } catch (error) {
      toast(`停止失败：${error.message}`, "error");
    }
  }

  async function safeStopWorkflow(button, reason) {
    try {
      await workflowApi("/api/workflow/stop", { reason }, button, "停机中…");
    } catch (error) {
      toast(`安全停机失败：${error.message}`, "error");
    }
  }

  async function startScanWorkflow(button) {
    try {
      await workflowApi("/api/workflow/scan/start", {}, button, "任务启动中…");
    } catch (error) {
      toast(`扫描任务启动失败：${error.message}`, "error");
    }
  }

  async function startStressWorkflow(button) {
    try {
      await workflowApi("/api/workflow/stress/start", {}, button, "任务启动中…");
    } catch (error) {
      toast(`应力消除启动失败：${error.message}`, "error");
    }
  }

  function legacyUnsupported(button, message) {
    bindControllerButton(button, async () => toast(message, "warning"));
  }

  function bindMotorControls() {
    const axisA = findCard("环向运动电机 A");
    const axisB = findCard("激光推杆电机 B");
    const axisC = findCard("矫顽力推杆电机 C");
    if (axisA) {
      const buttons = axisA.querySelectorAll(".motor-actions button");
      bindControllerButton(buttons[0], (button) => moveAxisA(axisA, 1, button));
      bindControllerButton(buttons[1], (button) => moveAxisA(axisA, -1, button));
      bindControllerButton(buttons[2], (button) => stopRs485(button, "A 轴"));
      legacyUnsupported(buttons[3], "旧 PC_MAIN 协议没有“设零”指令，需在新下位机协议中补充后才能启用。");
    }
    [[axisB, 1], [axisC, 2]].forEach(([card, motor]) => {
      if (!card) return;
      const buttons = card.querySelectorAll(".motor-actions button");
      bindControllerButton(buttons[0], (button) => moveAxisBC(card, motor, 1, button));
      bindControllerButton(buttons[1], (button) => moveAxisBC(card, motor, -1, button));
      legacyUnsupported(buttons[2], "旧 PC_MAIN 协议未定义该推杆电机的独立停止指令。");
      legacyUnsupported(buttons[3], "旧 PC_MAIN 协议未定义推杆电机回原点指令。");
    });
  }

  function bindRelayControls() {
    const card = findCard("外部模块与继电器");
    if (!card) return;
    const rows = card.querySelectorAll(".relay-row");
    [[rows[1], "relay_1"], [rows[2], "relay_2"]].forEach(([row, key]) => {
      const switchButton = row?.querySelector("button.el-switch, [role='switch'], .el-switch");
      bindControllerButton(switchButton, async (button) => {
        try {
          const next = !Boolean(controllerState[key]);
          const payload = {
            relay_1: key === "relay_1" ? next : Boolean(controllerState.relay_1),
            relay_2: key === "relay_2" ? next : Boolean(controllerState.relay_2),
          };
          await controllerApi("/api/main-controller/relays", payload, button, "设置中…");
          toast(`继电器 ${key === "relay_1" ? "1" : "2"} 已${next ? "开启" : "关闭"}`);
        } catch (error) {
          toast(`继电器设置失败：${error.message}`, "error");
        }
      });
    });
  }

  function bindExistingMainControls() {
    const overview = findCard("设备连接与自检");
    bindControllerButton(findButton(overview, "连接设备"), connectMainController);
    bindControllerButton(findCard("PCB 通信") && findButton(findCard("PCB 通信"), "测试连接"), connectMainController);
    bindControllerButton(findButton(document, "正常停止"), (button) => safeStopWorkflow(button, "正常停止"));
    bindControllerButton(findButton(document, "急停"), (button) => safeStopWorkflow(button, "急停"));
    bindControllerButton(findButton(overview, "开始自检"), async (button) => {
      try {
        await workflowApi("/api/workflow/preflight", {}, button, "自检中…");
        await refreshLaserStatus();
      } catch (error) {
        toast(`自检失败：${error.message}`, "error");
      }
    });
    bindControllerButton(findButton(document, "开始扫描任务"), startScanWorkflow);
    bindControllerButton(findButton(document, "启动应力消除"), startStressWorkflow);
    bindMotorControls();
    bindRelayControls();
  }

  const observer = new MutationObserver(() => {
    bindLaserControls();
    bindExistingMainControls();
  });

  function start() {
    observer.observe(document.body, { childList: true, subtree: true });
    bindLaserControls();
    bindExistingMainControls();
    refreshLaserStatus();
    refreshAcquisition();
    refreshMainController();
    refreshWorkflow();
    setInterval(refreshLaserStatus, 3000);
    setInterval(refreshAcquisition, 1000);
    setInterval(refreshMainController, 1000);
    setInterval(refreshWorkflow, 1000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
