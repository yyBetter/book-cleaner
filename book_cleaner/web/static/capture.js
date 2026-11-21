/** Capture tab: backend OpenCV camera, browser getUserMedia, Android USB/Wi-Fi pairing, drag-drop & folder import. */

import {
  apiJson,
  createStatusIcon,
  el,
  playBeep,
  showToast,
  translateFlag,
  withButtonLoading,
} from "/static/api.js";
import { HandsOnSmartTrigger } from "/static/smart_trigger.js";

export function createCaptureController(state, callbacks) {
  let activeSource = "backend_cam";
  let browserStream = null;
  let browserMotionTimer = null;
  let browserCaptureBusy = false;

  const browserSmartTrigger = new HandsOnSmartTrigger({
    enabled: false,
    handsOnAssist: true,
    marginRatio: 0.16,
    settleMs: 380,
    cooldownMs: 1200,
  });

  const sourceBtns = Array.from(document.querySelectorAll(".source-btn"));
  const srcPanels = {
    backend_cam: document.getElementById("src-backend_cam"),
    browser_cam: document.getElementById("src-browser_cam"),
    mobile_qr: document.getElementById("src-mobile_qr"),
    folder_import: document.getElementById("src-folder_import"),
  };

  const backendCamSelect = document.getElementById("backendCamSelect");
  const backendCamFeed = document.getElementById("backendCamFeed");
  const backendCamPlaceholder = document.getElementById("backendCamPlaceholder");
  const backendCamHudLeft = document.getElementById("backendCamHudLeft");
  const backendAutoTriggerChk = document.getElementById("backendAutoTriggerChk");
  const backendHandsOnChk = document.getElementById("backendHandsOnChk");
  const backendCenterGuide = document.getElementById("backendCenterGuide");
  const backendSettleFill = document.getElementById("backendSettleFill");
  const captureDropZone = document.getElementById("captureDropZone");

  const browserCamSelect = document.getElementById("browserCamSelect");
  const browserVideo = document.getElementById("browserVideo");
  const browserCamPlaceholder = document.getElementById("browserCamPlaceholder");
  const browserAutoTriggerChk = document.getElementById("browserAutoTriggerChk");
  const browserHandsOnChk = document.getElementById("browserHandsOnChk");
  const browserCenterGuide = document.getElementById("browserCenterGuide");
  const browserSettleFill = document.getElementById("browserSettleFill");
  const browserMotionHud = document.getElementById("browserMotionHud");
  const browserResHud = document.getElementById("browserResHud");

  const mobileQrImg = document.getElementById("mobileQrImg");
  const mobileUrlText = document.getElementById("mobileUrlText");
  const mobileUsbUrlText = document.getElementById("mobileUsbUrlText");
  const mobileHttpsBadge = document.getElementById("mobileHttpsBadge");
  const copyMobileUrlBtn = document.getElementById("copyMobileUrlBtn");
  const adbStatusBox = document.getElementById("adbStatusBox");
  const recentStrip = document.getElementById("recentCaptureStrip");

  const profileEls = {
    split_spread: document.getElementById("optSplitSpread"),
    auto_crop: document.getElementById("optAutoCrop"),
    dewarp: document.getElementById("optDewarp"),
    remove_fingers: document.getElementById("optRemoveFingers"),
    deskew: document.getElementById("optDeskew"),
    denoise: document.getElementById("optDenoise"),
    enhance_mode: document.getElementById("optEnhanceMode"),
  };

  function currentProfile() {
    return {
      split_spread: profileEls.split_spread.checked,
      auto_crop: profileEls.auto_crop.checked,
      dewarp: profileEls.dewarp.checked,
      remove_fingers: profileEls.remove_fingers.checked,
      deskew: profileEls.deskew.checked,
      denoise: profileEls.denoise.checked,
      enhance_mode: profileEls.enhance_mode.value,
    };
  }

  function applyBookProfile(profile = {}) {
    profileEls.split_spread.checked = Boolean(profile.split_spread);
    profileEls.auto_crop.checked = profile.auto_crop !== false;
    profileEls.dewarp.checked = profile.dewarp !== false;
    profileEls.remove_fingers.checked = profile.remove_fingers !== false;
    profileEls.deskew.checked = profile.deskew !== false;
    profileEls.denoise.checked = profile.denoise !== false;
    profileEls.enhance_mode.value = profile.enhance_mode || "clean_color";
  }

  function triggerFlashOverlay(id) {
    const node = document.getElementById(id);
    if (!node) return;
    node.classList.remove("is-flashing");
    void node.offsetWidth;
    node.classList.add("is-flashing");
  }

  function updateQaHud(qa, splitCount = 1) {
    const summaryEl = document.getElementById("captureQaSummary");
    const metricsEl = document.getElementById("captureQaMetrics");
    const lastHud = document.getElementById("lastQaHud");
    const blurEl = document.getElementById("qaValBlur");
    const brightEl = document.getElementById("qaValBright");
    const dupEl = document.getElementById("qaValDup");
    if (!qa || qa.blur_score === undefined) return;
    const flags = qa.flags || [];
    const ok = flags.length === 0;
    const label = ok
      ? `质检通过（${splitCount > 1 ? "已拆分为双页" : "已入队后台清洗"}）`
      : `告警：${flags.map(translateFlag).join("、")}`;
    summaryEl.textContent = label;
    summaryEl.style.color = ok ? "var(--ok)" : "var(--warn)";
    metricsEl.textContent = `清晰度: ${qa.blur_score} · 亮度: ${qa.brightness} · 重复帧: ${qa.is_duplicate ? "是" : "否"}`;
    if (blurEl) blurEl.textContent = String(qa.blur_score);
    if (brightEl) brightEl.textContent = String(qa.brightness);
    if (dupEl) {
      dupEl.textContent = qa.is_duplicate ? "重复" : "唯一";
      dupEl.style.color = qa.is_duplicate ? "var(--warn)" : "var(--ok)";
    }
    lastHud.textContent = `最近质检：${label}`;

    if (document.getElementById("beepToggleChk").checked) {
      playBeep(ok ? "ok" : "warn");
    }
  }

  function pushRecentPage(page) {
    state.recentCaptured.unshift(page);
    if (state.recentCaptured.length > 16) state.recentCaptured.length = 16;
    renderRecentStrip();
  }

  function renderRecentStrip() {
    recentStrip.replaceChildren();
    if (!state.recentCaptured.length) {
      recentStrip.appendChild(
        el("div", { class: "muted-note", style: "padding:8px 0;", text: "暂无本轮拍摄记录（支持拍照、拖拽或选择文件）" })
      );
      return;
    }
    for (const p of state.recentCaptured) {
      const hasWarn = Boolean(p.qa_flags && p.qa_flags.length);
      const chip = el(
        "button",
        {
          class: "thumb-chip",
          type: "button",
          onclick: () => callbacks.onJumpToReview(p.id),
        },
        [
          el("img", { src: `/api/pages/${p.id}/image/thumb?t=${encodeURIComponent(p.updated_at || "")}`, alt: `P${p.seq_no}` }),
          el("div", { class: "thumb-chip-meta" }, [
            el("span", { text: `#${p.seq_no}${p.spread_side !== "S" ? p.spread_side : ""}` }),
            createStatusIcon(hasWarn),
          ]),
        ]
      );
      recentStrip.appendChild(chip);
    }
  }

  async function uploadBlob(blob, filename, sourceType) {
    if (!state.currentBookId) return;
    const fd = new FormData();
    fd.append("file", blob, filename);
    fd.append("source_type", sourceType);
    const prof = currentProfile();
    for (const [k, v] of Object.entries(prof)) {
      fd.append(k, String(v));
    }
    const res = await apiJson(`/api/books/${state.currentBookId}/capture`, {
      method: "POST",
      body: fd,
    });
    updateQaHud(res.qa, res.split_count);
    for (const pg of res.pages || []) {
      pushRecentPage(pg);
    }
    callbacks.onCaptured();
    return res;
  }

  async function captureBrowserFrame(sourceType = "browser_cam") {
    if (!browserStream || !browserVideo.videoWidth) {
      throw new Error("浏览器摄像头未启动");
    }
    if (sourceType === "browser_cam") {
      browserSmartTrigger.markManualCapture(browserVideo);
    }
    const canvas = document.createElement("canvas");
    canvas.width = browserVideo.videoWidth;
    canvas.height = browserVideo.videoHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(browserVideo, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.94));
    return uploadBlob(blob, `cam_${Date.now()}.jpg`, sourceType);
  }

  async function triggerShutter() {
    if (!state.currentBookId) {
      showToast("请先选择或创建书目", "warn");
      return;
    }
    try {
      if (activeSource === "browser_cam" && browserStream) {
        triggerFlashOverlay("browserShutterFlash");
        await captureBrowserFrame("browser_cam");
      } else {
        triggerFlashOverlay("backendShutterFlash");
        const res = await apiJson(`/api/books/${state.currentBookId}/capture-camera`, {
          method: "POST",
          body: { profile_override: currentProfile() },
        });
        updateQaHud(res.qa, res.split_count);
        for (const pg of res.pages || []) {
          pushRecentPage(pg);
        }
        callbacks.onCaptured();
      }
    } catch (err) {
      showToast(err.message, "warn");
    }
  }

  async function refreshMobileQr() {
    if (!state.currentBookId) return;
    try {
      const info = await apiJson(`/api/mobile/info?book_id=${state.currentBookId}`);
      mobileUrlText.textContent = info.url;
      mobileUsbUrlText.textContent = `USB 手机直连地址：${info.usb_url}`;
      mobileHttpsBadge.textContent = info.is_https ? "HTTPS 实时连拍已就绪" : "HTTP 原生相机模式";
      mobileHttpsBadge.className = `pill ${info.is_https ? "pill-ok" : "pill-warn"}`;
      mobileQrImg.src = `/api/mobile/qr?book_id=${state.currentBookId}&_=${Date.now()}`;
    } catch (err) {
      mobileUrlText.textContent = `获取配对地址失败: ${err.message}`;
    }
  }

  if (copyMobileUrlBtn) {
    copyMobileUrlBtn.addEventListener("click", async () => {
      const txt = mobileUrlText.textContent.trim();
      if (!txt || txt.startsWith("正在读取") || txt.startsWith("获取配对")) return;
      try {
        await navigator.clipboard.writeText(txt);
        showToast("已复制手机连拍地址", "ok");
      } catch {
        showToast(`地址：${txt}`, "ok");
      }
    });
  }

  async function refreshAdbStatus() {
    try {
      const st = await apiJson("/api/android/adb-status");
      adbStatusBox.textContent = st.message || "-";
    } catch (err) {
      adbStatusBox.textContent = `ADB 状态查询失败: ${err.message}`;
    }
  }

  document.getElementById("refreshAdbBtn").addEventListener("click", refreshAdbStatus);
  const adbReverseBtn = document.getElementById("adbReverseBtn");
  adbReverseBtn.addEventListener("click", async () => {
    await withButtonLoading(adbReverseBtn, "打通通道中...", async () => {
      try {
        const res = await apiJson("/api/android/adb-reverse", {
          method: "POST",
          body: { book_id: state.currentBookId, open_on_phone: true },
        });
        adbStatusBox.textContent = res.message;
        showToast(res.message, res.ok ? "ok" : "warn");
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });

  function switchSource(srcKey) {
    activeSource = srcKey;
    sourceBtns.forEach((b) => b.classList.toggle("is-active", b.dataset.source === srcKey));
    Object.entries(srcPanels).forEach(([k, panel]) => panel.classList.toggle("hidden", k !== srcKey));
    if (srcKey === "mobile_qr") {
      refreshMobileQr();
      refreshAdbStatus();
    }
    if (srcKey === "browser_cam") populateBrowserDevices();
  }

  sourceBtns.forEach((b) => b.addEventListener("click", () => switchSource(b.dataset.source)));

  // Drag-and-drop upload in capture viewport
  if (captureDropZone) {
    ["dragenter", "dragover"].forEach((evt) => {
      captureDropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        captureDropZone.classList.add("is-dragover");
      });
    });
    ["dragleave", "drop"].forEach((evt) => {
      captureDropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        captureDropZone.classList.remove("is-dragover");
      });
    });
    captureDropZone.addEventListener("drop", async (e) => {
      const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []).filter((f) =>
        f.type.startsWith("image/")
      );
      if (!files.length) return;
      let okCount = 0;
      for (const f of files) {
        try {
          await uploadBlob(f, f.name, "upload");
          okCount += 1;
        } catch (err) {
          showToast(`${f.name}: ${err.message}`, "danger");
        }
      }
      if (okCount > 0) showToast(`已拖拽入库 ${okCount} 页图像，后台正在清洗与识别`, "ok");
    });
  }

  // Backend OpenCV Camera handlers
  async function scanBackendDevices() {
    try {
      const data = await apiJson("/api/camera/devices");
      backendCamSelect.replaceChildren();
      if (!data.devices.length) {
        backendCamSelect.appendChild(el("option", { value: "0", text: "未检测到设备 (尝试索引 0)" }));
      } else {
        for (const d of data.devices) {
          backendCamSelect.appendChild(
            el("option", { value: String(d.index), text: `${d.name} (${d.width}x${d.height})` })
          );
        }
      }
      updateBackendCamStatus(data.status);
    } catch (err) {
      showToast(err.message, "danger");
    }
  }

  function updateBackendCamStatus(st) {
    if (!st) return;
    if (st.is_open) {
      if (!backendCamFeed.src.includes("/api/camera/stream")) {
        backendCamFeed.src = `/api/camera/stream?t=${Date.now()}`;
      }
      backendCamFeed.classList.remove("hidden");
      backendCamPlaceholder.classList.add("hidden");
      backendCamHudLeft.textContent = `摄像头 #${st.active_index} (${st.width}x${st.height}) · ${st.status_label || st.auto_trigger_state}`;
      backendAutoTriggerChk.checked = Boolean(st.auto_trigger_enabled);
      backendHandsOnChk.checked = st.hands_on_assist !== false;
      backendSettleFill.style.width = `${Math.round((st.settle_progress || 0) * 100)}%`;
      backendCenterGuide.classList.remove("is-hand", "is-settling");
      if (st.auto_trigger_state === "HAND_IN_CENTER") backendCenterGuide.classList.add("is-hand");
      else if (st.auto_trigger_state === "SETTLING") backendCenterGuide.classList.add("is-settling");
    } else {
      backendCamFeed.src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
      backendCamFeed.classList.add("hidden");
      backendCamPlaceholder.classList.remove("hidden");
      backendCamHudLeft.textContent = "状态：待机";
      backendSettleFill.style.width = "0%";
      backendCenterGuide.classList.remove("is-hand", "is-settling");
    }
  }

  async function syncBackendAutoTrigger() {
    try {
      const res = await apiJson("/api/camera/auto-trigger", {
        method: "POST",
        body: {
          enabled: backendAutoTriggerChk.checked,
          hands_on_assist: backendHandsOnChk.checked,
          book_id: state.currentBookId,
        },
      });
      updateBackendCamStatus(res.status);
    } catch (err) {
      showToast(err.message, "warn");
    }
  }

  document.getElementById("scanBackendCamsBtn").addEventListener("click", scanBackendDevices);
  const openBackendCamBtn = document.getElementById("openBackendCamBtn");
  openBackendCamBtn.addEventListener("click", async () => {
    await withButtonLoading(openBackendCamBtn, "开启中...", async () => {
      try {
        const idx = Number(backendCamSelect.value || 0);
        const res = await apiJson("/api/camera/open", { method: "POST", body: { index: idx } });
        updateBackendCamStatus(res.status);
        showToast("后端摄像头预览已开启", "ok");
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });
  document.getElementById("closeBackendCamBtn").addEventListener("click", async () => {
    const res = await apiJson("/api/camera/close", { method: "POST" });
    updateBackendCamStatus(res.status);
  });
  backendAutoTriggerChk.addEventListener("change", syncBackendAutoTrigger);
  backendHandsOnChk.addEventListener("change", syncBackendAutoTrigger);

  // Browser WebRTC camera + HandsOnSmartTrigger loop
  async function populateBrowserDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    const devs = await navigator.mediaDevices.enumerateDevices();
    const cams = devs.filter((d) => d.kind === "videoinput");
    browserCamSelect.replaceChildren();
    if (!cams.length) {
      browserCamSelect.appendChild(el("option", { value: "", text: "默认摄像头" }));
    } else {
      cams.forEach((c, i) => {
        browserCamSelect.appendChild(
          el("option", { value: c.deviceId, text: c.label || `浏览器摄像头 #${i + 1}` })
        );
      });
    }
  }

  function startBrowserMotionLoop() {
    stopBrowserMotionLoop();
    browserMotionTimer = setInterval(async () => {
      if (!browserStream || !browserVideo.videoWidth || browserCaptureBusy) return;
      browserSmartTrigger.enabled = browserAutoTriggerChk.checked;
      browserSmartTrigger.handsOnAssist = browserHandsOnChk.checked;

      const res = browserSmartTrigger.step(browserVideo, null);
      browserMotionHud.textContent = res.label;
      browserSettleFill.style.width = `${Math.round((res.settleProgress || 0) * 100)}%`;
      browserCenterGuide.classList.remove("is-hand", "is-settling", "is-captured");
      if (res.state === "HAND_IN_CENTER") browserCenterGuide.classList.add("is-hand");
      else if (res.state === "SETTLING") browserCenterGuide.classList.add("is-settling");

      if (res.shouldCapture) {
        browserCenterGuide.classList.add("is-captured");
        browserCaptureBusy = true;
        try {
          await captureBrowserFrame("browser_auto");
        } catch (err) {
          showToast(err.message, "warn");
        } finally {
          browserCaptureBusy = false;
        }
      }
    }, 95);
  }

  function stopBrowserMotionLoop() {
    if (browserMotionTimer) {
      clearInterval(browserMotionTimer);
      browserMotionTimer = null;
    }
    browserSmartTrigger.reset({ clearLastHash: true });
    browserSettleFill.style.width = "0%";
    browserCenterGuide.classList.remove("is-hand", "is-settling", "is-captured");
  }

  document.getElementById("startBrowserCamBtn").addEventListener("click", async () => {
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error("当前浏览器环境不支持 getUserMedia（请通过 localhost 或 HTTPS 访问）");
      }
      if (browserStream) {
        browserStream.getTracks().forEach((t) => t.stop());
      }
      const devId = browserCamSelect.value;
      const videoConstraint = devId
        ? { deviceId: { exact: devId }, width: { ideal: 2560 }, height: { ideal: 1440 } }
        : { width: { ideal: 2560 }, height: { ideal: 1440 } };
      browserStream = await navigator.mediaDevices.getUserMedia({ video: videoConstraint, audio: false });
      browserVideo.srcObject = browserStream;
      browserVideo.classList.remove("hidden");
      browserCamPlaceholder.classList.add("hidden");
      browserVideo.onloadedmetadata = () => {
        browserResHud.textContent = `分辨率: ${browserVideo.videoWidth}x${browserVideo.videoHeight}`;
        populateBrowserDevices();
      };
      startBrowserMotionLoop();
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  document.getElementById("stopBrowserCamBtn").addEventListener("click", () => {
    stopBrowserMotionLoop();
    if (browserStream) {
      browserStream.getTracks().forEach((t) => t.stop());
      browserStream = null;
    }
    browserVideo.srcObject = null;
    browserVideo.classList.add("hidden");
    browserCamPlaceholder.classList.remove("hidden");
    browserResHud.textContent = "分辨率：-";
  });

  // Manual file upload
  document.getElementById("manualUploadInput").addEventListener("change", async (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    let okCount = 0;
    for (const f of files) {
      try {
        await uploadBlob(f, f.name, "upload");
        okCount += 1;
      } catch (err) {
        showToast(`${f.name}: ${err.message}`, "danger");
      }
    }
    if (okCount > 0) showToast(`已高速入库 ${okCount} 个文件，后台正在清洗与识别`, "ok");
    e.target.value = "";
  });

  // Folder bulk import
  const startFolderImportBtn = document.getElementById("startFolderImportBtn");
  startFolderImportBtn.addEventListener("click", async () => {
    if (!state.currentBookId) return;
    const folderPath = document.getElementById("folderPathInput").value.trim();
    if (!folderPath) {
      showToast("请填写本地目录路径", "warn");
      return;
    }
    await withButtonLoading(startFolderImportBtn, "启动导入任务...", async () => {
      try {
        const res = await apiJson(`/api/books/${state.currentBookId}/import-folder`, {
          method: "POST",
          body: {
            folder_path: folderPath,
            recursive: document.getElementById("folderRecursiveChk").checked,
            profile_override: currentProfile(),
          },
        });
        showToast(`批量导入任务 #${res.job.id} 已启动，请在「流水线大盘」查看进度`, "ok");
        callbacks.onJobsUpdated();
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });

  // Save book default clean profile
  const saveBookProfileBtn = document.getElementById("saveBookProfileBtn");
  saveBookProfileBtn.addEventListener("click", async () => {
    if (!state.currentBookId) return;
    await withButtonLoading(saveBookProfileBtn, "保存中...", async () => {
      try {
        const updated = await apiJson(`/api/books/${state.currentBookId}`, {
          method: "PATCH",
          body: { profile: currentProfile() },
        });
        callbacks.onBookUpdated(updated);
        showToast("已保存为本书默认清洗模板", "ok");
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });

  // Shutter & Undo last shot
  document.getElementById("captureShutterBtn").addEventListener("click", triggerShutter);
  document.getElementById("undoLastCaptureBtn").addEventListener("click", async () => {
    const last = state.recentCaptured[0];
    if (!last) {
      showToast("没有可撤销的最近拍摄页面", "warn");
      return;
    }
    try {
      await apiJson(`/api/pages/${last.id}`, { method: "DELETE" });
      state.recentCaptured.shift();
      browserSmartTrigger.resetLastHash();
      renderRecentStrip();
      callbacks.onCaptured();
      showToast(`已撤销第 #${last.seq_no} 页，请重新翻拍`, "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  return {
    applyBookProfile,
    refreshMobileQr,
    updateBackendCamStatus,
    triggerShutter,
    undoLast: () => document.getElementById("undoLastCaptureBtn").click(),
    renderRecentStrip,
  };
}
