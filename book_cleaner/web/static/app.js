/** Main workbench bootstrapper: book management, modals, theme toggle, SSE live updates, keyboard shortcuts. */

import {
  apiJson,
  closeTopLayer,
  el,
  enableDialogLightDismissFallback,
  initThemeToggle,
  showToast,
  wireTwoStepAction,
  withButtonLoading,
} from "/static/api.js";
import { createCaptureController } from "/static/capture.js";
import { createDataCleanController } from "/static/dataclean.js";
import { createReviewAndPipelineController } from "/static/review.js";

const state = {
  books: [],
  currentBookId: null,
  activeTab: "capture",
  recentCaptured: [],
};

const bookSelect = document.getElementById("bookSelect");
const pipelinePill = document.getElementById("pipelinePill");
const pipelinePillText = document.getElementById("pipelinePillText");
const activeOcrPill = document.getElementById("activeOcrPill");
const newBookDialog = document.getElementById("newBookDialog");
const settingsDialog = document.getElementById("settingsDialog");
const shortcutsDialog = document.getElementById("shortcutsDialog");
const tabBtns = Array.from(document.querySelectorAll(".tab-btn"));

initThemeToggle(document.getElementById("themeToggleBtn"), { compact: true });
enableDialogLightDismissFallback(newBookDialog);
enableDialogLightDismissFallback(settingsDialog);
enableDialogLightDismissFallback(shortcutsDialog);

const openShortcutsBtn = document.getElementById("openShortcutsBtn");
if (openShortcutsBtn && shortcutsDialog) {
  openShortcutsBtn.addEventListener("click", () => shortcutsDialog.showModal());
}

document.querySelectorAll("[data-close-dialog]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const dlg = document.getElementById(btn.dataset.closeDialog);
    if (dlg) closeTopLayer(dlg);
  });
});

let captureCtrl = null;
let reviewCtrl = null;
let dataCleanCtrl = null;

const OCR_ENGINE_LABELS = {
  rapidocr: "RapidOCR 离线",
  ppocrv5_mobile: "PP-OCRv5 轻量离线",
  ppocrv5_server: "PP-OCRv5 高精度离线",
  ollama_glm_ocr: "GLM-OCR 0.9B 离线",
  ollama_qwen25vl_3b: "Qwen2.5-VL 3B 离线",
  deepseek_ocr2_local: "DeepSeek-OCR 2 本地",
  deepseek_ocr2_api: "Vision API",
  hybrid_smart: "离线混合 OCR",
};

function updateActiveOcrPill(ocrCfg) {
  if (!activeOcrPill) return;
  const eng = (ocrCfg && ocrCfg.engine) || "rapidocr";
  const strict = !ocrCfg || ocrCfg.strict_offline !== false;
  const lockPrefix = strict ? "🔒 " : "";
  const label = OCR_ENGINE_LABELS[eng] || eng;
  activeOcrPill.textContent = `${lockPrefix}${label}`;
  activeOcrPill.className = `pill ${strict || eng !== "rapidocr" ? "pill-ok" : ""}`;
}

function updateTabBadges(bookStats) {
  const b = bookStats || {};
  const capBadge = document.getElementById("tabBadgeCapture");
  const pipeBadge = document.getElementById("tabBadgePipeline");
  const revBadge = document.getElementById("tabBadgeReview");

  if (capBadge) {
    capBadge.textContent = String(b.total || 0);
  }
  const activeQueue = (b.pending_clean || 0) + (b.pending_ocr || 0) + (b.pending_llm || 0);
  if (pipeBadge) {
    pipeBadge.textContent = String(activeQueue);
    pipeBadge.classList.toggle("hidden", activeQueue === 0);
  }
  const revCount = (b.needs_review || 0) + (b.failed || 0);
  if (revBadge) {
    revBadge.textContent = String(revCount);
    revBadge.classList.toggle("hidden", revCount === 0);
    revBadge.classList.toggle("is-warn", revCount > 0);
  }
}

function switchTab(tabKey, { focusTab = false, skipRefresh = false } = {}) {
  state.activeTab = tabKey;
  tabBtns.forEach((b) => {
    const active = b.dataset.tab === tabKey;
    b.classList.toggle("is-active", active);
    b.setAttribute("aria-selected", String(active));
    b.tabIndex = active ? 0 : -1;
    if (active) {
      b.scrollIntoView({ block: "nearest", inline: "nearest" });
      if (focusTab) b.focus();
    }
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("is-active", p.id === `tab-${tabKey}`);
  });
  if (tabKey === "review" && reviewCtrl && !skipRefresh) reviewCtrl.refreshPageList();
  if (tabKey === "dataclean" && dataCleanCtrl) {
    dataCleanCtrl.loadTemplates().then(() => dataCleanCtrl.refreshPreview());
  }
  if ((tabKey === "pipeline" || tabKey === "export") && reviewCtrl) {
    refreshStats();
    reviewCtrl.refreshJobs();
    if (tabKey === "pipeline") {
      reviewCtrl.refreshPipelinePages();
    }
    if (tabKey === "export" && dataCleanCtrl) {
      dataCleanCtrl.loadTemplates().then(() => dataCleanCtrl.refreshPreview());
    }
  }
}

tabBtns.forEach((b, idx) => {
  b.addEventListener("click", () => switchTab(b.dataset.tab));
  b.addEventListener("keydown", (e) => {
    let nextIdx = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") nextIdx = (idx + 1) % tabBtns.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") nextIdx = (idx - 1 + tabBtns.length) % tabBtns.length;
    else if (e.key === "Home") nextIdx = 0;
    else if (e.key === "End") nextIdx = tabBtns.length - 1;
    if (nextIdx !== null) {
      e.preventDefault();
      switchTab(tabBtns[nextIdx].dataset.tab, { focusTab: true });
    }
  });
});

async function refreshBooks(preferBookId = null) {
  const data = await apiJson("/api/books");
  state.books = data.items || [];
  bookSelect.replaceChildren();
  for (const b of state.books) {
    const count = (b.stats && b.stats.total) || 0;
    bookSelect.appendChild(
      el("option", { value: String(b.id), text: `${b.title} (${count}页)` })
    );
  }
  if (preferBookId && state.books.some((b) => b.id === preferBookId)) {
    state.currentBookId = preferBookId;
  } else if (!state.currentBookId || !state.books.some((b) => b.id === state.currentBookId)) {
    state.currentBookId = state.books[0] ? state.books[0].id : null;
  }
  if (state.currentBookId) {
    bookSelect.value = String(state.currentBookId);
    const activeBook = state.books.find((b) => b.id === state.currentBookId);
    if (activeBook && captureCtrl) {
      captureCtrl.applyBookProfile(activeBook.profile);
      captureCtrl.refreshMobileQr();
    }
  }
}

async function refreshStats() {
  if (!state.currentBookId) return;
  const stats = await apiJson(`/api/stats?book_id=${state.currentBookId}`);
  reviewCtrl.renderStats(stats);
  captureCtrl.updateBackendCamStatus(stats.camera);

  const b = stats.book || { total: 0, pending_clean: 0, pending_ocr: 0 };
  updateTabBadges(b);
  if (seedDemoBtn) {
    seedDemoBtn.classList.toggle("btn-primary", (b.total || 0) === 0);
  }
  if (stats.paused) {
    pipelinePill.className = "pill pill-warn";
    pipelinePillText.textContent = "流水线已暂停";
  } else {
    pipelinePill.className = "pill pill-ok";
    const activeQ = (b.pending_clean || 0) + (b.pending_ocr || 0) + (b.pending_llm || 0);
    pipelinePillText.textContent = activeQ > 0 ? `处理中 (${activeQ} 页排队)` : `就绪 (${b.total} 页)`;
  }
}

bookSelect.addEventListener("change", async () => {
  state.currentBookId = Number(bookSelect.value);
  const activeBook = state.books.find((b) => b.id === state.currentBookId);
  if (activeBook) captureCtrl.applyBookProfile(activeBook.profile);
  captureCtrl.refreshMobileQr();
  await Promise.all([refreshStats(), captureCtrl.loadRecentFromBook()]);
  if (state.activeTab === "review") await reviewCtrl.refreshPageList();
  if ((state.activeTab === "dataclean" || state.activeTab === "export") && dataCleanCtrl) {
    await dataCleanCtrl.refreshPreview();
  }
  if (state.activeTab === "pipeline") {
    await Promise.all([reviewCtrl.refreshJobs(), reviewCtrl.refreshPipelinePages()]);
  } else if (state.activeTab === "export") {
    await reviewCtrl.refreshJobs();
  }
});

wireTwoStepAction(document.getElementById("deleteBookBtn"), "确认删整本？", async () => {
  if (!state.currentBookId) return;
  try {
    const res = await apiJson(`/api/books/${state.currentBookId}`, { method: "DELETE" });
    await refreshBooks(res.fallback_book_id || null);
    await Promise.all([refreshStats(), captureCtrl.loadRecentFromBook()]);
    if (state.activeTab === "review") await reviewCtrl.refreshPageList();
    if ((state.activeTab === "dataclean" || state.activeTab === "export") && dataCleanCtrl) {
      await dataCleanCtrl.refreshPreview();
    }
    if (state.activeTab === "pipeline") {
      await Promise.all([reviewCtrl.refreshJobs(), reviewCtrl.refreshPipelinePages()]);
    } else if (state.activeTab === "export") {
      await reviewCtrl.refreshJobs();
    }
    showToast("已删除书目并清理关联文件", "ok");
  } catch (err) {
    showToast(err.message, "danger");
  }
});

// New Book Modal
document.getElementById("newBookBtn").addEventListener("click", () => {
  document.getElementById("newBookForm").reset();
  newBookDialog.showModal();
});
document.getElementById("newBookForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const submitBtn = e.target.querySelector('button[type="submit"]');
  await withButtonLoading(submitBtn, "创建中...", async () => {
    try {
      const created = await apiJson("/api/books", {
        method: "POST",
        body: {
          title: document.getElementById("newBookTitleInput").value,
          author: document.getElementById("newBookAuthorInput").value,
          isbn: document.getElementById("newBookIsbnInput").value,
        },
      });
      await closeTopLayer(newBookDialog);
      await refreshBooks(created.id);
      await Promise.all([refreshStats(), captureCtrl.loadRecentFromBook()]);
      showToast(`已创建书目《${created.title}》`, "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });
});

// Demo seed button
const seedDemoBtn = document.getElementById("seedDemoBtn");
seedDemoBtn.addEventListener("click", async () => {
  if (!state.currentBookId) return;
  await withButtonLoading(seedDemoBtn, "生成演示页中...", async () => {
    try {
      const res = await apiJson(`/api/books/${state.currentBookId}/seed-demo`, { method: "POST" });
      showToast(`已生成 ${res.created_pages} 页演示样本（含双页拆单、弯页手指、泛黄阴影及模糊/重复测试）`, "ok");
      await refreshBooks(state.currentBookId);
      await Promise.all([refreshStats(), captureCtrl.loadRecentFromBook()]);
      if (state.activeTab === "review") await reviewCtrl.refreshPageList();
    } catch (err) {
      showToast(err.message, "danger");
    }
  });
});

// Settings, Strict Offline Lock & Multi-Tier Offline OCR Model Hub
let cachedHubCatalog = null;

function syncStrictOfflineUi(strictEnabled) {
  const badge = document.getElementById("strictOfflineBadge");
  const desc = document.getElementById("strictOfflineDesc");
  const apiNote = document.getElementById("strictOfflineApiNote");
  const apiOpt = document.querySelector('#ocrEngineSelect option[value="deepseek_ocr2_api"]');
  if (badge) {
    badge.textContent = strictEnabled ? "已锁定 · 禁止外传" : "已解锁 · 允许外部接口";
    badge.className = `pill ${strictEnabled ? "pill-ok" : "pill-warn"}`;
  }
  if (desc) {
    desc.textContent = strictEnabled
      ? "开启后：图片识别、表格抽取与模型推理 100% 在本地运行，系统自动拦截所有非 127.0.0.1 / localhost 外部云端请求。"
      : "注意：当前允许访问外部云端接口，仅限处理非涉密公开文档！涉密文件请重新打开此开关。";
  }
  if (apiNote) {
    apiNote.textContent = strictEnabled
      ? "当前处于涉密严格离线模式：除本机 127.0.0.1 / localhost 地址外，系统将自动拦截外部视觉 API 请求。"
      : "外部接口已解锁：可使用云端 DeepSeek-OCR 2 / OpenAI 接口。";
    apiNote.style.color = strictEnabled ? "var(--ok)" : "var(--warn)";
  }
  if (apiOpt) {
    apiOpt.textContent = strictEnabled
      ? "Vision API（涉密模式已锁定外网，仅可连 127.0.0.1）"
      : "DeepSeek-OCR 2 视觉 API（云端 / 局域网）";
  }
}

function syncEngineSelectWithHub(catalog) {
  if (!catalog || !Array.isArray(catalog.models)) return;
  const engSel = document.getElementById("ocrEngineSelect");
  if (!engSel) return;
  const byEng = {};
  for (const m of catalog.models) byEng[m.engine_value] = m;

  Array.from(engSel.options).forEach((opt) => {
    const m = byEng[opt.value];
    if (!m || m.runner === "builtin_onnx") return;
    opt.disabled = !m.installed;
  });
}

function renderOcrModelCards(catalog) {
  if (!catalog) return;
  cachedHubCatalog = catalog;
  const hw = catalog.hardware || {};
  const models = catalog.models || [];

  const hwSummary = document.getElementById("hwSummaryText");
  const recBox = document.getElementById("ocrHubRecommendation");
  const installedBadge = document.getElementById("hubInstalledBadge");
  const cardList = document.getElementById("ocrModelCardList");
  const forceChk = document.getElementById("addonForceChk");
  const mirrorChk = document.getElementById("addonMirrorChk");
  const currentEngine = document.getElementById("ocrEngineSelect")?.value || "rapidocr";

  if (hwSummary) hwSummary.textContent = hw.summary || "";
  if (recBox) recBox.textContent = catalog.recommendation || "";

  const installedCnt = models.filter((m) => m.installed).length;
  if (installedBadge) {
    installedBadge.textContent = `已有 ${installedCnt} 个可用离线引擎`;
    installedBadge.className = "pill pill-ok";
  }
  syncEngineSelectWithHub(catalog);

  if (!cardList) return;
  cardList.replaceChildren();
  const forceOn = Boolean(forceChk && forceChk.checked);
  const useMirror = !mirrorChk || Boolean(mirrorChk.checked);

  for (const m of models) {
    const isInstalled = Boolean(m.installed);
    const isCurrent = currentEngine === m.engine_value;
    const stateStr = String(m.state || (isInstalled ? "installed" : "not_installed"));

    let fitBadgeText = "硬件适配";
    let fitBadgeCls = "pill";
    if (m.fit === "recommended") {
      fitBadgeText = "★ 最适合当前电脑";
      fitBadgeCls = "pill pill-ok";
    } else if (m.fit === "needs_runtime") {
      fitBadgeText = "需启动 Ollama";
      fitBadgeCls = "pill pill-warn";
    } else if (m.fit === "insufficient") {
      fitBadgeText = "硬件不支持";
      fitBadgeCls = "pill pill-danger";
    } else if (m.fit === "compatible") {
      fitBadgeText = "✓ 可离线运行";
      fitBadgeCls = "pill pill-ok";
    }

    const sizeTxt = m.size_mb > 0 ? (m.size_mb >= 1000 ? `${(m.size_mb / 1024).toFixed(1)} GB` : `${Math.round(m.size_mb)} MB`) : "0 MB 内置";

    const progressSection = [];
    if (stateStr === "downloading" || stateStr === "paused" || stateStr === "failed") {
      const pct = Math.max(0, Math.min(100, Number(m.percent || 0)));
      const speedTxt = m.speed_mb_s > 0 ? ` · ${m.speed_mb_s} MB/s` : "";
      const mbTxt = m.total_mb > 0 ? `${m.downloaded_mb || 0}/${m.total_mb} MB` : "";
      progressSection.push(
        el("div", { class: "ocr-progress-row" }, [
          el("div", { class: "stacked-bar", style: "height:7px;margin:0;flex:1;" }, [
            el("div", {
              class: stateStr === "paused" ? "seg-review" : stateStr === "failed" ? "seg-fail" : "seg-done",
              style: `width:${pct}%`,
            }),
          ]),
          el("span", { class: "mono muted-note", style: "font-size:11.5px;", text: `${pct}% ${mbTxt}${speedTxt}` }),
        ])
      );
      if (m.message) {
        progressSection.push(
          el("div", {
            class: "muted-note",
            style: `font-size:11.5px;color:${stateStr === "failed" ? "var(--danger)" : "var(--muted)"};`,
            text: m.message,
          })
        );
      }
    }

    const reasonNotes = (m.reasons || []).map((r) =>
      el("div", { class: "muted-note", style: "font-size:11.5px;color:var(--warn);", text: `• ${r}` })
    );

    const actions = [];
    if (isInstalled) {
      const useBtn = el("button", {
        type: "button",
        class: `btn btn-sm ${isCurrent ? "btn-soft" : "btn-primary"}`,
        disabled: isCurrent,
        text: isCurrent ? "当前正在使用 ✓" : "设为当前引擎",
        onclick: () => {
          const sel = document.getElementById("ocrEngineSelect");
          if (sel) sel.value = m.engine_value;
          renderOcrModelCards(catalog);
          showToast(`已切换候选引擎为「${m.name}」，点击下方「保存全部设置」生效`, "ok");
        },
      });
      actions.push(useBtn);
      if (m.runner !== "builtin_onnx") {
        actions.push(
          el("button", {
            type: "button",
            class: "btn btn-sm btn-danger-ghost",
            text: "卸载释放空间",
            onclick: async () => {
              try {
                const updated = await apiJson(`/api/ocr/models/${m.model_id}`, { method: "DELETE" });
                renderOcrModelCards(updated);
                showToast(`已卸载「${m.name}」`, "ok");
              } catch (err) {
                showToast(err.message, "danger");
              }
            },
          })
        );
      }
    } else if (stateStr === "downloading") {
      actions.push(
        el("button", {
          type: "button",
          class: "btn btn-sm btn-soft",
          text: "暂停下载",
          onclick: async () => {
            try {
              const updated = await apiJson(`/api/ocr/models/${m.model_id}/pause`, { method: "POST" });
              renderOcrModelCards(updated);
            } catch (err) {
              showToast(err.message, "danger");
            }
          },
        }),
        el("button", {
          type: "button",
          class: "btn btn-sm btn-danger-ghost",
          text: "取消",
          onclick: async () => {
            try {
              const updated = await apiJson(`/api/ocr/models/${m.model_id}/cancel`, { method: "POST" });
              renderOcrModelCards(updated);
            } catch (err) {
              showToast(err.message, "danger");
            }
          },
        })
      );
    } else if (stateStr === "paused") {
      actions.push(
        el("button", {
          type: "button",
          class: "btn btn-sm btn-primary",
          text: "继续下载",
          onclick: async () => {
            try {
              const updated = await apiJson(`/api/ocr/models/${m.model_id}/download`, {
                method: "POST",
                body: { use_cn_mirror: useMirror, force: forceOn },
              });
              renderOcrModelCards(updated);
            } catch (err) {
              showToast(err.message, "warn");
            }
          },
        }),
        el("button", {
          type: "button",
          class: "btn btn-sm btn-danger-ghost",
          text: "取消",
          onclick: async () => {
            try {
              const updated = await apiJson(`/api/ocr/models/${m.model_id}/cancel`, { method: "POST" });
              renderOcrModelCards(updated);
            } catch (err) {
              showToast(err.message, "danger");
            }
          },
        })
      );
    } else if (m.runner !== "builtin_onnx") {
      const blocked = !m.can_download && !forceOn;
      actions.push(
        el("button", {
          type: "button",
          class: `btn btn-sm ${m.fit === "recommended" ? "btn-primary" : ""}`,
          disabled: blocked,
          text: stateStr === "failed" ? "重试下载" : "下载模型",
          onclick: async () => {
            try {
              const updated = await apiJson(`/api/ocr/models/${m.model_id}/download`, {
                method: "POST",
                body: { use_cn_mirror: useMirror, force: forceOn },
              });
              renderOcrModelCards(updated);
              showToast(`开始后台下载「${m.name}」（可随时暂停）`, "ok");
            } catch (err) {
              showToast(err.message, "warn");
            }
          },
        })
      );
    }

    const strengthTags = (m.strengths || []).map((s) => el("span", { class: "badge-mode", text: s }));

    cardList.appendChild(
      el("div", { class: `ocr-model-card${m.is_recommended ? " is-recommended" : ""}` }, [
        el("div", { class: "ocr-model-head" }, [
          el("div", { class: "toolbar-row", style: "gap:6px;" }, [
            el("strong", { style: "font-size:13px;", text: m.name }),
            el("span", { class: "badge-mode mono", text: sizeTxt }),
          ]),
          el("span", { class: fitBadgeCls, text: isInstalled ? "已就绪 ✓" : fitBadgeText }),
        ]),
        el("div", { class: "muted-note", style: "font-size:12px;", text: m.summary }),
        el("div", { class: "toolbar-row", style: "gap:5px;" }, strengthTags),
        ...reasonNotes,
        ...progressSection,
        el("div", { class: "ocr-card-actions" }, actions),
      ])
    );
  }
}

async function refreshOcrHub(recheck = false) {
  const url = recheck ? "/api/ocr/models/check" : "/api/ocr/models";
  const catalog = await apiJson(url, { method: recheck ? "POST" : "GET" });
  renderOcrModelCards(catalog);
  return catalog;
}

document.getElementById("ocrStrictOfflineChk")?.addEventListener("change", (e) => {
  syncStrictOfflineUi(Boolean(e.target.checked));
});

document.getElementById("openSettingsBtn").addEventListener("click", async () => {
  try {
    const [cfg, catalog] = await Promise.all([apiJson("/api/settings"), refreshOcrHub(false)]);
    const ocr = cfg.ocr || {};
    const strict = ocr.strict_offline !== false;
    const strictChk = document.getElementById("ocrStrictOfflineChk");
    if (strictChk) strictChk.checked = strict;
    syncStrictOfflineUi(strict);

    const engSel = document.getElementById("ocrEngineSelect");
    if (engSel) engSel.value = ocr.engine || "rapidocr";
    const ollamaInp = document.getElementById("ocrOllamaBaseUrlInput");
    if (ollamaInp) ollamaInp.value = ocr.ollama_base_url || "http://127.0.0.1:11434";

    document.getElementById("ocrApiBaseUrlInput").value = ocr.api_base_url || "https://api.siliconflow.cn/v1";
    document.getElementById("ocrApiModelInput").value = ocr.api_model || "deepseek-ai/DeepSeek-OCR-2";
    document.getElementById("ocrApiKeyInput").value = ocr.api_key || "";
    document.getElementById("ocrHybridConfInput").value = String(ocr.hybrid_conf_threshold ?? 0.85);
    document.getElementById("ocrHybridTablesChk").checked = ocr.hybrid_upgrade_tables !== false;
    document.getElementById("ocrApiTestResult").textContent = "";

    const llm = cfg.llm || {};
    document.getElementById("llmModeSelect").value = llm.mode || "off";
    document.getElementById("llmBaseUrlInput").value = llm.base_url || "http://127.0.0.1:11434/v1";
    document.getElementById("llmModelInput").value = llm.model || "qwen2.5:7b";
    document.getElementById("llmApiKeyInput").value = llm.api_key || "";
    document.getElementById("llmLowConfInput").value = String(llm.low_conf_threshold ?? 0.85);
    document.getElementById("llmTestResult").textContent = "";
    renderOcrModelCards(catalog);
    updateActiveOcrPill(ocr);
    settingsDialog.showModal();
  } catch (err) {
    showToast(err.message, "danger");
  }
});

const recheckHwBtn = document.getElementById("recheckHwBtn");
recheckHwBtn.addEventListener("click", async () => {
  await withButtonLoading(recheckHwBtn, "体检中...", async () => {
    try {
      await refreshOcrHub(true);
      showToast("已完成本机硬件与离线模型体检", "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });
});

document.getElementById("addonForceChk").addEventListener("change", () => {
  if (cachedHubCatalog) renderOcrModelCards(cachedHubCatalog);
});

document.getElementById("ocrEngineSelect").addEventListener("change", () => {
  if (cachedHubCatalog) renderOcrModelCards(cachedHubCatalog);
});

function readOcrForm() {
  return {
    engine: document.getElementById("ocrEngineSelect").value,
    strict_offline: document.getElementById("ocrStrictOfflineChk")?.checked !== false,
    ollama_base_url: (document.getElementById("ocrOllamaBaseUrlInput")?.value || "http://127.0.0.1:11434").trim(),
    api_base_url: document.getElementById("ocrApiBaseUrlInput").value.trim(),
    api_model: document.getElementById("ocrApiModelInput").value.trim(),
    api_key: document.getElementById("ocrApiKeyInput").value.trim(),
    hybrid_conf_threshold: Number(document.getElementById("ocrHybridConfInput").value || 0.85),
    hybrid_upgrade_tables: document.getElementById("ocrHybridTablesChk").checked,
  };
}

function readLlmForm() {
  return {
    mode: document.getElementById("llmModeSelect").value,
    base_url: document.getElementById("llmBaseUrlInput").value.trim(),
    model: document.getElementById("llmModelInput").value.trim(),
    api_key: document.getElementById("llmApiKeyInput").value.trim(),
    low_conf_threshold: Number(document.getElementById("llmLowConfInput").value || 0.85),
  };
}

const testOcrApiBtn = document.getElementById("testOcrApiBtn");
testOcrApiBtn.addEventListener("click", async () => {
  const out = document.getElementById("ocrApiTestResult");
  out.textContent = "正在测试视觉 API 连通性...";
  await withButtonLoading(testOcrApiBtn, "测试中...", async () => {
    try {
      const o = readOcrForm();
      const res = await apiJson("/api/ocr/test-api", {
        method: "POST",
        body: {
          api_base_url: o.api_base_url,
          api_key: o.api_key,
          api_model: o.api_model,
          strict_offline: o.strict_offline,
        },
      });
      out.textContent = res.message;
      out.style.color = res.ok ? "var(--ok)" : "var(--danger)";
    } catch (err) {
      out.textContent = err.message;
      out.style.color = "var(--danger)";
    }
  });
});

const testLlmBtn = document.getElementById("testLlmBtn");
testLlmBtn.addEventListener("click", async () => {
  const out = document.getElementById("llmTestResult");
  out.textContent = "正在测试接口连通性...";
  await withButtonLoading(testLlmBtn, "测试中...", async () => {
    try {
      const o = readOcrForm();
      const res = await apiJson("/api/settings/test-llm", {
        method: "POST",
        body: { ...readLlmForm(), strict_offline: o.strict_offline },
      });
      out.textContent = res.message;
      out.style.color = res.ok ? "var(--ok)" : "var(--danger)";
    } catch (err) {
      out.textContent = err.message;
      out.style.color = "var(--danger)";
    }
  });
});

document.getElementById("settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const submitBtn = e.target.querySelector('button[type="submit"]');
  await withButtonLoading(submitBtn, "保存中...", async () => {
    try {
      const ocrCfg = readOcrForm();
      await apiJson("/api/settings", { method: "PUT", body: { ocr: ocrCfg, llm: readLlmForm() } });
      updateActiveOcrPill(ocrCfg);
      await closeTopLayer(settingsDialog);
      showToast("OCR 引擎与大模型设置已保存", "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });
});

// Keyboard shortcuts (Ctrl+S in review editor; Space/Enter in capture; J/K/A in review)
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && state.activeTab === "review" && !settingsDialog.open) {
    e.preventDefault();
    document.getElementById("saveTextBtn").click();
    return;
  }

  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || newBookDialog.open || settingsDialog.open) {
    return;
  }
  if (e.key === "?" && shortcutsDialog) {
    e.preventDefault();
    if (shortcutsDialog.open) closeTopLayer(shortcutsDialog);
    else shortcutsDialog.showModal();
    return;
  }
  if (shortcutsDialog && shortcutsDialog.open) {
    return;
  }
  if (state.activeTab === "capture") {
    if (e.code === "Space" || e.code === "Enter") {
      e.preventDefault();
      captureCtrl.triggerShutter();
    } else if (e.code === "Backspace") {
      e.preventDefault();
      captureCtrl.undoLast();
    }
  } else if (state.activeTab === "review") {
    if (e.key === "j" || e.key === "J" || e.key === "ArrowDown") {
      e.preventDefault();
      reviewCtrl.stepSelection(1);
    } else if (e.key === "k" || e.key === "K" || e.key === "ArrowUp") {
      e.preventDefault();
      reviewCtrl.stepSelection(-1);
    } else if (e.key === "a" || e.key === "A") {
      e.preventDefault();
      reviewCtrl.approveCurrent();
    }
  }
});

// Live SSE Event Stream with debounced UI refresh
let sseRefreshTimer = null;
function connectSse() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    if (!ev.data || ev.data === "{}") return;
    let payload = null;
    try {
      payload = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (payload.type === "ocr_hub_progress" || payload.type === "addon_progress") {
      if (settingsDialog.open) {
        const d = payload.data || {};
        if (payload.type === "ocr_hub_progress" && cachedHubCatalog && Array.isArray(cachedHubCatalog.models) && d.model_id) {
          const target = cachedHubCatalog.models.find((m) => m.model_id === d.model_id);
          if (target && d.state === "downloading") {
            Object.assign(target, {
              state: d.state,
              percent: d.percent,
              downloaded_mb: d.downloaded_mb,
              total_mb: d.total_mb,
              speed_mb_s: d.speed_mb_s,
              message: d.message,
            });
            renderOcrModelCards(cachedHubCatalog);
            return;
          }
        }
        refreshOcrHub(false).catch(() => {});
      }
      return;
    }
    clearTimeout(sseRefreshTimer);
    sseRefreshTimer = setTimeout(async () => {
      await refreshStats();
      await refreshBooks(state.currentBookId);
      await captureCtrl.loadRecentFromBook();
      if (payload.type === "job_progress") {
        await reviewCtrl.refreshJobs();
      }
      if (state.activeTab === "pipeline") {
        await reviewCtrl.refreshPipelinePages();
      }
      if (state.activeTab === "review") {
        const curId = reviewCtrl.getSelectedPageId();
        await reviewCtrl.refreshPageList(curId);
      }
    }, 280);
  };
  es.onerror = () => {
    es.close();
    setTimeout(connectSse, 3000);
  };
}

async function init() {
  captureCtrl = createCaptureController(state, {
    onCaptured: () => {
      refreshStats();
      refreshBooks(state.currentBookId);
    },
    onBookUpdated: (updated) => {
      const idx = state.books.findIndex((b) => b.id === updated.id);
      if (idx >= 0) state.books[idx] = updated;
    },
    onJobsUpdated: () => reviewCtrl.refreshJobs(),
    onJumpToReview: async (pageId) => {
      reviewCtrl.applyReviewFilter("ALL");
      switchTab("review", { skipRefresh: true });
      await reviewCtrl.refreshPageList(pageId);
    },
  });

  reviewCtrl = createReviewAndPipelineController(state, {
    onStatsNeeded: () => {
      refreshStats();
      refreshBooks(state.currentBookId);
    },
    onPagesChanged: () => {
      refreshStats();
      refreshBooks(state.currentBookId);
      captureCtrl.loadRecentFromBook();
    },
    onJumpToTab: (tabKey) => {
      switchTab(tabKey);
    },
    onJumpToReview: async (pageId, filterStatus = "ALL") => {
      reviewCtrl.applyReviewFilter(filterStatus);
      switchTab("review", { skipRefresh: true });
      await reviewCtrl.refreshPageList(pageId);
    },
  });

  dataCleanCtrl = createDataCleanController(state, {
    onExportTriggered: () => {
      switchTab("export");
      setTimeout(() => reviewCtrl.refreshJobs(), 450);
    },
    onCleaned: (summary) => {
      const dcBadge = document.getElementById("tabBadgeDataclean");
      if (dcBadge && summary) {
        const n = (summary.entries || 0) + (summary.table_rows || 0);
        dcBadge.textContent = String(n);
        dcBadge.classList.toggle("hidden", n === 0);
      }
    },
  });

  await refreshBooks();
  await dataCleanCtrl.loadTemplates();
  await dataCleanCtrl.refreshPreview();
  await refreshStats();
  apiJson("/api/settings").then((cfg) => updateActiveOcrPill(cfg.ocr)).catch(() => {});
  await captureCtrl.loadRecentFromBook();
  connectSse();
}

init().catch((err) => showToast(`初始化失败: ${err.message}`, "danger"));
