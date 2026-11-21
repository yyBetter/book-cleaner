/** Shared HTTP client, safe DOM helpers, top-layer animated dialog close, theme toggle, and Web Audio cues. */

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

export function createStatusIcon(isWarn = false) {
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon icon-sm");
  svg.setAttribute("aria-hidden", "true");
  svg.style.color = isWarn ? "var(--warn)" : "var(--ok)";

  if (isWarn) {
    const poly = document.createElementNS(svgNS, "path");
    poly.setAttribute("d", "M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z");
    const line1 = document.createElementNS(svgNS, "line");
    line1.setAttribute("x1", "12");
    line1.setAttribute("y1", "9");
    line1.setAttribute("x2", "12");
    line1.setAttribute("y2", "13");
    const line2 = document.createElementNS(svgNS, "line");
    line2.setAttribute("x1", "12");
    line2.setAttribute("y1", "17");
    line2.setAttribute("x2", "12.01");
    line2.setAttribute("y2", "17");
    svg.append(poly, line1, line2);
  } else {
    const poly = document.createElementNS(svgNS, "polyline");
    poly.setAttribute("points", "20 6 9 17 4 12");
    svg.appendChild(poly);
  }
  return svg;
}

let supportsDisplayTransition;
function canTransitionDisplay() {
  if (supportsDisplayTransition !== undefined) return supportsDisplayTransition;
  if (!window.CSS?.supports?.("transition-behavior", "allow-discrete") || !document.body) {
    return false;
  }
  const probe = document.createElement("div");
  probe.style.cssText = "transition: display 1s allow-discrete !important; display: block;";
  document.body.appendChild(probe);
  getComputedStyle(probe).display;
  probe.style.display = "none";
  supportsDisplayTransition = getComputedStyle(probe).display === "block";
  probe.remove();
  return supportsDisplayTransition;
}

function supportsTopLayerExit() {
  return Boolean(window.CSS?.supports?.("overlay", "auto") && canTransitionDisplay());
}

export async function closeTopLayer(dialog) {
  if (!dialog || !dialog.open) return;
  if (dialog.hasAttribute("data-closing")) return;
  if (!supportsTopLayerExit()) {
    dialog.setAttribute("data-closing", "");
    const animations = dialog.getAnimations ? dialog.getAnimations({ subtree: true }) : [];
    if (animations.length > 0) {
      await Promise.race([
        Promise.allSettled(animations.map((a) => a.finished)),
        new Promise((r) => setTimeout(r, 1200)),
      ]);
    }
    if (!dialog.hasAttribute("data-closing")) return;
    dialog.removeAttribute("data-closing");
  }
  dialog.close();
}

export function enableDialogLightDismissFallback(dialog) {
  if (!dialog) return;
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeTopLayer(dialog);
  });
  if (!("closedBy" in HTMLDialogElement.prototype)) {
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      const inside =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!inside) closeTopLayer(dialog);
    });
  }
}

export async function withButtonLoading(btn, busyText, asyncFn) {
  if (!btn) return asyncFn();
  if (btn.disabled) return;
  const origChildren = Array.from(btn.childNodes);
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  btn.textContent = busyText;
  try {
    return await asyncFn();
  } finally {
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
    btn.replaceChildren(...origChildren);
  }
}

export function wireTwoStepAction(btn, confirmLabel, onConfirm, timeoutMs = 3000) {
  if (!btn) return;
  let timer = null;
  let origChildren = null;

  function resetArm() {
    clearTimeout(timer);
    timer = null;
    if (btn.dataset.armed === "1" && origChildren) {
      delete btn.dataset.armed;
      btn.replaceChildren(...origChildren);
      origChildren = null;
    }
  }

  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    if (btn.dataset.armed !== "1") {
      origChildren = Array.from(btn.childNodes);
      btn.dataset.armed = "1";
      btn.textContent = confirmLabel;
      timer = setTimeout(resetArm, timeoutMs);
      return;
    }
    resetArm();
    await onConfirm();
  });
}

const THEME_STORAGE_KEY = "superscanner_theme_v2";
const THEME_CYCLE = ["light", "dark", "system"];
const THEME_LABELS = {
  light: "日光工坊",
  dark: "纯黑暗房",
  system: "跟随系统",
};
const THEME_COMPACT_LABELS = {
  light: "日光",
  dark: "暗房",
  system: "系统",
};

export function initThemeToggle(btn, { compact = false } = {}) {
  const labels = compact ? THEME_COMPACT_LABELS : THEME_LABELS;
  function applyTheme(mode) {
    const valid = THEME_CYCLE.includes(mode) ? mode : "light";
    if (valid === "system") {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = valid;
    }
    const metaScheme = document.querySelector('meta[name="color-scheme"]');
    if (metaScheme) {
      metaScheme.setAttribute("content", valid === "system" ? "light dark" : valid);
    }
    try {
      localStorage.setItem(THEME_STORAGE_KEY, valid);
    } catch {
      // Ignore storage errors
    }
    if (btn) {
      btn.dataset.themeMode = valid;
      const fullLabel = THEME_LABELS[valid] || labels[valid];
      btn.title = `当前外观：${fullLabel}（点击切换）`;
      btn.setAttribute("aria-label", `切换外观（当前：${fullLabel}）`);
      if (btn.dataset.iconOnly !== "1") {
        const labelNode = btn.querySelector("[data-theme-label]");
        if (labelNode) labelNode.textContent = labels[valid];
        else btn.textContent = labels[valid];
      }
    }
  }

  let initial = "light";
  try {
    initial = localStorage.getItem(THEME_STORAGE_KEY) || "light";
  } catch {
    initial = "light";
  }
  applyTheme(initial);

  if (btn) {
    btn.addEventListener("click", () => {
      const cur = btn.dataset.themeMode || "light";
      const next = THEME_CYCLE[(THEME_CYCLE.indexOf(cur) + 1) % THEME_CYCLE.length];
      applyTheme(next);
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
  const node = el("div", { class: `toast is-${level}` }, [
    el("span", { class: "toast-badge", "aria-hidden": "true" }),
    el("span", { text: message }),
  ]);
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
