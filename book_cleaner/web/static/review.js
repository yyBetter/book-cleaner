/** Pipeline monitor, interactive QA Review comparison workbench, and Export center. */

import {
  apiJson,
  el,
  enableDialogLightDismissFallback,
  showToast,
  translateFlag,
  translateStatus,
  wireTwoStepAction,
  withButtonLoading,
} from "/static/api.js";

export function createReviewAndPipelineController(state, callbacks) {
  let currentFilterStatus = "ALL";
  let currentOffset = 0;
  const pageSize = 40;
  let currentTotal = 0;
  let currentList = [];
  let selectedPage = null;
  let multiSelectMode = false;
  const selectedPageIds = new Set();
  let pipelineFilter = "ALL";
  let pipelineItems = [];
  let selectedPipelinePageId = null;
  let nextStepAction = "capture";

  // Pipeline Monitor elements
  const kpiTotal = document.getElementById("kpiTotal");
  const kpiCompleted = document.getElementById("kpiCompleted");
  const kpiClean = document.getElementById("kpiClean");
  const kpiOcr = document.getElementById("kpiOcr");
  const kpiReview = document.getElementById("kpiReview");
  const kpiFailed = document.getElementById("kpiFailed");
  const pipelineHeroHeadline = document.getElementById("pipelineHeroHeadline");
  const pipelineHeroSub = document.getElementById("pipelineHeroSub");
  const pipelineNextStepBtn = document.getElementById("pipelineNextStepBtn");
  const pipeStep1Meta = document.getElementById("pipeStep1Meta");
  const pipeStep2Meta = document.getElementById("pipeStep2Meta");
  const pipeStep3Meta = document.getElementById("pipeStep3Meta");
  const pipelinePagesList = document.getElementById("pipelinePagesList");
  const pipeXrayTitle = document.getElementById("pipeXrayTitle");
  const pipeXrayApproveBtn = document.getElementById("pipeXrayApproveBtn");
  const pipeXrayJumpBtn = document.getElementById("pipeXrayJumpBtn");
  const pipeXrayDeleteBtn = document.getElementById("pipeXrayDeleteBtn");
  const pipeXrayRawImg = document.getElementById("pipeXrayRawImg");
  const pipeXrayCleanImg = document.getElementById("pipeXrayCleanImg");
  const pipeXrayTimeline = document.getElementById("pipeXrayTimeline");
  const pipelineJobsDialog = document.getElementById("pipelineJobsDialog");
  const openJobsDialogBtn = document.getElementById("openJobsDialogBtn");
  const pipelineJobsCount = document.getElementById("pipelineJobsCount");
  const pausePipelineBtn = document.getElementById("pausePipelineBtn");
  const resumePipelineBtn = document.getElementById("resumePipelineBtn");
  const retryFailedBtn = document.getElementById("retryFailedBtn");
  const jobsTableBody = document.getElementById("jobsTableBody");
  const exportJobsBody = document.getElementById("exportJobsBody");
  enableDialogLightDismissFallback(pipelineJobsDialog);

  // Review elements
  const reviewGallery = document.getElementById("reviewGallery");
  const pageListPagerInfo = document.getElementById("pageListPagerInfo");
  const reviewImageTitle = document.getElementById("reviewImageTitle");
  const compareWrap = document.getElementById("compareWrap");
  const compareCleanImg = document.getElementById("compareCleanImg");
  const compareRawImg = document.getElementById("compareRawImg");
  const compareRawLayer = document.getElementById("compareRawLayer");
  const compareDivider = document.getElementById("compareDivider");
  const compareSlider = document.getElementById("compareSlider");
  const zoomToggleBtn = document.getElementById("zoomToggleBtn");
  const reviewCleanDiag = document.getElementById("reviewCleanDiag");
  const reviewConfBadge = document.getElementById("reviewConfBadge");
  const reviewFlagRow = document.getElementById("reviewFlagRow");
  const reviewTextEditor = document.getElementById("reviewTextEditor");
  const recleanModeSelect = document.getElementById("recleanModeSelect");
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
    compareSlider.value = String(Math.round(clamped));
    compareRawLayer.style.clipPath = `inset(0 ${100 - clamped}% 0 0)`;
    compareDivider.style.left = `${clamped}%`;
  }

  compareSlider.addEventListener("input", (e) => setSliderPercent(e.target.value));
  document.querySelectorAll("[data-compare-preset]").forEach((btn) => {
    btn.addEventListener("click", () => setSliderPercent(Number(btn.dataset.comparePreset)));
  });

  // Direct pointer drag on comparison stage
  let isDraggingCompare = false;
  function updateCompareFromPointer(clientX) {
    const rect = compareWrap.getBoundingClientRect();
    if (!rect.width) return;
    setSliderPercent(((clientX - rect.left) / rect.width) * 100);
  }
  compareWrap.addEventListener("pointerdown", (e) => {
    isDraggingCompare = true;
    try {
      compareWrap.setPointerCapture?.(e.pointerId);
    } catch {
      // Ignore synthetic events
    }
    updateCompareFromPointer(e.clientX);
  });
  compareWrap.addEventListener("pointermove", (e) => {
    if (!isDraggingCompare) return;
    updateCompareFromPointer(e.clientX);
  });
  ["pointerup", "pointercancel"].forEach((evt) => {
    compareWrap.addEventListener(evt, (e) => {
      if (!isDraggingCompare) return;
      isDraggingCompare = false;
      try {
        compareWrap.releasePointerCapture?.(e.pointerId);
      } catch {
        // Ignore
      }
    });
  });

  zoomToggleBtn.addEventListener("click", () => {
    const zoomed = compareWrap.classList.toggle("is-zoomed");
    zoomToggleBtn.textContent = zoomed ? "恢复适应" : "放大细节";
  });

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
    kpiTotal.textContent = String(b.total);
    kpiCompleted.textContent = String(b.completed);
    kpiClean.textContent = String(b.pending_clean);
    kpiOcr.textContent = String(b.pending_ocr + (b.pending_llm || 0));
    kpiReview.textContent = String(b.needs_review);
    kpiFailed.textContent = String(b.failed);

    const denom = Math.max(1, b.total);
    const proc = b.pending_clean + b.pending_ocr + (b.pending_llm || 0);
    const processed = b.completed + b.needs_review + b.failed;

    document.getElementById("barDone").style.width = `${(b.completed / denom) * 100}%`;
    document.getElementById("barProc").style.width = `${(proc / denom) * 100}%`;
    document.getElementById("barReview").style.width = `${(b.needs_review / denom) * 100}%`;
    document.getElementById("barFailed").style.width = `${(b.failed / denom) * 100}%`;

    document.getElementById("globalProgressNote").textContent =
      b.total > 0
        ? proc === 0
          ? `自动加工进度 100%（${b.total}/${b.total} 页全部跑完）`
          : `正在自动加工：已跑完 ${processed}/${b.total} 页（${proc} 页排队中）`
        : "尚无页面";

    if (pipeStep1Meta) pipeStep1Meta.textContent = b.total > 0 ? "· 已安全落盘" : "· 等待入库";
    if (pipeStep2Meta) pipeStep2Meta.textContent = `· 已洗净 ${Math.max(0, b.total - b.pending_clean)} 页`;
    if (pipeStep3Meta) pipeStep3Meta.textContent = `· 已出字 ${processed} 页`;

    const isPaused = Boolean(stats && stats.paused);
    if (pausePipelineBtn) pausePipelineBtn.classList.toggle("hidden", isPaused);
    if (resumePipelineBtn) resumePipelineBtn.classList.toggle("hidden", !isPaused);
    if (retryFailedBtn) retryFailedBtn.classList.toggle("hidden", b.failed === 0);

    if (pipelineHeroHeadline && pipelineHeroSub && pipelineNextStepBtn) {
      if (b.total === 0) {
        pipelineHeroHeadline.textContent = "等待采集照片…";
        pipelineHeroSub.textContent = "在「1 拍照」拍入或拖入纸张图片后，后台会自动完成切边、去阴影、离线识字与清晰度把关。";
        pipelineNextStepBtn.textContent = "去「1 拍照」开始采集 →";
        nextStepAction = "capture";
      } else if (isPaused) {
        pipelineHeroHeadline.textContent = `后台流水线已暂停（共 ${b.total} 页，还有 ${proc} 页待加工）`;
        pipelineHeroSub.textContent = "点击右侧「恢复运行」后将继续自动图像清洗和离线文字识别。";
        pipelineNextStepBtn.textContent = "去「3 复核」查看已加工页 →";
        nextStepAction = "review_all";
      } else if (proc > 0) {
        pipelineHeroHeadline.textContent = `后台正在自动清洗与识字（剩余 ${proc} 页排队）…`;
        pipelineHeroSub.textContent = `目前已完成 ${processed}/${b.total} 页，您可以继续拍照，不必等待。`;
        pipelineNextStepBtn.textContent = "去「3 复核」先看已出结果 →";
        nextStepAction = "review_all";
      } else if (b.failed > 0) {
        pipelineHeroHeadline.textContent = `后台加工已跑完（共 ${b.total} 页，${b.failed} 页失败待处理）`;
        pipelineHeroSub.textContent = "可点右侧「重试失败页」重新识别，或在下方直接删除无法使用的废片。";
        pipelineNextStepBtn.textContent = "去「3 复核」清理异常页 →";
        nextStepAction = "review_needs";
      } else if (b.needs_review > 0) {
        pipelineHeroHeadline.textContent = `后台已全部自动加工完毕（${b.total}/${b.total} 页）`;
        pipelineHeroSub.textContent = `${b.completed} 页直接合格；系统发现 ${b.needs_review} 页疑似模糊、连拍重复或文字把握度偏低（已标黄），请复核确认或删废片。`;
        pipelineNextStepBtn.textContent = `去「3 复核」清理 ${b.needs_review} 页异常 →`;
        nextStepAction = "review_needs";
      } else {
        pipelineHeroHeadline.textContent = `全部 ${b.total} 页均已清洗并绿灯合格！`;
        pipelineHeroSub.textContent = "可以前往「4 表格」按模板自动分拣对表，并导出标准 Excel 或 CatBoost 宽表。";
        pipelineNextStepBtn.textContent = "去「4 表格」分拣并导出 →";
        nextStepAction = "dataclean";
      }
    }
  }

  function formatCleanActions(page) {
    const st = page.status || "";
    if (st === "CAPTURED" || st === "STAGING") {
      return [{ label: "排队等待清洗…", cls: "pipe-tag" }];
    }
    if (st === "CLEANING") {
      return [{ label: "正在切边展平…", cls: "pipe-tag" }];
    }
    const m = page.clean_meta || {};
    const chips = [];
    if (m.cropped) chips.push({ label: "已切边", cls: "pipe-tag" });
    const deg = Number(m.deskew_deg || 0);
    if (Math.abs(deg) >= 0.3) chips.push({ label: `纠偏 ${deg.toFixed(1)}°`, cls: "pipe-tag" });
    const dewarp = Number(m.dewarp_px || 0);
    if (dewarp >= 1.5) chips.push({ label: `展平 ${dewarp.toFixed(1)}px`, cls: "pipe-tag" });
    const fingers = Number(m.fingers_removed || 0);
    if (fingers > 0) chips.push({ label: `擦手指 ×${fingers}`, cls: "pipe-tag" });
    if (Number(m.bg_uniformity_before || 0) - Number(m.bg_uniformity_after || 0) >= 2.0) {
      chips.push({ label: "去阴影漂白", cls: "pipe-tag" });
    }
    const mode = (m.profile && m.profile.enhance_mode) || "";
    const modeMap = {
      clean_color: "洁净彩页",
      grayscale_crisp: "锐利灰度",
      bw_sauvola: "黑白二值",
    };
    if (modeMap[mode]) chips.push({ label: modeMap[mode], cls: "pipe-tag" });
    if (!chips.length) chips.push({ label: "基础光学校准", cls: "pipe-tag" });
    return chips;
  }

  function formatOcrSummary(page) {
    const st = page.status || "";
    if (st === "CAPTURED" || st === "STAGING" || st === "CLEANING" || st === "CLEANED") {
      return { badge: "等待识别…", cls: "pipe-tag", note: "清洗完自动排队" };
    }
    if (st === "OCR_RUNNING") {
      return { badge: "正在离线识字…", cls: "pipe-tag", note: "RapidOCR 运行中" };
    }
    if (st === "FAILED") {
      return { badge: "识别中断", cls: "pipe-tag is-danger", note: page.error_msg || "可点重试" };
    }
    const txt = String(page.clean_text || page.ocr_text || "").trim();
    const chars = txt.length;
    const confPct = Number(page.ocr_confidence || 0) * 100;
    if (chars === 0) {
      return { badge: "未识出文字", cls: "pipe-tag is-warn", note: "画面过糊或空白页" };
    }
    return {
      badge: `识出 ${chars} 字`,
      cls: confPct >= 82 ? "pipe-tag is-ok" : "pipe-tag is-warn",
      note: `把握度 ${confPct.toFixed(1)}%`,
    };
  }

  function setPipelineFilter(nextFilter) {
    pipelineFilter = nextFilter || "ALL";
    document.querySelectorAll("[data-pipe-filter]").forEach((b) => {
      const act = b.dataset.pipeFilter === pipelineFilter;
      b.classList.toggle("is-active", act);
      b.classList.toggle("btn-primary", act);
    });
    document.querySelectorAll("[data-pipe-step-filter]").forEach((step) => {
      step.classList.toggle("is-active", step.dataset.pipeStepFilter === pipelineFilter);
    });
  }

  const EMPTY_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

  function renderPipelineXray(page) {
    if (!pipeXrayTimeline) return;
    // Reset two-step delete arm when switching selection
    if (pipeXrayDeleteBtn) {
      pipeXrayDeleteBtn.dataset.armed = "0";
      pipeXrayDeleteBtn.textContent = "删废片";
    }

    if (!page) {
      pipeXrayTitle.textContent = "请点击左侧照片查看加工详情";
      pipeXrayApproveBtn.disabled = true;
      pipeXrayJumpBtn.disabled = true;
      pipeXrayDeleteBtn.disabled = true;
      pipeXrayRawImg.src = EMPTY_PIXEL;
      pipeXrayCleanImg.src = EMPTY_PIXEL;
      pipeXrayTimeline.replaceChildren(
        el("div", {
          class: "pack-empty-state",
          text: "左侧选择任意一页照片后，此处会对比展示「原图 vs 清洗成片」并逐项列出切边、展平、去阴影与识别出来的正文片段。",
        })
      );
      return;
    }

    selectedPipelinePageId = page.id;
    const seqLabel = `#${page.seq_no}${page.spread_side !== "S" ? page.spread_side : ""}`;
    pipeXrayTitle.textContent = `第 ${seqLabel} 页 · ${translateStatus(page.status)}`;
    pipeXrayApproveBtn.disabled = page.status === "APPROVED";
    pipeXrayJumpBtn.disabled = false;
    pipeXrayDeleteBtn.disabled = false;

    const ts = encodeURIComponent(page.updated_at || "");
    const rawUrl = `/api/pages/${page.id}/image/raw?t=${ts}`;
    const cleanUrl = page.clean_path ? `/api/pages/${page.id}/image/clean?t=${ts}` : rawUrl;
    pipeXrayRawImg.src = rawUrl;
    pipeXrayCleanImg.src = cleanUrl;

    // 1. Capture QA Step
    const flags = Array.isArray(page.qa_flags) ? [...page.qa_flags] : [];
    if (page.is_duplicate && !flags.includes("duplicate_page")) flags.push("duplicate_page");
    const qaChips = [
      el("span", { class: "pipe-tag", text: `清晰度 ${Math.round(Number(page.blur_score || 0))}` }),
      el("span", { class: "pipe-tag", text: `曝光 ${Math.round(Number(page.brightness || 0))}` }),
    ];
    if (flags.length) {
      flags.forEach((f) => qaChips.push(el("span", { class: "pipe-tag is-warn", text: translateFlag(f) })));
    } else {
      qaChips.push(el("span", { class: "pipe-tag is-ok", text: "清晰无重拍" }));
    }

    // 2. Cleaning Step
    const cleanChips = formatCleanActions(page).map((c) => el("span", { class: c.cls, text: c.label }));
    const meta = page.clean_meta || {};
    const sizeNote =
      meta.output_width && meta.output_height ? `${meta.output_width}×${meta.output_height}` : "";

    // 3. OCR Step
    const ocrInfo = formatOcrSummary(page);
    const fullTxt = String(page.clean_text || page.ocr_text || "").trim();

    // 4. Plain Conclusion & Guidance
    let verdictBadge = "正在自动排队加工";
    let verdictCls = "pipe-tag";
    let verdictDesc = "后台正在依次执行图像清洗与离线文字识别，完成后会自动刷新状态。";
    if (page.status === "READY" || page.status === "APPROVED" || page.status === "LLM_DONE") {
      verdictBadge = page.status === "APPROVED" ? "✓ 已通过终审" : "✓ 质检合格";
      verdictCls = "pipe-tag is-ok";
      verdictDesc = "画面清晰且识别把握度达标，可直接进入「4 表格」分拣导出，无需额外处理。";
    } else if (page.status === "NEEDS_REVIEW") {
      verdictBadge = "⚠ 标黄待确认";
      verdictCls = "pipe-tag is-warn";
      const flagNames = flags.map((f) => translateFlag(f)).join(" / ");
      verdictDesc = flagNames
        ? `系统怀疑存在「${flagNames}」。如果是废片点上方「删废片」；如果肉眼看字没问题点「直接通过」即可放行。`
        : "文字识别把握度偏低，建议点「精校对比」核对一遍文字，或确认后「直接通过」。";
    } else if (page.status === "FAILED") {
      verdictBadge = "✕ 处理失败";
      verdictCls = "pipe-tag is-danger";
      verdictDesc = page.error_msg || "识别或清洗过程意外中断，可点「重试失败页」或删掉重拍。";
    }

    pipeXrayTimeline.replaceChildren(
      el("div", { class: "pipe-tl-step" }, [
        el("div", { class: "pipe-tl-head" }, [
          el("span", { text: "① 拍照入库与初检" }),
          el("span", { class: flags.length ? "pipe-tag is-warn" : "pipe-tag is-ok", text: flags.length ? "发现异常标记" : "初检通过" }),
        ]),
        el("div", { class: "pipe-tags" }, qaChips),
      ]),
      el("div", { class: "pipe-tl-step" }, [
        el("div", { class: "pipe-tl-head" }, [
          el("span", { text: "② 图像美容清洗做了啥" }),
          sizeNote ? el("span", { class: "muted-note mono", style: "font-size:11.5px;", text: sizeNote }) : el("span"),
        ]),
        el("div", { class: "pipe-tags" }, cleanChips),
      ]),
      el("div", { class: "pipe-tl-step" }, [
        el("div", { class: "pipe-tl-head" }, [
          el("span", { text: "③ 离线文字识别 (OCR)" }),
          el("span", { class: ocrInfo.cls, text: `${ocrInfo.badge} · ${ocrInfo.note}` }),
        ]),
        el("div", {
          class: "pipe-ocr-preview scroller",
          text: fullTxt ? fullTxt.slice(0, 600) : "（本页暂未识出有效正文，如原图确实模糊/空白可直接当废片删掉）",
        }),
      ]),
      el("div", { class: "pipe-tl-step" }, [
        el("div", { class: "pipe-tl-head" }, [
          el("span", { text: "④ 结论与建议" }),
          el("span", { class: verdictCls, text: verdictBadge }),
        ]),
        el("div", { class: "muted-note", style: "font-size:12.5px;line-height:1.5;", text: verdictDesc }),
      ])
    );
  }

  function renderPipelineList() {
    if (!pipelinePagesList) return;
    pipelinePagesList.replaceChildren();
    if (!pipelineItems.length) {
      pipelinePagesList.appendChild(
        el("div", {
          class: "pack-empty-state",
          text:
            pipelineFilter === "ALL"
              ? "本书还没有拍照页面，请先前往「1 拍照」拍摄或拖入图片。"
              : "当前筛选条件下暂无页面，点击上方「全部」查看整册实况。",
        })
      );
      renderPipelineXray(null);
      return;
    }

    for (const p of pipelineItems) {
      const isSelected = selectedPipelinePageId === p.id;
      const isWarn = p.status === "NEEDS_REVIEW" || p.status === "FAILED";
      const rawTxt = String(p.clean_text || p.ocr_text || "").replace(/\s+/g, " ").trim();
      const snippet = rawTxt ? rawTxt.slice(0, 42) : "（未识出文字或待处理）";

      const flags = Array.isArray(p.qa_flags) ? [...p.qa_flags] : [];
      if (p.is_duplicate && !flags.includes("duplicate_page")) flags.push("duplicate_page");

      const stageTags = [
        el("span", {
          class: flags.length ? "pipe-tag is-warn" : "pipe-tag",
          text: flags.length ? `${translateFlag(flags[0])}` : `清晰度 ${Math.round(Number(p.blur_score || 0))}`,
        }),
      ];
      formatCleanActions(p)
        .slice(0, 3)
        .forEach((c) => stageTags.push(el("span", { class: c.cls, text: c.label })));

      const ocrInfo = formatOcrSummary(p);
      stageTags.push(el("span", { class: ocrInfo.cls, text: `${ocrInfo.badge} · ${ocrInfo.note}` }));

      let statusLabel = "加工中…";
      let statusCls = "pipe-tag";
      if (p.status === "READY" || p.status === "APPROVED" || p.status === "LLM_DONE") {
        statusLabel = p.status === "APPROVED" ? "✓ 已通过" : "✓ 已合格";
        statusCls = "pipe-tag is-ok";
      } else if (p.status === "NEEDS_REVIEW") {
        statusLabel = "⚠ 标黄待复核";
        statusCls = "pipe-tag is-warn";
      } else if (p.status === "FAILED") {
        statusLabel = "✕ 失败";
        statusCls = "pipe-tag is-danger";
      }

      const rowCard = el(
        "button",
        {
          type: "button",
          class: `pipe-row-card${isWarn ? " is-warn-row" : ""}${isSelected ? " is-selected" : ""}`,
          onclick: () => {
            selectedPipelinePageId = p.id;
            renderPipelineList();
            renderPipelineXray(p);
          },
        },
        [
          el("img", {
            class: "pipe-row-thumb",
            src: `/api/pages/${p.id}/image/thumb?t=${encodeURIComponent(p.updated_at || "")}`,
            alt: `第 ${p.seq_no} 页`,
            loading: "lazy",
          }),
          el("div", { class: "pipe-row-main" }, [
            el("div", { class: "pipe-row-head" }, [
              el("span", {
                class: "pipe-row-title",
                text: `#${p.seq_no}${p.spread_side !== "S" ? p.spread_side : ""} · ${translateStatus(p.status)}`,
              }),
              el("span", { class: "pipe-row-snippet", title: rawTxt || snippet, text: snippet }),
            ]),
            el("div", { class: "pipe-row-stages" }, stageTags),
          ]),
          el("div", { class: "pipe-row-side" }, [
            el("span", { class: statusCls, text: statusLabel }),
            el("span", { class: "muted-note", style: "font-size:11.5px;", text: isSelected ? "右侧已展开对比 →" : "点开看原图对比" }),
          ]),
        ]
      );
      pipelinePagesList.appendChild(rowCard);
    }
  }

  async function refreshPipelinePages(preferPageId = null) {
    if (!state.currentBookId || !pipelinePagesList) return;
    const qs = new URLSearchParams({
      status: pipelineFilter,
      order: "desc",
      limit: "50",
      offset: "0",
    });
    const data = await apiJson(`/api/books/${state.currentBookId}/pages?${qs.toString()}`);
    pipelineItems = data.items || [];

    if (!pipelineItems.length) {
      selectedPipelinePageId = null;
      renderPipelineList();
      return;
    }

    let chosen = null;
    if (preferPageId) {
      chosen = pipelineItems.find((p) => p.id === preferPageId) || null;
    }
    if (!chosen && selectedPipelinePageId) {
      chosen = pipelineItems.find((p) => p.id === selectedPipelinePageId) || null;
    }
    if (!chosen) {
      chosen = pipelineItems[0];
    }
    selectedPipelinePageId = chosen.id;
    renderPipelineList();
    renderPipelineXray(chosen);
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
        exportJobsBody.appendChild(
          el("tr", {}, [
            el("td", { text: `#${j.id}` }),
            el("td", { text: (j.params && j.params.format) || j.job_type }),
            el("td", { text: j.status }),
            el("td", { text: String(j.total_items) }),
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
          style: "grid-column:1/-1;",
          class: "empty-state",
          text: "当前筛选条件下暂无页面（可切换筛选标签，或在采集工位导入演示样书）",
        })
      );
      return;
    }

    for (const p of currentList) {
      const isSelected = selectedPage && selectedPage.id === p.id;
      const isChecked = selectedPageIds.has(p.id);
      const hasWarn = p.qa_flags && p.qa_flags.length > 0;
      const cardNodes = [
        el("img", {
          src: `/api/pages/${p.id}/image/thumb?t=${encodeURIComponent(p.updated_at || "")}`,
          loading: "lazy",
          alt: `第 ${p.seq_no} 页`,
        }),
        el("div", { class: "review-card-info" }, [
          el("span", { text: `#${p.seq_no}${p.spread_side !== "S" ? p.spread_side : ""}` }),
          el("span", {
            text: translateStatus(p.status),
            style: `color:${
              p.status === "APPROVED"
                ? "var(--ok)"
                : hasWarn || p.status === "NEEDS_REVIEW"
                ? "var(--warn)"
                : "var(--muted)"
            }`,
          }),
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

  function clearSelectedPage() {
    const blank = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
    selectedPage = null;
    reviewImageTitle.textContent = "未选择页面";
    compareCleanImg.src = blank;
    compareRawImg.src = blank;
    reviewTextEditor.value = "";
    reviewConfBadge.textContent = "置信度 -";
    reviewConfBadge.className = "pill";
    reviewFlagRow.replaceChildren();
    reviewCleanDiag.textContent = "请从左侧选择一个页面开始对比复核";
  }

  async function selectPage(pageId) {
    const p = await apiJson(`/api/pages/${pageId}`);
    selectedPage = p;
    reviewGallery.querySelectorAll(".review-card").forEach((card) => {
      const active = Number(card.dataset.pageId) === p.id;
      card.classList.toggle("is-selected", active);
      if (active) card.scrollIntoView({ block: "nearest", inline: "nearest" });
    });

    const ts = encodeURIComponent(p.updated_at || Date.now());
    reviewImageTitle.textContent = `第 ${p.seq_no} 页 (${translateStatus(p.status)})`;
    compareRawImg.src = `/api/pages/${p.id}/image/raw?t=${ts}`;
    compareCleanImg.src = `/api/pages/${p.id}/image/clean?t=${ts}`;
    reviewTextEditor.value = p.clean_text || p.ocr_text || "";

    const confPct = p.ocr_confidence ? `${(p.ocr_confidence * 100).toFixed(1)}%` : "-";
    reviewConfBadge.textContent = `OCR 置信度 ${confPct} · 清晰度 ${Number(p.blur_score || 0).toFixed(0)}`;
    reviewConfBadge.className = `pill ${p.ocr_confidence >= 0.85 ? "pill-ok" : "pill-warn"}`;

    reviewFlagRow.replaceChildren();
    for (const f of p.qa_flags || []) {
      reviewFlagRow.appendChild(el("span", { class: "pill pill-warn", text: translateFlag(f) }));
    }
    if (p.llm_status === "DONE") {
      reviewFlagRow.appendChild(el("span", { class: "pill pill-ok", text: "LLM已精校" }));
    }

    const m = p.clean_meta || {};
    if (m.bg_uniformity_before !== undefined) {
      reviewCleanDiag.textContent =
        `切边:${m.cropped ? "是" : "否"} · 纠偏:${m.deskew_deg || 0}° · 展平:${m.dewarp_px || 0}px · 擦除手指:${m.fingers_removed || 0} · 底色波动:${m.bg_uniformity_before}→${m.bg_uniformity_after}`;
    } else {
      reviewCleanDiag.textContent = "等待或已完成本地图像清洗";
    }

    if (m.profile && m.profile.enhance_mode) {
      recleanModeSelect.value = m.profile.enhance_mode;
    }
  }

  // Filter & Paging bindings
  document.querySelectorAll("[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-filter]").forEach((b) => b.classList.remove("is-active", "btn-primary"));
      btn.classList.add("is-active", "btn-primary");
      currentFilterStatus = btn.dataset.filter;
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
  wireTwoStepAction(deletePageBtn, "确认删除此页？", async () => {
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

  // Pipeline controls & feed filters
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
        refreshPipelinePages();
      });
    });
  }

  document.querySelectorAll("[data-pipe-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      setPipelineFilter(btn.dataset.pipeFilter);
      refreshPipelinePages();
    });
  });

  document.querySelectorAll("[data-pipe-step-filter]").forEach((step) => {
    step.addEventListener("click", () => {
      setPipelineFilter(step.dataset.pipeStepFilter);
      refreshPipelinePages();
    });
  });

  const refreshPipelinePagesBtn = document.getElementById("refreshPipelinePagesBtn");
  if (refreshPipelinePagesBtn) {
    refreshPipelinePagesBtn.addEventListener("click", () => {
      refreshPipelinePages();
      callbacks.onStatsNeeded();
    });
  }

  if (openJobsDialogBtn && pipelineJobsDialog) {
    openJobsDialogBtn.addEventListener("click", async () => {
      await refreshJobs();
      pipelineJobsDialog.showModal();
    });
  }

  if (pipeXrayApproveBtn) {
    pipeXrayApproveBtn.addEventListener("click", async () => {
      if (!selectedPipelinePageId) return;
      await withButtonLoading(pipeXrayApproveBtn, "处理中...", async () => {
        try {
          await apiJson(`/api/pages/${selectedPipelinePageId}`, {
            method: "PATCH",
            body: { status: "APPROVED" },
          });
          showToast("已通过本页质检", "ok");
          await refreshPipelinePages(selectedPipelinePageId);
          if (callbacks.onStatsNeeded) callbacks.onStatsNeeded();
        } catch (err) {
          showToast(err.message, "danger");
        }
      });
    });
  }

  if (pipeXrayJumpBtn) {
    pipeXrayJumpBtn.addEventListener("click", () => {
      if (!selectedPipelinePageId) return;
      if (callbacks.onJumpToReview) callbacks.onJumpToReview(selectedPipelinePageId, "ALL");
    });
  }

  if (pipeXrayDeleteBtn) {
    wireTwoStepAction(pipeXrayDeleteBtn, "确认删废？", async () => {
      if (!selectedPipelinePageId) return;
      try {
        const curIdx = pipelineItems.findIndex((x) => x.id === selectedPipelinePageId);
        const nextCandidate = pipelineItems[curIdx + 1] || pipelineItems[curIdx - 1] || null;
        await apiJson(`/api/pages/${selectedPipelinePageId}`, { method: "DELETE" });
        await refreshPipelinePages(nextCandidate ? nextCandidate.id : null);
        if (callbacks.onPagesChanged) callbacks.onPagesChanged();
        showToast("已删除废片并清理磁盘文件", "ok");
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  }

  if (pipelineNextStepBtn) {
    pipelineNextStepBtn.addEventListener("click", () => {
      if (nextStepAction === "capture" && callbacks.onJumpToTab) {
        callbacks.onJumpToTab("capture");
      } else if (nextStepAction === "dataclean" && callbacks.onJumpToTab) {
        callbacks.onJumpToTab("dataclean");
      } else if (nextStepAction === "review_needs" && callbacks.onJumpToReview) {
        callbacks.onJumpToReview(null, "NEEDS_REVIEW");
      } else if (callbacks.onJumpToReview) {
        callbacks.onJumpToReview(null, "ALL");
      }
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
  }

  // Export triggers
  document.querySelectorAll(".export-trigger").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!state.currentBookId) return;
      await withButtonLoading(btn, "正在生成...", async () => {
        try {
          const fmt = btn.dataset.format;
          const payload = { format: fmt };
          if (fmt === "xlsx") {
            const tplSel = document.getElementById("exportExcelTemplateSelect");
            if (tplSel && tplSel.value) payload.template_id = Number(tplSel.value);
          }
          const res = await apiJson(`/api/books/${state.currentBookId}/export`, {
            method: "POST",
            body: payload,
          });
          showToast(`导出任务 #${res.job.id} 已加入队列`, "ok");
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

  return {
    renderStats,
    refreshJobs,
    refreshPipelinePages,
    applyReviewFilter,
    refreshPageList,
    selectPage,
    approveCurrent,
    stepSelection,
    getSelectedPageId: () => (selectedPage ? selectedPage.id : null),
  };
}
