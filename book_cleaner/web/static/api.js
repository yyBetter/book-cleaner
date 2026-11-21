/** Shared HTTP client, safe DOM helpers, Safari closedby dialog fallback, and Web Audio cues. */

export async function apiJson(url, options = {}) {
  const opts = { ...options };
  if (opts.body && !(opts.body instanceof FormData) && typeof opts.body === "object") {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(url, opts);
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!resp.ok) {
    const msg = (data && (data.detail || data.message)) || `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return data;
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

export function enableDialogLightDismissFallback(dialog) {
  if (!dialog) return;
  if (!("closedBy" in HTMLDialogElement.prototype)) {
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      const inside =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!inside) dialog.close();
    });
  }
}

let _audioCtx = null;

export function playBeep(kind = "ok") {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!_audioCtx) _audioCtx = new Ctx();
    if (_audioCtx.state === "suspended") _audioCtx.resume();
    const osc = _audioCtx.createOscillator();
    const gain = _audioCtx.createGain();
    osc.connect(gain);
    gain.connect(_audioCtx.destination);
    if (kind === "warn") {
      osc.type = "triangle";
      osc.frequency.setValueAtTime(320, _audioCtx.currentTime);
      osc.frequency.setValueAtTime(240, _audioCtx.currentTime + 0.09);
      gain.gain.setValueAtTime(0.14, _audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + 0.22);
      osc.start();
      osc.stop(_audioCtx.currentTime + 0.23);
    } else {
      osc.type = "sine";
      osc.frequency.setValueAtTime(880, _audioCtx.currentTime);
      gain.gain.setValueAtTime(0.08, _audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + 0.09);
      osc.start();
      osc.stop(_audioCtx.currentTime + 0.1);
    }
  } catch {
    // Ignore audio context errors on restricted environments
  }
}

export function showToast(message, level = "ok") {
  const stack = document.getElementById("toastStack");
  if (!stack) return;
  const node = el("div", { class: `toast is-${level}`, text: message });
  stack.appendChild(node);
  setTimeout(() => node.remove(), 3600);
}

const FLAG_LABELS = {
  blurry: "图像偏糊",
  too_dark: "曝光过暗",
  overexposed: "高光过曝",
  duplicate_page: "疑似重复页",
  low_ocr_confidence: "OCR置信度偏低",
  empty_ocr: "未识别出文字",
};

export function translateFlag(flag) {
  return FLAG_LABELS[flag] || flag;
}

const STATUS_LABELS = {
  STAGING: "暂存中",
  CAPTURED: "已采待洗",
  CLEANING: "清洗中",
  CLEANED: "已洗待OCR",
  OCR_RUNNING: "OCR识别中",
  OCR_DONE: "待精校",
  LLM_RUNNING: "LLM精校中",
  LLM_DONE: "精校完成",
  READY: "待抽检",
  NEEDS_REVIEW: "需人工复核",
  APPROVED: "已通过",
  FAILED: "失败",
};

export function translateStatus(status) {
  return STATUS_LABELS[status] || status;
}
