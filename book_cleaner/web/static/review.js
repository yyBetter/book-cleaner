/** Pipeline monitor, interactive QA Review comparison workbench, and Export center. */

import {
  apiJson,
  el,
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

  // Pipeline Monitor elements
  const kpiTotal = document.getElementById("kpiTotal");
  const kpiCompleted = document.getElementById("kpiCompleted");
  const kpiClean = document.getElementById("kpiClean");
  const kpiOcr = document.getElementById("kpiOcr");
  const kpiReview = document.getElementById("kpiReview");
  const kpiFailed = document.getElementById("kpiFailed");
  const jobsTableBody = document.getElementById("jobsTableBody");
  const exportJobsBody = document.getElementById("exportJobsBody");

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
    document.getElementById("barDone").style.width = `${(b.completed / denom) * 100}%`;
    document.getElementById("barProc").style.width = `${(proc / denom) * 100}%`;
    document.getElementById("barReview").style.width = `${(b.needs_review / denom) * 100}%`;
    document.getElementById("barFailed").style.width = `${(b.failed / denom) * 100}%`;
    document.getElementById("globalProgressNote").textContent =
      b.total > 0 ? `完成度 ${((b.completed / denom) * 100).toFixed(1)}% (${b.completed}/${b.total})` : "尚无页面";
  }

  async function refreshJobs() {
    const qs = state.currentBookId ? `?book_id=${state.currentBookId}&limit=30` : "?limit=30";
    const data = await apiJson(`/api/jobs${qs}`);
    const items = data.items || [];

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

  // Pipeline controls
  document.getElementById("pausePipelineBtn").addEventListener("click", async () => {
    await apiJson("/api/pipeline/pause", { method: "POST" });
    callbacks.onStatsNeeded();
  });
  document.getElementById("resumePipelineBtn").addEventListener("click", async () => {
    await apiJson("/api/pipeline/resume", { method: "POST" });
    callbacks.onStatsNeeded();
  });
  const retryFailedBtn = document.getElementById("retryFailedBtn");
  retryFailedBtn.addEventListener("click", async () => {
    await withButtonLoading(retryFailedBtn, "重试中...", async () => {
      const qs = state.currentBookId ? `?book_id=${state.currentBookId}` : "";
      const res = await apiJson(`/api/pipeline/retry-failed${qs}`, { method: "POST" });
      showToast(`已重新入队 ${res.retried} 个失败页面`, "ok");
      callbacks.onStatsNeeded();
    });
  });

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
    refreshPageList,
    selectPage,
    approveCurrent,
    stepSelection,
    getSelectedPageId: () => (selectedPage ? selectedPage.id : null),
  };
}
