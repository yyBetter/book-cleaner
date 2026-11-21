/**
 * Android-first mobile companion: zero-storage RAM-to-PC streaming via ImageCapture.takePhoto()
 * or live video frame Blob, bounded in-memory upload queue, hands-on smart auto-capture, Torch & WakeLock.
 */

import { apiJson, createStatusIcon, el, playBeep, translateFlag } from "/static/api.js";
import { HandsOnSmartTrigger } from "/static/smart_trigger.js";

const mobileBookSelect = document.getElementById("mobileBookSelect");
const mobileBookTitle = document.getElementById("mobileBookTitle");
const mobilePageCountPill = document.getElementById("mobilePageCountPill");
const mobileSplitSpreadChk = document.getElementById("mobileSplitSpreadChk");
const mobileLiveCard = document.getElementById("mobileLiveCard");
const mobileNativeCard = document.getElementById("mobileNativeCard");
const mobileCaptureModeBadge = document.getElementById("mobileCaptureModeBadge");
const mobileQueueBadge = document.getElementById("mobileQueueBadge");
const mobileVideo = document.getElementById("mobileVideo");
const mobileCenterGuide = document.getElementById("mobileCenterGuide");
const mobileSettleFill = document.getElementById("mobileSettleFill");
const mobileSmartHud = document.getElementById("mobileSmartHud");
const mobileResHud = document.getElementById("mobileResHud");
const mobileAutoTriggerChk = document.getElementById("mobileAutoTriggerChk");
const mobileTorchBtn = document.getElementById("mobileTorchBtn");
const mobileUndoBtn = document.getElementById("mobileUndoBtn");
const mobileContextNotice = document.getElementById("mobileContextNotice");
const mobileQaResult = document.getElementById("mobileQaResult");
const mobileRecentStrip = document.getElementById("mobileRecentStrip");

let currentBookId = Number(new URLSearchParams(window.location.search).get("book_id") || 0) || null;
let liveStream = null;
let videoTrack = null;
let imageCapture = null;
let photoSettings = {};
let imageCaptureFailed = false;
let torchOn = false;
let wakeLock = null;
let smartLoopTimer = null;
let captureBusy = false;

const MAX_INFLIGHT = 2;
let inflightUploads = 0;
const pendingQueue = [];
const recentShots = [];

const smartTrigger = new HandsOnSmartTrigger({
  enabled: true,
  handsOnAssist: true,
  marginRatio: 0.16,
  settleMs: 380,
  cooldownMs: 1200,
});

function updateQueueBadge() {
  const active = inflightUploads + pendingQueue.length;
  mobileQueueBadge.textContent = active > 0 ? `传输中 ${active}` : "待传 0";
  mobileQueueBadge.className = `pill ${active > 0 ? "pill-warn" : "pill-ok"}`;
}

async function acquireWakeLock() {
  if (!("wakeLock" in navigator) || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => {
      wakeLock = null;
    });
  } catch {
    // Ignore if device power policy blocks wake lock
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && liveStream) {
    acquireWakeLock();
  }
});

async function loadBooks() {
  const data = await apiJson("/api/books");
  const books = data.items || [];
  mobileBookSelect.replaceChildren();
  for (const b of books) {
    mobileBookSelect.appendChild(el("option", { value: String(b.id), text: b.title }));
  }
  if (!currentBookId || !books.some((b) => b.id === currentBookId)) {
    currentBookId = books[0] ? books[0].id : null;
  }
  if (currentBookId) {
    mobileBookSelect.value = String(currentBookId);
    const active = books.find((b) => b.id === currentBookId);
    if (active) {
      mobileBookTitle.textContent = active.title;
      mobilePageCountPill.textContent = `${(active.stats && active.stats.total) || 0} 页`;
      mobileSplitSpreadChk.checked = Boolean(active.profile && active.profile.split_spread);
    }
  }
}

function renderRecent() {
  mobileRecentStrip.replaceChildren();
  for (const p of recentShots) {
    const hasWarn = Boolean(p.qa_flags && p.qa_flags.length);
    mobileRecentStrip.appendChild(
      el("div", { class: "thumb-chip" }, [
        el("img", { src: `/api/pages/${p.id}/image/thumb`, alt: `#${p.seq_no}` }),
        el("div", { class: "thumb-chip-meta" }, [
          el("span", { text: `#${p.seq_no}${p.spread_side !== "S" ? p.spread_side : ""}` }),
          createStatusIcon(hasWarn),
        ]),
      ])
    );
  }
}

async function performUpload(task) {
  if (!currentBookId) return;
  let fd = new FormData();
  fd.append("file", task.blob, task.filename);
  fd.append("source_type", task.sourceType);
  fd.append("split_spread", String(mobileSplitSpreadChk.checked));
  // Drop blob reference promptly so phone RAM stays constant over 100k pages
  task.blob = null;

  try {
    const res = await apiJson(`/api/books/${currentBookId}/capture`, { method: "POST", body: fd });
    fd = null;
    const qa = res.qa || {};
    const flags = qa.flags || [];
    if (flags.length === 0) {
      mobileQaResult.textContent = `已直传电脑（清晰度 ${qa.blur_score}），手机 0 占用`;
      mobileQaResult.style.color = "var(--ok)";
      playBeep("ok");
      if (navigator.vibrate) navigator.vibrate(40);
    } else {
      mobileQaResult.textContent = `注意：${flags.map(translateFlag).join("、")}，可点撤销重拍`;
      mobileQaResult.style.color = "var(--warn)";
      playBeep("warn");
      if (navigator.vibrate) navigator.vibrate([120, 60, 120]);
    }
    for (const p of res.pages || []) {
      recentShots.unshift(p);
    }
    if (recentShots.length > 8) recentShots.length = 8;
    renderRecent();
    await loadBooks();
  } catch (err) {
    mobileQaResult.textContent = `上传失败: ${err.message}`;
    mobileQaResult.style.color = "var(--danger)";
  }
}

function pumpUploadQueue() {
  updateQueueBadge();
  while (inflightUploads < MAX_INFLIGHT && pendingQueue.length > 0) {
    const nextTask = pendingQueue.shift();
    inflightUploads += 1;
    updateQueueBadge();
    performUpload(nextTask).finally(() => {
      inflightUploads = Math.max(0, inflightUploads - 1);
      pumpUploadQueue();
    });
  }
}

function enqueueUpload(blob, filename, sourceType) {
  if (!blob) return;
  if (inflightUploads + pendingQueue.length >= MAX_INFLIGHT) {
    mobileQaResult.textContent = "等待上一张直传完成，请稍慢半秒翻页...";
    mobileQaResult.style.color = "var(--warn)";
    return false;
  }
  mobileQaResult.textContent = "内存抓拍完成，正在直传电脑...";
  mobileQaResult.style.color = "var(--muted)";
  pendingQueue.push({ blob, filename, sourceType });
  pumpUploadQueue();
  return true;
}

function captureCanvasBlob() {
  return new Promise((resolve) => {
    const canvas = document.createElement("canvas");
    canvas.width = mobileVideo.videoWidth;
    canvas.height = mobileVideo.videoHeight;
    canvas.getContext("2d").drawImage(mobileVideo, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(resolve, "image/jpeg", 0.93);
  });
}

async function captureZeroStorageBlob() {
  if (imageCapture && !imageCaptureFailed) {
    try {
      const blob = await imageCapture.takePhoto(photoSettings);
      if (blob && blob.size > 0) {
        mobileCaptureModeBadge.textContent = "硬件高清静帧直传";
        mobileCaptureModeBadge.className = "pill pill-ok";
        return blob;
      }
    } catch {
      imageCaptureFailed = true;
      mobileCaptureModeBadge.textContent = "实时视频帧直传";
      mobileCaptureModeBadge.className = "pill";
    }
  }
  return captureCanvasBlob();
}

function updateGuideVisuals(res) {
  mobileSmartHud.textContent = res.label;
  mobileSettleFill.style.width = `${Math.round((res.settleProgress || 0) * 100)}%`;
  mobileCenterGuide.classList.remove("is-hand", "is-settling", "is-captured");
  if (res.state === "HAND_IN_CENTER") {
    mobileCenterGuide.classList.add("is-hand");
  } else if (res.state === "SETTLING") {
    mobileCenterGuide.classList.add("is-settling");
  } else if (res.shouldCapture) {
    mobileCenterGuide.classList.add("is-captured");
  }
}

function startSmartAutoLoop() {
  if (smartLoopTimer) clearInterval(smartLoopTimer);
  smartLoopTimer = setInterval(async () => {
    if (!liveStream || !mobileVideo.videoWidth || captureBusy) return;
    if (inflightUploads + pendingQueue.length >= MAX_INFLIGHT) return;

    smartTrigger.enabled = mobileAutoTriggerChk.checked;
    const res = smartTrigger.step(mobileVideo, null);
    updateGuideVisuals(res);

    if (res.shouldCapture) {
      captureBusy = true;
      try {
        const blob = await captureZeroStorageBlob();
        enqueueUpload(blob, `android_ram_${Date.now()}.jpg`, "mobile_auto_ram");
      } finally {
        captureBusy = false;
      }
    }
  }, 95);
}

// Plain-HTTP fallback input (hidden automatically once zero-storage live mode is active)
document.getElementById("mobileNativeCameraInput").addEventListener("change", async (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  enqueueUpload(file, file.name || `phone_${Date.now()}.jpg`, "mobile_native");
  e.target.value = "";
});

async function initHardwareImageCapture(track) {
  if (typeof window.ImageCapture !== "function") {
    mobileCaptureModeBadge.textContent = "实时视频帧直传";
    return;
  }
  try {
    imageCapture = new window.ImageCapture(track);
    const caps = await imageCapture.getPhotoCapabilities();
    if (caps && caps.imageWidth && typeof caps.imageWidth.max === "number") {
      const targetW = Math.min(3200, caps.imageWidth.max);
      photoSettings = {
        imageWidth: Math.max(caps.imageWidth.min || targetW, targetW),
      };
    }
    mobileCaptureModeBadge.textContent = "硬件高清静帧直传";
    mobileCaptureModeBadge.className = "pill pill-ok";
  } catch {
    imageCapture = null;
    mobileCaptureModeBadge.textContent = "实时视频帧直传";
  }
}

// Live Android Chrome zero-storage mode over USB (127.0.0.1) or Wi-Fi HTTPS (--auto-https)
async function maybeStartSecureLiveCamera() {
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    mobileContextNotice.textContent =
      "当前为普通局域网 HTTP：系统原生相机可能会写入手机相册临时文件。请用 USB 连接（一键打通）或运行 start_windows_https.bat 解锁「手机 0 存储」内存直传。";
    return;
  }
  try {
    liveStream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 3840 },
        height: { ideal: 2160 },
      },
      audio: false,
    });
    videoTrack = liveStream.getVideoTracks()[0] || null;
    if (videoTrack && videoTrack.getCapabilities) {
      const caps = videoTrack.getCapabilities();
      if (caps.focusMode && caps.focusMode.includes("continuous")) {
        videoTrack.applyConstraints({ advanced: [{ focusMode: "continuous" }] }).catch(() => {});
      }
      if (caps.torch) {
        mobileTorchBtn.classList.remove("hidden");
      }
    }
    if (videoTrack) {
      await initHardwareImageCapture(videoTrack);
    }
    mobileVideo.srcObject = liveStream;
    mobileVideo.onloadedmetadata = () => {
      mobileResHud.textContent = `${mobileVideo.videoWidth}x${mobileVideo.videoHeight}`;
    };
    mobileLiveCard.classList.remove("hidden");
    // Hide system-camera file input so operator strictly stays in zero-phone-storage RAM pipeline
    mobileNativeCard.classList.add("hidden");
    await acquireWakeLock();
    startSmartAutoLoop();
  } catch {
    mobileContextNotice.textContent = "未能获取摄像头权限，暂时保留系统相机备用按钮。";
  }
}

mobileAutoTriggerChk.addEventListener("change", () => {
  smartTrigger.enabled = mobileAutoTriggerChk.checked;
  smartTrigger.reset({ clearLastHash: false });
});

mobileTorchBtn.addEventListener("click", async () => {
  if (!videoTrack) return;
  torchOn = !torchOn;
  try {
    await videoTrack.applyConstraints({ advanced: [{ torch: torchOn }] });
    const labelSpan = mobileTorchBtn.querySelector("span");
    if (labelSpan) labelSpan.textContent = torchOn ? "关闭补光灯" : "开启补光灯";
    else mobileTorchBtn.textContent = torchOn ? "关闭补光灯" : "开启补光灯";
  } catch {
    torchOn = false;
  }
});

mobileUndoBtn.addEventListener("click", async () => {
  const last = recentShots[0];
  if (!last) {
    mobileQaResult.textContent = "当前没有可撤销的最近拍摄页";
    return;
  }
  try {
    await apiJson(`/api/pages/${last.id}`, { method: "DELETE" });
    recentShots.shift();
    smartTrigger.resetLastHash();
    renderRecent();
    await loadBooks();
    mobileQaResult.textContent = `已撤销第 #${last.seq_no} 页，请重新对准拍摄`;
    mobileQaResult.style.color = "var(--warn)";
  } catch (err) {
    mobileQaResult.textContent = `撤销失败: ${err.message}`;
  }
});

document.getElementById("mobileLiveShutterBtn").addEventListener("click", async () => {
  if (!liveStream || !mobileVideo.videoWidth || captureBusy) return;
  captureBusy = true;
  try {
    smartTrigger.markManualCapture(mobileVideo);
    const blob = await captureZeroStorageBlob();
    enqueueUpload(blob, `android_manual_ram_${Date.now()}.jpg`, "mobile_live_ram");
  } finally {
    captureBusy = false;
  }
});

mobileBookSelect.addEventListener("change", async () => {
  currentBookId = Number(mobileBookSelect.value);
  await loadBooks();
});

loadBooks().then(() => maybeStartSecureLiveCamera());
