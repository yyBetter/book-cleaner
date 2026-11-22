/** Pipeline monitor, interactive QA Review comparison workbench, and Export center. */

import {
  apiJson,
  el,
  enableDialogLightDismissFallback,
  renderSafeMarkdown,
  showToast,
  translateFlag,
  translateStatus,
  wireTwoStepAction,
  withButtonLoading,
} from "/static/api.js";

const copyTimers = new WeakMap();
let lastCopyMessage = "";

function legacyCopy(text) {
  const field = Object.assign(document.createElement("textarea"), { value: text });
  field.setAttribute("readonly", "");
  field.style.cssText = "position:fixed;inset-block-start:0;opacity:0";
  document.body.append(field);
  field.select();
  try {
    return document.execCommand("copy");
  } finally {
    field.remove();
  }
}

function reportCopy(button, ok) {
  const tip = button.nextElementSibling;
  clearTimeout(copyTimers.get(button));
  button.removeAttribute("data-copied");
  button.removeAttribute("data-failed");
  let message = ok ? "已复制" : "复制失败";
  if (message === lastCopyMessage) message += "\u00A0";
  lastCopyMessage = message;
  if (tip) tip.textContent = "";
  void button.offsetWidth;
  button.setAttribute(ok ? "data-copied" : "data-failed", "");
  if (tip) requestAnimationFrame(() => (tip.textContent = message));
  copyTimers.set(
    button,
    setTimeout(() => {
      button.removeAttribute("data-copied");
      button.removeAttribute("data-failed");
      if (tip) tip.textContent = "";
    }, 2000)
  );
}

async function copyFromTarget(button, target) {
  if (!button || !target) return;
  const text = target.matches("input, textarea") ? target.value : target.innerText.trim();
  if (!text) {
    reportCopy(button, false);
    return;
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else if (!legacyCopy(text)) {
      throw new Error("Copy failed");
    }
    reportCopy(button, true);
  } catch {
    try {
      reportCopy(button, legacyCopy(text));
    } catch {
      reportCopy(button, false);
    }
  }
}

if ("CommandEvent" in window) {
  document.addEventListener(
    "command",
    (event) => {
      if (event.command === "--copy") {
        copyFromTarget(event.source, event.target);
      }
    },
    { capture: true }
  );
} else {
  document.addEventListener("click", (event) => {
    const button = event.target.closest('button[command="--copy"]');
    if (!button) return;
    const target = document.getElementById(button.getAttribute("commandfor") || "");
    if (target) copyFromTarget(button, target);
  });
}

export function createReviewAndPipelineController(state, callbacks) {
  let currentFilterStatus = "ALL";
  let currentOffset = 0;
  const pageSize = 40;
  let currentTotal = 0;
  let currentList = [];
  let selectedPage = null;
  let multiSelectMode = false;
  const selectedPageIds = new Set();
  let reviewTextViewMode = "preview"; // "preview" | "edit"
  let imgMode = "clean"; // "clean" | "raw" | "split"
  let zoomScale = 1.0;
  let pinnedSyncEl = null;
  let loadingTicker = null;

  // Compact Progress Strip & Jobs Dialog elements
  const kpiTotal = document.getElementById("kpiTotal");
  const kpiCompleted = document.getElementById("kpiCompleted");
  const kpiClean = document.getElementById("kpiClean");
  const kpiOcr = document.getElementById("kpiOcr");
  const kpiReview = document.getElementById("kpiReview");
  const kpiFailed = document.getElementById("kpiFailed");
  const pipelineHeroHeadline = document.getElementById("pipelineHeroHeadline");
  const pipelineHeroSub = document.getElementById("pipelineHeroSub");
  const globalProgressNote = document.getElementById("globalProgressNote");
  const pipeStep1Meta = document.getElementById("pipeStep1Meta");
  const pipeStep2Meta = document.getElementById("pipeStep2Meta");
  const pipeStep3Meta = document.getElementById("pipeStep3Meta");
  const pipelineJobsDialog = document.getElementById("pipelineJobsDialog");
  const openJobsDialogBtn = document.getElementById("openJobsDialogBtn");
  const pipelineJobsCount = document.getElementById("pipelineJobsCount");
  const pausePipelineBtn = document.getElementById("pausePipelineBtn");
  const resumePipelineBtn = document.getElementById("resumePipelineBtn");
  const retryFailedBtn = document.getElementById("retryFailedBtn");
  const jobsTableBody = document.getElementById("jobsTableBody");
  const exportJobsBody = document.getElementById("exportJobsBody");
  enableDialogLightDismissFallback(pipelineJobsDialog);

  // Unified Proofreading Workbench elements
  const reviewGallery = document.getElementById("reviewGallery");
  const pageListPagerInfo = document.getElementById("pageListPagerInfo");
  const reviewImageTitle = document.getElementById("reviewImageTitle");
  const reviewEnginePill = document.getElementById("reviewEnginePill");
  const compareStage = document.getElementById("compareStage");
  const compareWrap = document.getElementById("compareWrap");
  const compareCleanImg = document.getElementById("compareCleanImg");
  const compareRawImg = document.getElementById("compareRawImg");
  const compareRawLayer = document.getElementById("compareRawLayer");
  const compareDivider = document.getElementById("compareDivider");
  const compareSlider = document.getElementById("compareSlider");
  const compareSliderRow = document.getElementById("compareSliderRow");
  const reviewBboxOverlay = document.getElementById("reviewBboxOverlay");
  const zoomOutBtn = document.getElementById("zoomOutBtn");
  const zoomResetBtn = document.getElementById("zoomResetBtn");
  const zoomInBtn = document.getElementById("zoomInBtn");
  const zoomToggleBtn = document.getElementById("zoomToggleBtn");
  const reviewCleanDiag = document.getElementById("reviewCleanDiag");
  const reviewConfBadge = document.getElementById("reviewConfBadge");
  const reviewAlertBanner = document.getElementById("reviewAlertBanner");
  const reviewFlagRow = document.getElementById("reviewFlagRow");
  const reviewSyncHint = document.getElementById("reviewSyncHint");
  const reviewLoadingPanel = document.getElementById("reviewLoadingPanel");
  const reviewTextEditor = document.getElementById("reviewTextEditor");
  const reviewMdPreview = document.getElementById("reviewMdPreview");
  const recleanModeSelect = document.getElementById("recleanModeSelect");
  const reocrPageBtn = document.getElementById("reocrPageBtn");
  const toggleMultiSelectBtn = document.getElementById("toggleMultiSelectBtn");
  const batchDeleteBar = document.getElementById("batchDeleteBar");
  const selectAllReviewBtn = document.getElementById("selectAllReviewBtn");
  const batchDeleteSelectedBtn = document.getElementById("batchDeleteSelectedBtn");
  const cancelMultiSelectBtn = document.getElementById("cancelMultiSelectBtn");
  const deleteFlaggedBtn = document.getElementById("deleteFlaggedBtn");

  function syncMultiSelectUi() {
    if (toggleMultiSelectBtn) {
      toggleMultiSelectBtn.textContent = multiSelectMode ? "退出多选" : "多选删废";
    }
    if (batchDeleteBar) {
      batchDeleteBar.classList.toggle("hidden", !multiSelectMode);
    }
    if (batchDeleteSelectedBtn && batchDeleteSelectedBtn.dataset.armed !== "1") {
      batchDeleteSelectedBtn.textContent = selectedPageIds.size ? `删选中 (${selectedPageIds.size})` : "删选中";
    }
  }

  function setSliderPercent(pct) {
    const clamped = Math.max(0, Math.min(100, Number(pct)));
    if (compareSlider) compareSlider.value = String(Math.round(clamped));
    if (compareRawLayer && imgMode === "split") {
      compareRawLayer.style.clipPath = `inset(0 ${100 - clamped}% 0 0)`;
    }
    if (compareDivider) compareDivider.style.left = `${clamped}%`;
  }

  if (compareSlider) {
    compareSlider.addEventListener("input", (e) => setSliderPercent(e.target.value));
  }
  document.querySelectorAll("[data-compare-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (imgMode !== "split") setImageMode("split");
      setSliderPercent(Number(btn.dataset.comparePreset));
    });
  });

  function getActivePrimaryImg() {
    return imgMode === "raw" ? compareRawImg : compareCleanImg;
  }

  function applyZoomLayout({ keepCenter = false } = {}) {
    const primary = getActivePrimaryImg();
    const isZoomed = Math.abs(zoomScale - 1.0) > 0.02;
    const isPanZoom = zoomScale > 1.02;

    if (zoomResetBtn) {
      zoomResetBtn.textContent = `${Math.round(zoomScale * 100)}%`;
    }
    if (zoomToggleBtn) {
      zoomToggleBtn.textContent = zoomScale > 1.05 ? "适应" : "放大";
    }
    if (compareWrap) {
      compareWrap.classList.toggle("is-zoomed", isZoomed);
      compareWrap.classList.toggle("is-pan-ready", imgMode !== "split" && isPanZoom);
    }
    if (compareStage) {
      compareStage.classList.toggle("is-zoomed", isPanZoom);
    }

    const prevW = compareStage ? compareStage.scrollWidth : 1;
    const prevH = compareStage ? compareStage.scrollHeight : 1;
    const prevCenterX = compareStage ? compareStage.scrollLeft + compareStage.clientWidth / 2 : 0;
    const prevCenterY = compareStage ? compareStage.scrollTop + compareStage.clientHeight / 2 : 0;

    [compareCleanImg, compareRawImg].forEach((img) => {
      if (!img) return;
      img.style.width = "";
      img.style.height = "";
      img.style.maxWidth = "";
      img.style.maxHeight = "";
    });

    if (isZoomed && compareStage && primary && primary.naturalWidth > 0 && primary.naturalHeight > 0) {
      const stageW = Math.max(240, compareStage.clientWidth - 24);
      const stageH = Math.max(220, compareStage.clientHeight - 24);
      const fitScale = Math.min(stageW / primary.naturalWidth, stageH / primary.naturalHeight);
      const targetW = Math.max(120, Math.round(primary.naturalWidth * fitScale * zoomScale));
      const targetH = Math.max(120, Math.round(primary.naturalHeight * fitScale * zoomScale));

      if (imgMode === "raw") {
        if (compareRawImg) {
          compareRawImg.style.width = `${targetW}px`;
          compareRawImg.style.height = `${targetH}px`;
          compareRawImg.style.maxWidth = "none";
          compareRawImg.style.maxHeight = "none";
        }
      } else {
        if (compareCleanImg) {
          compareCleanImg.style.width = `${targetW}px`;
          compareCleanImg.style.height = `${targetH}px`;
          compareCleanImg.style.maxWidth = "none";
          compareCleanImg.style.maxHeight = "none";
        }
      }

      if (keepCenter && prevW > 0 && prevH > 0) {
        requestAnimationFrame(() => {
          const rx = prevCenterX / prevW;
          const ry = prevCenterY / prevH;
          compareStage.scrollLeft = Math.max(0, rx * compareStage.scrollWidth - compareStage.clientWidth / 2);
          compareStage.scrollTop = Math.max(0, ry * compareStage.scrollHeight - compareStage.clientHeight / 2);
        });
      }
    }
  }

  function setZoomScale(nextScale, opts = {}) {
    let clamped = Math.max(0.75, Math.min(3.2, Number(nextScale) || 1.0));
    if (Math.abs(clamped - 1.0) < 0.04) clamped = 1.0;
    zoomScale = clamped;
    applyZoomLayout(opts);
  }

  function setImageMode(mode) {
    const next = mode === "raw" || mode === "split" ? mode : "clean";
    imgMode = next;
    if (compareWrap) {
      compareWrap.dataset.imgMode = next;
    }
    document.querySelectorAll("[data-img-mode]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.imgMode === next);
    });
    if (compareSliderRow) {
      compareSliderRow.classList.toggle("hidden", next !== "split");
    }
    if (compareRawLayer) {
      if (next === "split") {
        const pct = compareSlider ? Number(compareSlider.value || 50) : 50;
        compareRawLayer.style.clipPath = `inset(0 ${100 - pct}% 0 0)`;
        if (compareDivider) compareDivider.style.left = `${pct}%`;
      } else {
        compareRawLayer.style.clipPath = "none";
      }
    }
    applyZoomLayout();
    refreshBboxForCurrentState();
  }

  document.querySelectorAll("[data-img-mode]").forEach((btn) => {
    btn.addEventListener("click", () => setImageMode(btn.dataset.imgMode));
  });

  [compareCleanImg, compareRawImg].forEach((img) => {
    if (!img) return;
    img.addEventListener("load", () => {
      if (Math.abs(zoomScale - 1.0) > 0.02) applyZoomLayout();
    });
  });

  if (zoomOutBtn) zoomOutBtn.addEventListener("click", () => setZoomScale(zoomScale - 0.2, { keepCenter: true }));
  if (zoomInBtn) zoomInBtn.addEventListener("click", () => setZoomScale(zoomScale + 0.2, { keepCenter: true }));
  if (zoomResetBtn) zoomResetBtn.addEventListener("click", () => setZoomScale(1.0));
  if (zoomToggleBtn) {
    zoomToggleBtn.addEventListener("click", () => {
      setZoomScale(zoomScale > 1.05 ? 1.0 : 1.75, { keepCenter: true });
    });
  }

  if (compareStage) {
    compareStage.addEventListener(
      "wheel",
      (e) => {
        if (!selectedPage) return;
        e.preventDefault();
        const step = e.deltaY < 0 ? 0.15 : -0.15;
        setZoomScale(zoomScale + step, { keepCenter: true });
      },
      { passive: false }
    );
  }

  // Pointer drag: split-curtain wipe in split mode, drag-to-pan when zoomed in clean/raw mode
  let dragMode = null; // "split" | "pan" | null
  let panStart = { x: 0, y: 0, left: 0, top: 0 };

  function updateCompareFromPointer(clientX) {
    const rect = compareWrap.getBoundingClientRect();
    if (!rect.width) return;
    setSliderPercent(((clientX - rect.left) / rect.width) * 100);
  }

  if (compareWrap) {
    compareWrap.addEventListener("dblclick", (e) => {
      if (!selectedPage) return;
      e.preventDefault();
      setZoomScale(zoomScale > 1.05 ? 1.0 : 1.75, { keepCenter: true });
    });

    compareWrap.addEventListener("pointerdown", (e) => {
      if (imgMode === "split") {
        dragMode = "split";
        try {
          compareWrap.setPointerCapture?.(e.pointerId);
        } catch {
          // Ignore synthetic events
        }
        updateCompareFromPointer(e.clientX);
      } else if (zoomScale > 1.02 && compareStage) {
        dragMode = "pan";
        panStart = {
          x: e.clientX,
          y: e.clientY,
          left: compareStage.scrollLeft,
          top: compareStage.scrollTop,
        };
        compareWrap.classList.add("is-panning");
        try {
          compareWrap.setPointerCapture?.(e.pointerId);
        } catch {
          // Ignore
        }
      }
    });

    compareWrap.addEventListener("pointermove", (e) => {
      if (dragMode === "split") {
        updateCompareFromPointer(e.clientX);
      } else if (dragMode === "pan" && compareStage) {
        compareStage.scrollLeft = panStart.left - (e.clientX - panStart.x);
        compareStage.scrollTop = panStart.top - (e.clientY - panStart.y);
      }
    });

    ["pointerup", "pointercancel"].forEach((evt) => {
      compareWrap.addEventListener(evt, (e) => {
        if (!dragMode) return;
        dragMode = null;
        compareWrap.classList.remove("is-panning");
        try {
          compareWrap.releasePointerCapture?.(e.pointerId);
        } catch {
          // Ignore
        }
      });
    });
  }

  // OCR Bounding-Box Sync Overlay Helpers
  function normalizeMatchText(s) {
    return String(s || "")
      .toLowerCase()
      .replace(/[\s|｜,:;，。：；、\-_'"`()（）\[\]{}<>《》·•]+/g, "");
  }

  function makeBigrams(s) {
    const out = new Set();
    if (!s) return out;
    if (s.length === 1) {
      out.add(s);
      return out;
    }
    for (let i = 0; i < s.length - 1; i += 1) {
      out.add(s.slice(i, i + 2));
    }
    return out;
  }

  function bigramDice(a, b) {
    if (!a || !b) return 0;
    const ga = makeBigrams(a);
    const gb = makeBigrams(b);
    let hit = 0;
    ga.forEach((g) => {
      if (gb.has(g)) hit += 1;
    });
    return (2 * hit) / (ga.size + gb.size);
  }

  function matchBlocksForFragment(fragNorm, blocks, { multiLine = false } = {}) {
    if (!fragNorm || !blocks.length) return [];
    const exact = [];
    const contained = [];
    let bestSingle = null;
    let bestScore = 0;

    for (const blk of blocks) {
      const normB = blk._norm;
      if (!normB) continue;
      if (normB === fragNorm) {
        exact.push(blk);
        continue;
      }
      if (multiLine && normB.length >= 2 && fragNorm.includes(normB)) {
        contained.push(blk);
        continue;
      }
      let score = 0;
      if (normB.length >= 2 && fragNorm.length >= 2) {
        if (normB.includes(fragNorm)) {
          score = 0.88 + Math.min(0.1, fragNorm.length / Math.max(1, normB.length) * 0.1);
        } else if (fragNorm.includes(normB)) {
          score = 0.82 + Math.min(0.1, normB.length / Math.max(1, fragNorm.length) * 0.1);
        } else {
          const dice = bigramDice(fragNorm, normB);
          if (dice >= 0.45) score = dice * 0.8;
        }
      }
      if (score > bestScore) {
        bestScore = score;
        bestSingle = blk;
      }
    }

    if (exact.length) return exact;
    if (multiLine && contained.length) return contained;
    return bestSingle ? [bestSingle] : [];
  }

  function findBlocksForSyncEl(targetEl, page) {
    if (!targetEl || !page || !Array.isArray(page.ocr_blocks) || !page.ocr_blocks.length) return [];
    const validBlocks = page.ocr_blocks
      .filter((b) => Array.isArray(b.bbox) && b.bbox.length >= 4 && b.text)
      .map((b) => ({ ...b, _norm: normalizeMatchText(b.text) }))
      .filter((b) => b._norm.length > 0);
    if (!validBlocks.length) return [];

    if (targetEl.matches("tr")) {
      const picked = [];
      const seen = new Set();
      targetEl.querySelectorAll("th, td").forEach((cell) => {
        const normCell = normalizeMatchText(cell.textContent);
        if (!normCell) return;
        for (const m of matchBlocksForFragment(normCell, validBlocks, { multiLine: false })) {
          const k = JSON.stringify(m.bbox);
          if (!seen.has(k)) {
            seen.add(k);
            picked.push(m);
          }
        }
      });
      return picked;
    }

    if (targetEl.matches(".md-sync-cell")) {
      const normCell = normalizeMatchText(targetEl.textContent);
      if (normCell) {
        const res = matchBlocksForFragment(normCell, validBlocks, { multiLine: false });
        if (res.length) return res;
      }
      const row = targetEl.closest("tr");
      return row ? findBlocksForSyncEl(row, page) : [];
    }

    const normText = normalizeMatchText(targetEl.textContent);
    return matchBlocksForFragment(normText, validBlocks, { multiLine: true });
  }

  function canDisplayBboxOverlay() {
    if (!selectedPage || !compareCleanImg) return false;
    if (imgMode === "clean" || imgMode === "split") return true;
    const m = selectedPage.clean_meta || {};
    const rot = ((Number(m.rotated_deg || 0) % 360) + 360) % 360;
    if (m.cropped || rot !== 0 || Math.abs(Number(m.deskew_deg || 0)) >= 0.6 || Number(m.dewarp_px || 0) >= 2.0) {
      return false;
    }
    const cw = Number(m.output_width || compareCleanImg.naturalWidth || 0);
    const ch = Number(m.output_height || compareCleanImg.naturalHeight || 0);
    const rw = Number(compareRawImg ? compareRawImg.naturalWidth : 0);
    const rh = Number(compareRawImg ? compareRawImg.naturalHeight : 0);
    return cw > 0 && ch > 0 && Math.abs(cw - rw) <= 6 && Math.abs(ch - rh) <= 6;
  }

  function renderBboxOverlay(blocks, { pinned = false, scrollToMatch = false } = {}) {
    if (!reviewBboxOverlay) return;
    if (!selectedPage || !blocks.length || !canDisplayBboxOverlay()) {
      reviewBboxOverlay.replaceChildren();
      return;
    }
    const m = selectedPage.clean_meta || {};
    const w = Number(m.output_width || (compareCleanImg && compareCleanImg.naturalWidth) || 0);
    const h = Number(m.output_height || (compareCleanImg && compareCleanImg.naturalHeight) || 0);
    if (!w || !h) {
      reviewBboxOverlay.replaceChildren();
      return;
    }

    const svgNS = "http://www.w3.org/2000/svg";
    reviewBboxOverlay.setAttribute("viewBox", `0 0 ${w} ${h}`);
    const polys = [];
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;

    for (const b of blocks) {
      const pts = b.bbox
        .slice(0, 4)
        .map((pt) => {
          const px = Number(pt[0] || 0);
          const py = Number(pt[1] || 0);
          minX = Math.min(minX, px);
          minY = Math.min(minY, py);
          maxX = Math.max(maxX, px);
          maxY = Math.max(maxY, py);
          return `${px.toFixed(1)},${py.toFixed(1)}`;
        })
        .join(" ");
      const poly = document.createElementNS(svgNS, "polygon");
      poly.setAttribute("points", pts);
      poly.setAttribute("class", pinned ? "bbox-poly is-pinned" : "bbox-poly");
      polys.push(poly);
    }
    reviewBboxOverlay.replaceChildren(...polys);

    if (scrollToMatch && zoomScale > 1.02 && compareStage && Number.isFinite(minX)) {
      const cxRatio = ((minX + maxX) / 2) / w;
      const cyRatio = ((minY + maxY) / 2) / h;
      const imgRect = getActivePrimaryImg()?.getBoundingClientRect();
      if (imgRect && imgRect.width > compareStage.clientWidth) {
        compareStage.scrollTo({
          left: Math.max(0, cxRatio * compareStage.scrollWidth - compareStage.clientWidth / 2),
          top: Math.max(0, cyRatio * compareStage.scrollHeight - compareStage.clientHeight / 2),
          behavior: "smooth",
        });
      }
    }
  }

  function clearBboxHighlight({ clearPin = true } = {}) {
    if (clearPin && pinnedSyncEl) {
      pinnedSyncEl.classList.remove("is-sync-pinned");
      pinnedSyncEl = null;
    }
    if (reviewMdPreview) {
      reviewMdPreview.querySelectorAll(".is-sync-hover").forEach((n) => n.classList.remove("is-sync-hover"));
    }
    if (!pinnedSyncEl && reviewBboxOverlay) {
      reviewBboxOverlay.replaceChildren();
    }
  }

  function refreshBboxForCurrentState() {
    if (!pinnedSyncEl) {
      if (reviewBboxOverlay) reviewBboxOverlay.replaceChildren();
      return;
    }
    const matches = findBlocksForSyncEl(pinnedSyncEl, selectedPage);
    renderBboxOverlay(matches, { pinned: true, scrollToMatch: false });
  }

  if (reviewMdPreview) {
    reviewMdPreview.addEventListener("pointerover", (e) => {
      if (pinnedSyncEl || !selectedPage) return;
      const syncEl = e.target.closest(".md-sync-cell, .md-sync-block");
      if (!syncEl || !reviewMdPreview.contains(syncEl)) return;
      reviewMdPreview.querySelectorAll(".is-sync-hover").forEach((n) => n.classList.remove("is-sync-hover"));
      syncEl.classList.add("is-sync-hover");
      renderBboxOverlay(findBlocksForSyncEl(syncEl, selectedPage), { pinned: false });
    });

    reviewMdPreview.addEventListener("pointerleave", () => {
      if (pinnedSyncEl) return;
      clearBboxHighlight({ clearPin: false });
    });

    reviewMdPreview.addEventListener("click", (e) => {
      if (!selectedPage) return;
      const syncEl = e.target.closest(".md-sync-cell, .md-sync-block");
      if (!syncEl || !reviewMdPreview.contains(syncEl)) return;
      if (pinnedSyncEl === syncEl) {
        clearBboxHighlight({ clearPin: true });
        return;
      }
      if (pinnedSyncEl) pinnedSyncEl.classList.remove("is-sync-pinned");
      pinnedSyncEl = syncEl;
      pinnedSyncEl.classList.add("is-sync-pinned");
      renderBboxOverlay(findBlocksForSyncEl(syncEl, selectedPage), { pinned: true, scrollToMatch: true });
    });

    reviewMdPreview.addEventListener("dblclick", () => {
      reviewTextViewMode = "edit";
      syncReviewTextView();
      if (reviewTextEditor) reviewTextEditor.focus();
    });
  }

  function renderStats(stats) {
    const b = (stats && stats.book) || {
      total: 0,
      completed: 0,
      pending_clean: 0,
      pending_ocr: 0,
      pending_llm: 0,
      needs_review: 0,
      failed: 0,
    };
    if (kpiTotal) kpiTotal.textContent = String(b.total);
    if (kpiCompleted) kpiCompleted.textContent = String(b.completed);
    if (kpiClean) kpiClean.textContent = String(b.pending_clean);
    if (kpiOcr) kpiOcr.textContent = String(b.pending_ocr + (b.pending_llm || 0));
    if (kpiReview) kpiReview.textContent = String(b.needs_review);
    if (kpiFailed) kpiFailed.textContent = String(b.failed);

    const denom = Math.max(1, b.total);
    const proc = b.pending_clean + b.pending_ocr + (b.pending_llm || 0);
    const processed = b.completed + b.needs_review + b.failed;

    const barDone = document.getElementById("barDone");
    const barProc = document.getElementById("barProc");
    const barReview = document.getElementById("barReview");
    const barFailed = document.getElementById("barFailed");
    if (barDone) barDone.style.width = `${(b.completed / denom) * 100}%`;
    if (barProc) barProc.style.width = `${(proc / denom) * 100}%`;
    if (barReview) barReview.style.width = `${(b.needs_review / denom) * 100}%`;
    if (barFailed) barFailed.style.width = `${(b.failed / denom) * 100}%`;

    if (globalProgressNote) {
      if (b.total === 0) {
        globalProgressNote.textContent = "尚无页面";
      } else if (proc > 0) {
        globalProgressNote.textContent = `已完成 ${processed}/${b.total} · 排队 ${proc}`;
      } else {
        globalProgressNote.textContent = `合格 ${b.completed} · 待复核 ${b.needs_review} · 失败 ${b.failed}`;
      }
    }

    if (pipeStep1Meta) pipeStep1Meta.textContent = b.total > 0 ? "· 已安全落盘" : "· 等待入库";
    if (pipeStep2Meta) pipeStep2Meta.textContent = `· 已洗净 ${Math.max(0, b.total - b.pending_clean)} 页`;
    if (pipeStep3Meta) pipeStep3Meta.textContent = `· 已出字 ${processed} 页`;

    const isPaused = Boolean(stats && stats.paused);
    if (pausePipelineBtn) pausePipelineBtn.classList.toggle("hidden", isPaused);
    if (resumePipelineBtn) resumePipelineBtn.classList.toggle("hidden", !isPaused);
    if (retryFailedBtn) retryFailedBtn.classList.toggle("hidden", b.failed === 0);

    if (pipelineHeroHeadline && pipelineHeroSub) {
      if (b.total === 0) {
        pipelineHeroHeadline.textContent = "等待采集照片…";
        pipelineHeroSub.textContent = "在「1 拍照」拍入或拖入纸张图片后，后台会自动完成切边、去阴影、离线识字与清晰度把关。";
      } else if (isPaused) {
        pipelineHeroHeadline.textContent = `后台已暂停（共 ${b.total} 页，剩 ${proc} 页待加工）`;
        pipelineHeroSub.textContent = "点击右侧「恢复运行」后将继续自动图像清洗和离线文字识别。";
      } else if (proc > 0) {
        pipelineHeroHeadline.textContent = `正在后台清洗与识字（剩 ${proc} 页排队）…`;
        pipelineHeroSub.textContent = `已完成 ${processed}/${b.total} 页，可先在下方核对已出结果，也可以继续拍照。`;
      } else if (b.failed > 0) {
        pipelineHeroHeadline.textContent = `加工完毕（${b.failed} 页失败待处理）`;
        pipelineHeroSub.textContent = "可点右侧「重试失败」重新识别，或在下方直接删掉无法使用的废片。";
      } else if (b.needs_review > 0) {
        pipelineHeroHeadline.textContent = `已全部加工完毕（${b.total}/${b.total} 页）`;
        pipelineHeroSub.textContent = `${b.completed} 页直接合格；${b.needs_review} 页存在模糊、重复或低置信疑点，建议逐页核对或删废。`;
      } else {
        pipelineHeroHeadline.textContent = `全部 ${b.total} 页绿灯合格！`;
        pipelineHeroSub.textContent = "可前往「3 表格」按模板自动分拣对表，并导出标准 Excel 或 CatBoost 宽表。";
      }
    }
  }

  const UPGRADE_REASON_LABELS = {
    low_confidence: "低置信度",
    complex_table: "复杂多列表格",
    numeric_density_anomaly: "数字密集/越界疑点",
    high_line_fragmentation: "行碎片率高(无线表/手写)",
    out_of_range_alert: "数值越界告警",
  };

  const PROCESSING_STATUSES = new Set([
    "STAGING",
    "CAPTURED",
    "CLEANING",
    "CLEANED",
    "OCR_RUNNING",
    "LLM_RUNNING",
  ]);

  const ENGINE_HUMAN_LABELS = {
    rapidocr: "RapidOCR 离线",
    ppocrv5_mobile: "PP-OCRv5 轻量离线",
    ppocrv5_server: "PP-OCRv5 高精离线",
    ollama_glm_ocr: "GLM-OCR 0.9B 视觉",
    ollama_qwen25vl_3b: "Qwen2.5-VL 3B 视觉",
    deepseek_ocr2_local: "DeepSeek-OCR 2 本地",
    deepseek_ocr2_api: "Vision API",
    hybrid_smart: "离线智能混合",
  };

  function formatOcrEngineInfo(page) {
    const meta = (page && page.clean_meta) || {};
    const eng = String(meta.ocr_engine_used || "rapidocr");
    const engineMap = {
      rapidocr: { label: "RapidOCR 离线", cls: "pipe-tag" },
      ppocrv5_mobile: { label: "PP-OCRv5 轻量离线", cls: "pipe-tag" },
      ppocrv5_server: { label: "PP-OCRv5 高精离线", cls: "pipe-tag is-ok" },
      ollama_glm_ocr: { label: "GLM-OCR 0.9B 视觉", cls: "pipe-tag is-accent" },
      ollama_qwen25vl_3b: { label: "Qwen2.5-VL 3B 视觉", cls: "pipe-tag is-accent" },
      deepseek_ocr2_local: { label: "DeepSeek-OCR 2 本地", cls: "pipe-tag is-accent" },
      deepseek_ocr2_api: { label: "Vision API 升级", cls: "pipe-tag is-accent" },
    };
    const info = engineMap[eng] || { label: ENGINE_HUMAN_LABELS[eng] || eng, cls: "pipe-tag" };
    const elapsedMs = Number(meta.ocr_elapsed_ms || 0);
    const elapsedText = elapsedMs > 0 ? ` · ${(elapsedMs / 1000).toFixed(1)}s` : "";
    const reasons = Array.isArray(meta.ocr_upgrade_reasons) ? meta.ocr_upgrade_reasons : [];
    const reasonText = reasons.map((r) => UPGRADE_REASON_LABELS[r] || r).join("、");
    const titleParts = [
      reasonText ? `坐标锚定触发原因：${reasonText}` : "实际执行文字识别的 OCR 引擎",
      elapsedMs > 0 ? `耗时 ${(elapsedMs / 1000).toFixed(1)} 秒` : "",
    ].filter(Boolean);
    return {
      label: `${info.label}${elapsedText}`,
      cls: info.cls,
      title: titleParts.join(" · "),
      hasLlmSheet: Boolean(meta.llm_sheet_extracted),
    };
  }

  const SHORT_STATUS_LABELS = {
    STAGING: "暂存",
    CAPTURED: "待洗",
    CLEANING: "清洗",
    CLEANED: "待字",
    OCR_RUNNING: "识字",
    OCR_DONE: "待核",
    LLM_RUNNING: "精校",
    LLM_DONE: "精完",
    READY: "待检",
    NEEDS_REVIEW: "待核",
    APPROVED: "通过",
    FAILED: "失败",
  };

  function syncStepFilterHighlight(status) {
    document.querySelectorAll("[data-pipe-step-filter]").forEach((step) => {
      step.classList.toggle("is-active", step.dataset.pipeStepFilter === status);
    });
  }

  async function refreshJobs() {
    const qs = state.currentBookId ? `?book_id=${state.currentBookId}&limit=30` : "?limit=30";
    const data = await apiJson(`/api/jobs${qs}`);
    const items = data.items || [];

    if (pipelineJobsCount) {
      pipelineJobsCount.textContent = String(items.length);
    }

    jobsTableBody.replaceChildren();
    exportJobsBody.replaceChildren();

    let exportCount = 0;
    for (const j of items) {
      const isExport = String(j.job_type || "").startsWith("EXPORT_");
      const progress = j.total_items > 0 ? `${j.done_items}/${j.total_items}` : `${j.done_items}`;
      const downloadBtn =
        isExport && j.status === "DONE"
          ? el("a", {
              class: "btn btn-sm btn-primary",
              href: `/api/jobs/${j.id}/download`,
              text: "下载文件",
            })
          : el("span", { class: "muted-note", text: j.error_msg || "-" });

      jobsTableBody.appendChild(
        el("tr", {}, [
          el("td", { text: `#${j.id}` }),
          el("td", { text: j.job_type }),
          el("td", { text: j.status }),
          el("td", { text: progress }),
          el("td", { class: "cell-trunc", text: j.result_path || j.error_msg || "-", title: j.result_path || j.error_msg || "" }),
          el("td", {}, [downloadBtn.cloneNode(true)]),
        ])
      );

      if (isExport) {
        exportCount += 1;
        const p = j.params || {};
        const pagesTxt = p.limited_by_license
          ? `${p.exported_page_count || j.total_items}/${p.original_page_count || j.total_items} 页（体验版限前${p.free_export_page_limit || 5}页）`
          : String(j.total_items);
        exportJobsBody.appendChild(
          el("tr", {}, [
            el("td", { text: `#${j.id}` }),
            el("td", { text: p.format || j.job_type }),
            el("td", { text: j.status }),
            el("td", { text: pagesTxt }),
            el("td", {}, [downloadBtn]),
          ])
        );
      }
    }

    if (!items.length) {
      jobsTableBody.appendChild(
        el("tr", {}, [
          el("td", {
            colspan: "6",
            class: "empty-state",
            text: "当前书目尚无批量导入或导出后台任务记录",
          }),
        ])
      );
    }
    if (exportCount === 0) {
      exportJobsBody.appendChild(
        el("tr", {}, [
          el("td", {
            colspan: "5",
            class: "empty-state",
            text: "暂无导出文件，点击上方任意格式按钮即可在后台生成下载",
          }),
        ])
      );
    }
  }

  async function refreshPageList(targetPageId = null) {
    if (!state.currentBookId) return;
    const qa = document.getElementById("reviewQaFilter").value;
    const search = document.getElementById("reviewSearchInput").value.trim();
    const qs = new URLSearchParams({
      status: currentFilterStatus,
      qa,
      search,
      limit: String(pageSize),
      offset: String(currentOffset),
    });
    const data = await apiJson(`/api/books/${state.currentBookId}/pages?${qs.toString()}`);
    currentList = data.items || [];
    currentTotal = data.total || 0;

    const startIdx = currentTotal === 0 ? 0 : currentOffset + 1;
    const endIdx = Math.min(currentTotal, currentOffset + currentList.length);
    pageListPagerInfo.textContent = `${startIdx}-${endIdx} / ${currentTotal}`;

    renderGallery();

    if (targetPageId) {
      await selectPage(targetPageId);
    } else if (selectedPage && currentList.some((p) => p.id === selectedPage.id)) {
      await selectPage(selectedPage.id);
    } else if (currentList.length > 0) {
      await selectPage(currentList[0].id);
    } else {
      clearSelectedPage();
    }
  }

  function renderGallery() {
    reviewGallery.replaceChildren();
    if (!currentList.length) {
      reviewGallery.appendChild(
        el("div", {
          class: "empty-state",
          text: "当前筛选条件下暂无页面（可切换上方状态筛选，或前往「1 拍照」采集）",
        })
      );
      return;
    }

    for (const p of currentList) {
      const isSelected = selectedPage && selectedPage.id === p.id;
      const isChecked = selectedPageIds.has(p.id);
      const flags = Array.isArray(p.qa_flags) ? [...p.qa_flags] : [];
      if (p.is_duplicate && !flags.includes("duplicate_page")) flags.push("duplicate_page");
      const hasWarn = flags.length > 0;

      const isProc = PROCESSING_STATUSES.has(p.status);
      let tone = "muted";
      if (p.status === "APPROVED" || p.status === "LLM_DONE") tone = "ok";
      else if (p.status === "FAILED") tone = "danger";
      else if (isProc) tone = "proc";
      else if (hasWarn || p.status === "NEEDS_REVIEW") tone = "warn";

      const shortStatus = SHORT_STATUS_LABELS[p.status] || translateStatus(p.status).slice(0, 2);
      const seqLabel = `#${p.seq_no}${p.spread_side !== "S" ? p.spread_side : ""}`;
      let subTxt = translateStatus(p.status);
      if (p.status === "FAILED" && p.error_msg) {
        subTxt = p.error_msg;
      } else if (isProc) {
        subTxt = p.status === "OCR_RUNNING" ? "正在识字与还原表格..." : `${translateStatus(p.status)}...`;
      } else if (hasWarn) {
        subTxt = translateFlag(flags[0]);
      } else if (p.ocr_confidence) {
        subTxt = `${(Number(p.ocr_confidence) * 100).toFixed(1)}% · 清晰${Math.round(Number(p.blur_score || 0))}`;
      }

      const badgeChildren = isProc
        ? [el("span", { class: "proc-pulse-dot", "aria-hidden": "true" }), shortStatus]
        : [shortStatus];

      const cardNodes = [
        el("img", {
          class: "review-card-thumb",
          src: `/api/pages/${p.id}/image/thumb?t=${encodeURIComponent(p.updated_at || "")}`,
          loading: "lazy",
          alt: `第 ${p.seq_no} 页`,
        }),
        el("div", { class: "review-card-body" }, [
          el("div", { class: "review-card-top" }, [
            el("span", { class: "review-card-seq", text: seqLabel }),
            el("span", { class: `review-card-badge is-${tone}` }, badgeChildren),
          ]),
          el("div", { class: `review-card-sub${hasWarn || p.status === "FAILED" ? " is-warn" : ""}`, text: subTxt, title: subTxt }),
        ]),
      ];
      if (multiSelectMode) {
        cardNodes.unshift(
          el("span", {
            class: `review-check-dot${isChecked ? " is-checked" : ""}`,
            text: isChecked ? "✓" : "",
            "aria-hidden": "true",
          })
        );
      }
      const card = el(
        "button",
        {
          type: "button",
          class: `review-card${isSelected ? " is-selected" : ""}${multiSelectMode && isChecked ? " is-checked" : ""}`,
          "data-page-id": String(p.id),
          onclick: () => {
            if (multiSelectMode) {
              if (selectedPageIds.has(p.id)) selectedPageIds.delete(p.id);
              else selectedPageIds.add(p.id);
              syncMultiSelectUi();
              renderGallery();
              return;
            }
            selectPage(p.id);
          },
        },
        cardNodes
      );
      const quickDelBtn = el("button", {
        type: "button",
        class: "review-card-del",
        title: `删除第 #${p.seq_no} 页废片`,
        "aria-label": `删除第 #${p.seq_no} 页废片`,
        text: "×",
        onclick: async (e) => {
          e.stopPropagation();
          try {
            await apiJson(`/api/pages/${p.id}`, { method: "DELETE" });
            selectedPageIds.delete(p.id);
            if (selectedPage && selectedPage.id === p.id) selectedPage = null;
            showToast(`已移除第 #${p.seq_no} 页废片`, "ok");
            await refreshPageList();
            callbacks.onPagesChanged?.();
          } catch (err) {
            showToast(err.message, "danger");
          }
        },
      });
      reviewGallery.appendChild(el("div", { class: "review-card-wrap" }, [card, quickDelBtn]));
    }
  }

  function stopLoadingTicker() {
    if (loadingTicker) {
      clearInterval(loadingTicker);
      loadingTicker = null;
    }
  }

  function renderLoadingState(page) {
    if (!reviewLoadingPanel) return;
    stopLoadingTicker();
    if (!page || !PROCESSING_STATUSES.has(page.status)) {
      reviewLoadingPanel.classList.add("hidden");
      reviewLoadingPanel.replaceChildren();
      return;
    }

    const meta = page.clean_meta || {};
    const activeEng = String(meta.ocr_active_engine || meta.ocr_engine_used || "rapidocr");
    const engLabel = ENGINE_HUMAN_LABELS[activeEng] || activeEng;
    const timeoutS = Math.max(30, Math.round(Number(meta.ocr_timeout_s || 300)));
    const parsedStart = Date.parse(meta.ocr_started_at || page.updated_at || "");
    const startMs = Number.isFinite(parsedStart) ? parsedStart : Date.now();
    const isSlowLocalVlm =
      activeEng.startsWith("ollama_") || activeEng === "deepseek_ocr2_local" || activeEng === "hybrid_smart";

    const stageLabelMap = {
      STAGING: "正在保存原始图像...",
      CAPTURED: "已进入清洗队列，马上开始预处理...",
      CLEANING: "正在执行光学清洗（去折痕 / 纠偏 / 自动扶正）...",
      CLEANED: `已完成图像清洗，正在排队送入「${engLabel}」...`,
      OCR_RUNNING: `正在调用「${engLabel}」识别文字与表格...`,
      LLM_RUNNING: "正在进行文本级 LLM 精校...",
    };

    const elapsedNode = el("div", { class: "muted-note mono", style: "font-size:12px;" });
    const updateElapsed = () => {
      const sec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
      elapsedNode.textContent =
        page.status === "OCR_RUNNING"
          ? `已等待 ${sec} 秒 · 单页超时上限 ${timeoutS} 秒`
          : `当前已排队/处理 ${sec} 秒`;
    };
    updateElapsed();

    const hintTxt = isSlowLocalVlm
      ? "本地视觉大模型在无大显存电脑上处理复杂表格通常需 1～3 分钟，完成后将自动刷新，期间也可点击左侧继续拍摄。"
      : "后台正在高速处理，完成后将自动展示识别结果。";

    reviewLoadingPanel.replaceChildren(
      el("div", { class: "loading-spinner", "aria-hidden": "true" }),
      el("div", { class: "review-loading-title", text: stageLabelMap[page.status] || "后台正在处理本页..." }),
      elapsedNode,
      el("div", { class: "muted-note", style: "font-size:12px;max-width:340px;line-height:1.5;", text: hintTxt })
    );
    reviewLoadingPanel.classList.remove("hidden");
    loadingTicker = setInterval(updateElapsed, 1000);
  }

  function renderReviewAlertBanner(page) {
    if (!reviewAlertBanner) return;
    reviewAlertBanner.replaceChildren();
    if (!page) {
      reviewAlertBanner.className = "review-alert hidden";
      return;
    }

    const m = page.clean_meta || {};
    const flags = Array.isArray(page.qa_flags) ? page.qa_flags : [];
    const fallbackReason = String(m.ocr_fallback_reason || "").trim();
    const isFailed = page.status === "FAILED";
    const hasFallback =
      Boolean(fallbackReason) ||
      flags.includes("ocr_fallback_rapidocr") ||
      flags.includes("ocr_blocked_offline_policy");

    if (!isFailed && !page.error_msg && !hasFallback) {
      reviewAlertBanner.className = "review-alert hidden";
      return;
    }

    const isDanger = isFailed;
    const reqEng = ENGINE_HUMAN_LABELS[m.ocr_requested_engine] || m.ocr_requested_engine || "";
    const usedEng = ENGINE_HUMAN_LABELS[m.ocr_engine_used] || m.ocr_engine_used || "RapidOCR";

    let title = isDanger ? "本页识别/清洗发生异常" : "高级 OCR 未生效 · 已自动降级保底";
    let detail = "";
    if (isDanger) {
      detail = page.error_msg || "后台识别或清洗失败，请检查模型服务状态后点击重试。";
    } else if (fallbackReason) {
      detail = reqEng ? `原选「${reqEng}」未能完成，已回退为「${usedEng}」：${fallbackReason}` : fallbackReason;
    } else if (flags.includes("ocr_blocked_offline_policy")) {
      title = "涉密严格离线锁已拦截外部请求";
      detail = `已拦截至外部 Vision API 的请求，改用本地「${usedEng}」。`;
    } else {
      detail = `所选引擎调用失败或超时，已自动回退为「${usedEng}」，因此当前结果可能不如预期准确。`;
    }

    const actions = el("div", { class: "toolbar-row", style: "gap:6px;margin-top:6px;flex-wrap:wrap;" }, [
      el("button", {
        type: "button",
        class: "btn btn-compact",
        text: "用当前引擎重识",
        onclick: () => reocrPageBtn?.click(),
      }),
      el("button", {
        type: "button",
        class: "btn btn-compact btn-ghost",
        text: "检查 OCR 设置",
        onclick: () => callbacks.onOpenSettings?.(),
      }),
    ]);

    reviewAlertBanner.className = `review-alert ${isDanger ? "is-danger" : "is-warn"}`;
    reviewAlertBanner.append(
      el("strong", { style: "font-size:12.5px;", text: title }),
      el("div", { style: "font-size:12px;line-height:1.45;margin-top:2px;", text: detail }),
      actions
    );
  }

  function clearSelectedPage() {
    const blank = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
    stopLoadingTicker();
    selectedPage = null;
    clearBboxHighlight({ clearPin: true });
    setZoomScale(1.0);
    reviewImageTitle.textContent = "未选择页面";
    if (reviewEnginePill) {
      reviewEnginePill.className = "pipe-tag hidden";
      reviewEnginePill.textContent = "";
    }
    if (reviewAlertBanner) {
      reviewAlertBanner.className = "review-alert hidden";
      reviewAlertBanner.replaceChildren();
    }
    if (reviewLoadingPanel) {
      reviewLoadingPanel.className = "review-loading-panel hidden";
      reviewLoadingPanel.replaceChildren();
    }
    compareCleanImg.src = blank;
    compareRawImg.src = blank;
    reviewTextEditor.value = "";
    if (reviewMdPreview) reviewMdPreview.replaceChildren(el("div", { class: "muted-note", text: "请从左侧选择页面开始复核" }));
    reviewConfBadge.textContent = "置信度 -";
    reviewConfBadge.className = "pill";
    reviewFlagRow.replaceChildren();
    reviewFlagRow.classList.add("hidden");
    if (reviewSyncHint) reviewSyncHint.classList.add("hidden");
    reviewCleanDiag.textContent = "请从左侧选择一个页面开始对比复核";
    reviewCleanDiag.title = "";
  }

  async function selectPage(pageId) {
    const p = await apiJson(`/api/pages/${pageId}`);
    selectedPage = p;
    stopLoadingTicker();
    clearBboxHighlight({ clearPin: true });
    setZoomScale(1.0);
    if (compareStage) compareStage.scrollTo(0, 0);

    reviewGallery.querySelectorAll(".review-card").forEach((card) => {
      const active = Number(card.dataset.pageId) === p.id;
      card.classList.toggle("is-selected", active);
      if (active) card.scrollIntoView({ block: "nearest", inline: "nearest" });
    });

    const ts = encodeURIComponent(p.updated_at || Date.now());
    reviewImageTitle.textContent = `第 #${p.seq_no}${p.spread_side !== "S" ? p.spread_side : ""} 页 · ${translateStatus(p.status)}`;
    if (reviewEnginePill) {
      if (PROCESSING_STATUSES.has(p.status)) {
        const activeEng = String((p.clean_meta && p.clean_meta.ocr_active_engine) || "");
        reviewEnginePill.className = "pipe-tag is-proc";
        reviewEnginePill.textContent = activeEng ? `${ENGINE_HUMAN_LABELS[activeEng] || activeEng} 处理中` : "处理中...";
        reviewEnginePill.title = "后台正在处理本页";
      } else {
        const eng = formatOcrEngineInfo(p);
        reviewEnginePill.className = eng.cls;
        reviewEnginePill.textContent = eng.label;
        reviewEnginePill.title = eng.title;
      }
    }

    const rawUrl = `/api/pages/${p.id}/image/raw?t=${ts}`;
    const cleanUrl = p.clean_path ? `/api/pages/${p.id}/image/clean?t=${ts}` : rawUrl;
    compareRawImg.src = rawUrl;
    compareCleanImg.src = cleanUrl;
    reviewTextEditor.value = p.clean_text || p.ocr_text || "";
    renderReviewAlertBanner(p);
    renderLoadingState(p);
    syncReviewTextView();

    const confPct = p.ocr_confidence ? `${(p.ocr_confidence * 100).toFixed(1)}%` : "-";
    const blurVal = Number(p.blur_score || 0).toFixed(0);
    reviewConfBadge.textContent = `OCR ${confPct}`;
    reviewConfBadge.title = `OCR 置信度 ${confPct} · 清晰度 ${blurVal}`;
    reviewConfBadge.className = `pill ${p.ocr_confidence >= 0.85 ? "pill-ok" : "pill-warn"}`;

    reviewFlagRow.replaceChildren();
    for (const f of p.qa_flags || []) {
      reviewFlagRow.appendChild(el("span", { class: "pill pill-warn", text: translateFlag(f) }));
    }
    if (p.llm_status === "DONE") {
      reviewFlagRow.appendChild(el("span", { class: "pill pill-ok", text: "LLM已精校" }));
    }
    reviewFlagRow.classList.toggle("hidden", reviewFlagRow.childElementCount === 0);

    if (reviewSyncHint) {
      const hasBlocks = Array.isArray(p.ocr_blocks) && p.ocr_blocks.length > 0;
      reviewSyncHint.classList.toggle("hidden", !hasBlocks || reviewTextViewMode !== "preview");
    }

    const m = p.clean_meta || {};
    if (m.bg_uniformity_before !== undefined) {
      const rotPart = Number(m.rotated_deg || 0) % 360 !== 0 ? `转正:${m.rotated_deg}° · ` : "";
      const diagText =
        `${rotPart}切边:${m.cropped ? "是" : "否"} · 纠偏:${m.deskew_deg || 0}° · 展平:${m.dewarp_px || 0}px · 擦除手指:${m.fingers_removed || 0} · 底色波动:${m.bg_uniformity_before}→${m.bg_uniformity_after}`;
      reviewCleanDiag.textContent = diagText;
      reviewCleanDiag.title = diagText;
    } else {
      reviewCleanDiag.textContent = "等待或已完成本地图像清洗";
      reviewCleanDiag.title = "";
    }

    if (m.profile && m.profile.enhance_mode) {
      recleanModeSelect.value = m.profile.enhance_mode;
    }
  }

  function syncReviewTextView() {
    if (!reviewTextEditor || !reviewMdPreview) return;
    const isProc = Boolean(selectedPage && PROCESSING_STATUSES.has(selectedPage.status));
    if (isProc) {
      clearBboxHighlight({ clearPin: true });
      reviewTextEditor.classList.add("hidden");
      reviewMdPreview.classList.add("hidden");
      if (reviewSyncHint) reviewSyncHint.classList.add("hidden");
      return;
    }
    const isPreview = reviewTextViewMode === "preview";
    reviewTextEditor.classList.toggle("hidden", isPreview);
    reviewMdPreview.classList.toggle("hidden", !isPreview);
    if (isPreview) {
      clearBboxHighlight({ clearPin: true });
      reviewMdPreview.replaceChildren(renderSafeMarkdown(reviewTextEditor.value));
    } else {
      clearBboxHighlight({ clearPin: true });
    }
    if (reviewSyncHint) {
      const hasBlocks = Boolean(selectedPage && Array.isArray(selectedPage.ocr_blocks) && selectedPage.ocr_blocks.length > 0);
      reviewSyncHint.classList.toggle("hidden", !isPreview || !hasBlocks);
    }
    document.querySelectorAll("[data-review-text-mode]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.reviewTextMode === reviewTextViewMode);
    });
  }

  document.querySelectorAll("[data-review-text-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      reviewTextViewMode = btn.dataset.reviewTextMode || "preview";
      syncReviewTextView();
    });
  });

  // Filter & Paging bindings
  document.querySelectorAll("[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-filter]").forEach((b) => b.classList.remove("is-active", "btn-primary"));
      btn.classList.add("is-active", "btn-primary");
      currentFilterStatus = btn.dataset.filter;
      syncStepFilterHighlight(currentFilterStatus);
      currentOffset = 0;
      refreshPageList();
    });
  });
  document.getElementById("reviewQaFilter").addEventListener("change", () => {
    currentOffset = 0;
    refreshPageList();
  });
  let searchTimer = null;
  document.getElementById("reviewSearchInput").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      currentOffset = 0;
      refreshPageList();
    }, 250);
  });
  document.getElementById("prevPageListBtn").addEventListener("click", () => {
    if (currentOffset > 0) {
      currentOffset = Math.max(0, currentOffset - pageSize);
      refreshPageList();
    }
  });
  document.getElementById("nextPageListBtn").addEventListener("click", () => {
    if (currentOffset + pageSize < currentTotal) {
      currentOffset += pageSize;
      refreshPageList();
    }
  });

  // Page Actions
  const saveTextBtn = document.getElementById("saveTextBtn");
  saveTextBtn.addEventListener("click", async () => {
    if (!selectedPage) return;
    await withButtonLoading(saveTextBtn, "保存中...", async () => {
      try {
        const updated = await apiJson(`/api/pages/${selectedPage.id}`, {
          method: "PATCH",
          body: { clean_text: reviewTextEditor.value },
        });
        selectedPage = updated;
        if (reviewTextViewMode === "preview") {
          reviewMdPreview.replaceChildren(renderSafeMarkdown(reviewTextEditor.value));
        }
        showToast("校对文本已保存", "ok");
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });

  async function approveCurrent() {
    if (!selectedPage) return;
    try {
      await apiJson(`/api/pages/${selectedPage.id}`, {
        method: "PATCH",
        body: { clean_text: reviewTextEditor.value, status: "APPROVED" },
      });
      showToast(`第 #${selectedPage.seq_no} 页已通过复核`, "ok");
      const curIdx = currentList.findIndex((p) => p.id === selectedPage.id);
      const nextItem = currentList[curIdx + 1];
      await refreshPageList(nextItem ? nextItem.id : null);
      callbacks.onStatsNeeded();
    } catch (err) {
      showToast(err.message, "danger");
    }
  }

  document.getElementById("approvePageBtn").addEventListener("click", approveCurrent);

  const deletePageBtn = document.getElementById("deletePageBtn");
  wireTwoStepAction(deletePageBtn, "确认删此页？", async () => {
    if (!selectedPage) return;
    const doomed = selectedPage;
    try {
      await apiJson(`/api/pages/${doomed.id}`, { method: "DELETE" });
      showToast(`已删除第 #${doomed.seq_no} 页废片`, "ok");
      selectedPage = null;
      await refreshPageList();
      callbacks.onPagesChanged?.();
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  if (toggleMultiSelectBtn) {
    toggleMultiSelectBtn.addEventListener("click", () => {
      multiSelectMode = !multiSelectMode;
      if (!multiSelectMode) selectedPageIds.clear();
      syncMultiSelectUi();
      renderGallery();
    });
  }
  if (cancelMultiSelectBtn) {
    cancelMultiSelectBtn.addEventListener("click", () => {
      multiSelectMode = false;
      selectedPageIds.clear();
      syncMultiSelectUi();
      renderGallery();
    });
  }
  if (selectAllReviewBtn) {
    selectAllReviewBtn.addEventListener("click", () => {
      const allPicked = currentList.length > 0 && currentList.every((p) => selectedPageIds.has(p.id));
      if (allPicked) currentList.forEach((p) => selectedPageIds.delete(p.id));
      else currentList.forEach((p) => selectedPageIds.add(p.id));
      syncMultiSelectUi();
      renderGallery();
    });
  }
  wireTwoStepAction(batchDeleteSelectedBtn, "确认删除选中？", async () => {
    if (!state.currentBookId || !selectedPageIds.size) {
      showToast("请先勾选要删除的废片", "warn");
      return;
    }
    try {
      const ids = Array.from(selectedPageIds);
      const res = await apiJson(`/api/books/${state.currentBookId}/pages/batch-delete`, {
        method: "POST",
        body: { page_ids: ids },
      });
      if (selectedPage && selectedPageIds.has(selectedPage.id)) selectedPage = null;
      selectedPageIds.clear();
      multiSelectMode = false;
      syncMultiSelectUi();
      await refreshPageList();
      callbacks.onPagesChanged?.();
      showToast(`已批量删除 ${res.deleted_count || 0} 页废片`, "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });
  wireTwoStepAction(deleteFlaggedBtn, "确认清理全部异常页？", async () => {
    if (!state.currentBookId) return;
    try {
      const res = await apiJson(`/api/books/${state.currentBookId}/pages/delete-flagged`, {
        method: "POST",
      });
      selectedPage = null;
      selectedPageIds.clear();
      await refreshPageList();
      callbacks.onPagesChanged?.();
      showToast(`已一键清理 ${res.deleted_count || 0} 张异常废片`, "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });

  async function handleRotatePage(btn, pageId, deltaDeg, afterRotate) {
    if (!pageId) return;
    await withButtonLoading(btn, "旋转识字中...", async () => {
      try {
        const updated = await apiJson(`/api/pages/${pageId}/rotate`, {
          method: "POST",
          body: { delta_deg: deltaDeg, rerun_ocr: true },
        });
        await afterRotate(updated);
        if (callbacks.onStatsNeeded) callbacks.onStatsNeeded();
        showToast("已旋转并重新完成离线识别", "ok");
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  }

  const reviewRotLeftBtn = document.getElementById("reviewRotLeftBtn");
  const reviewRotRightBtn = document.getElementById("reviewRotRightBtn");
  if (reviewRotLeftBtn) {
    reviewRotLeftBtn.addEventListener("click", () => {
      if (!selectedPage) return;
      handleRotatePage(reviewRotLeftBtn, selectedPage.id, -90, async (updated) => {
        await selectPage(updated.id);
        renderGallery();
      });
    });
  }
  if (reviewRotRightBtn) {
    reviewRotRightBtn.addEventListener("click", () => {
      if (!selectedPage) return;
      handleRotatePage(reviewRotRightBtn, selectedPage.id, 90, async (updated) => {
        await selectPage(updated.id);
        renderGallery();
      });
    });
  }

  const recleanPageBtn = document.getElementById("recleanPageBtn");
  recleanPageBtn.addEventListener("click", async () => {
    if (!selectedPage) return;
    await withButtonLoading(recleanPageBtn, "重洗中...", async () => {
      try {
        const updated = await apiJson(`/api/pages/${selectedPage.id}/reclean`, {
          method: "POST",
          body: {
            profile_patch: { enhance_mode: recleanModeSelect.value },
            rerun_ocr: false,
          },
        });
        await selectPage(updated.id);
        renderGallery();
        showToast("已完成实时重洗并刷新对比图", "ok");
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });

  if (reocrPageBtn) {
    reocrPageBtn.addEventListener("click", async () => {
      if (!selectedPage) return;
      await withButtonLoading(reocrPageBtn, "重识排队中...", async () => {
        try {
          const updated = await apiJson(`/api/pages/${selectedPage.id}/reclean`, {
            method: "POST",
            body: { profile_patch: {}, rerun_ocr: true },
          });
          await refreshPageList(updated.id);
          callbacks.onStatsNeeded?.();
          showToast("已重新提交给当前 OCR 引擎识字，请稍候...", "ok");
        } catch (err) {
          showToast(err.message, "danger");
        }
      });
    });
  }

  const llmCleanPageBtn = document.getElementById("llmCleanPageBtn");
  llmCleanPageBtn.addEventListener("click", async () => {
    if (!selectedPage) return;
    await withButtonLoading(llmCleanPageBtn, "LLM精校中...", async () => {
      try {
        const updated = await apiJson(`/api/pages/${selectedPage.id}/llm-clean`, { method: "POST" });
        await selectPage(updated.id);
        showToast("已完成 LLM 单页精校", "ok");
      } catch (err) {
        showToast(err.message, "warn");
      }
    });
  });

  // Compact Pipeline Drawer & Batch Jobs Dialog Controls
  if (pausePipelineBtn) {
    pausePipelineBtn.addEventListener("click", async () => {
      await apiJson("/api/pipeline/pause", { method: "POST" });
      callbacks.onStatsNeeded();
    });
  }
  if (resumePipelineBtn) {
    resumePipelineBtn.addEventListener("click", async () => {
      await apiJson("/api/pipeline/resume", { method: "POST" });
      callbacks.onStatsNeeded();
    });
  }
  if (retryFailedBtn) {
    retryFailedBtn.addEventListener("click", async () => {
      await withButtonLoading(retryFailedBtn, "重试中...", async () => {
        const qs = state.currentBookId ? `?book_id=${state.currentBookId}` : "";
        const res = await apiJson(`/api/pipeline/retry-failed${qs}`, { method: "POST" });
        showToast(`已重新入队 ${res.retried} 个失败页面`, "ok");
        callbacks.onStatsNeeded();
        refreshPageList();
      });
    });
  }

  document.querySelectorAll("[data-pipe-step-filter]").forEach((step) => {
    step.addEventListener("click", () => {
      applyReviewFilter(step.dataset.pipeStepFilter || "ALL");
      refreshPageList();
    });
  });

  if (openJobsDialogBtn && pipelineJobsDialog) {
    openJobsDialogBtn.addEventListener("click", async () => {
      await refreshJobs();
      pipelineJobsDialog.showModal();
    });
  }

  function applyReviewFilter(status) {
    currentFilterStatus = status || "ALL";
    currentOffset = 0;
    document.querySelectorAll("[data-filter]").forEach((b) => {
      const act = b.dataset.filter === currentFilterStatus;
      b.classList.toggle("is-active", act);
      b.classList.toggle("btn-primary", act);
    });
    syncStepFilterHighlight(currentFilterStatus);
  }

  // Export triggers
  document.querySelectorAll(".export-trigger").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!state.currentBookId) return;
      await withButtonLoading(btn, "正在生成...", async () => {
        try {
          const fmt = btn.dataset.format;
          const redactChk = document.getElementById("exportRedactPiiChk");
          const termsInp = document.getElementById("exportCustomRedactInput");
          const tplSel = document.getElementById("exportExcelTemplateSelect");
          const customTerms = String((termsInp && termsInp.value) || "")
            .split(/[,，;；\r\n]+/)
            .map((x) => x.trim())
            .filter(Boolean);
          const payload = {
            format: fmt,
            redact_pii: Boolean(redactChk && redactChk.checked),
            custom_redact_terms: customTerms,
          };
          if (tplSel && tplSel.value) payload.template_id = Number(tplSel.value);
          const res = await apiJson(`/api/books/${state.currentBookId}/export`, {
            method: "POST",
            body: payload,
          });
          const p = (res.job && res.job.params) || {};
          if (p.limited_by_license) {
            showToast(
              `免费体验版单次导出前 ${p.free_export_page_limit || 5} 页（本次 ${p.exported_page_count}/${p.original_page_count} 页），点击右上角激活解锁全量导出`,
              "warn"
            );
          } else {
            showToast(`导出任务 #${res.job.id} 已加入队列`, "ok");
          }
          setTimeout(refreshJobs, 450);
        } catch (err) {
          showToast(err.message, "danger");
        }
      });
    });
  });

  function stepSelection(delta) {
    if (!currentList.length) return;
    const idx = selectedPage ? currentList.findIndex((p) => p.id === selectedPage.id) : -1;
    const nextIdx = Math.max(0, Math.min(currentList.length - 1, idx + delta));
    selectPage(currentList[nextIdx].id);
  }

  setImageMode("clean");

  return {
    renderStats,
    refreshJobs,
    applyReviewFilter,
    refreshPageList,
    selectPage,
    approveCurrent,
    stepSelection,
    clearPinnedBbox: () => clearBboxHighlight({ clearPin: true }),
    getSelectedPageId: () => (selectedPage ? selectedPage.id : null),
  };
}
