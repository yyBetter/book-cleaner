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
  deepseek_ocr2_local: "DeepSeek-OCR 2 本地",
  deepseek_ocr2_api: "DeepSeek-OCR 2 API",
  hybrid_smart: "智能混合 OCR",
};

function updateActiveOcrPill(ocrCfg) {
  if (!activeOcrPill) return;
  const eng = (ocrCfg && ocrCfg.engine) || "rapidocr";
  activeOcrPill.textContent = OCR_ENGINE_LABELS[eng] || eng;
  activeOcrPill.className = `pill ${eng === "rapidocr" ? "" : "pill-ok"}`;
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

// Settings, OCR Engine & DeepSeek-OCR 2 On-Demand Add-on Modal
function renderAddonStatus(st) {
  if (!st) return;
  const hw = st.hardware || {};
  const badge = document.getElementById("addonStateBadge");
  const hwSummary = document.getElementById("hwSummaryText");
  const hwDetails = document.getElementById("hwDetailList");
  const progBar = document.getElementById("addonProgressBar");
  const progText = document.getElementById("addonProgressText");
  const logTail = document.getElementById("addonLogTail");
  const installBtn = document.getElementById("installAddonBtn");
  const cancelBtn = document.getElementById("cancelAddonBtn");
  const uninstallBtn = document.getElementById("uninstallAddonBtn");
  const forceChk = document.getElementById("addonForceChk");
  const localOpt = document.querySelector('#ocrEngineSelect option[value="deepseek_ocr2_local"]');

  const installed = Boolean(st.installed);
  const installing = st.install_state === "installing";
  if (installed) {
    badge.textContent = `已安装 (${st.disk_usage_mb} MB)`;
    badge.className = "pill pill-ok";
  } else if (installing) {
    badge.textContent = `安装中 ${st.percent || 0}%`;
    badge.className = "pill pill-warn";
  } else if (st.install_state === "failed") {
    badge.textContent = "安装失败";
    badge.className = "pill pill-danger";
  } else {
    badge.textContent = "未安装（可按需下载）";
    badge.className = "pill";
  }

  if (localOpt) {
    localOpt.disabled = !installed;
    localOpt.textContent = installed
      ? "DeepSeek-OCR 2 本地插件包（已安装就绪）"
      : "DeepSeek-OCR 2 本地插件包（需先检测并下载）";
  }

  const gpus = hw.gpus || [];
  const gpuTxt = gpus.length ? `${gpus[0].name} (${gpus[0].vram_mb} MB)` : "未检测到 NVIDIA GPU";
  hwSummary.textContent = `平台: ${hw.platform || "-"} · 显卡: ${gpuTxt} · 内存: ${hw.ram_gb || 0} GB · 可用空间: ${hw.free_disk_gb || 0} GB`;
  hwSummary.style.color = hw.can_run_local ? "var(--ok)" : "var(--warn)";

  hwDetails.replaceChildren();
  hwDetails.appendChild(el("div", { text: hw.recommendation || "" }));
  for (const reason of hw.reasons || []) {
    hwDetails.appendChild(el("div", { style: "color:var(--warn);", text: `• ${reason}` }));
  }

  progBar.style.width = `${st.percent || 0}%`;
  progText.textContent = st.message || "";
  if (st.log_tail) {
    logTail.textContent = st.log_tail;
    logTail.classList.remove("hidden");
  } else {
    logTail.classList.add("hidden");
  }

  installBtn.classList.toggle("hidden", installing || installed);
  installBtn.disabled = !hw.can_run_local && !forceChk.checked;
  cancelBtn.classList.toggle("hidden", !installing);
  uninstallBtn.classList.toggle("hidden", !installed && st.disk_usage_mb <= 0);
}

async function refreshAddonStatus(recheck = false) {
  const url = recheck ? "/api/ocr/addons/deepseek/check" : "/api/ocr/addons/deepseek";
  const st = await apiJson(url, { method: recheck ? "POST" : "GET" });
  renderAddonStatus(st);
  return st;
}

document.getElementById("openSettingsBtn").addEventListener("click", async () => {
  try {
    const [cfg, addonSt] = await Promise.all([apiJson("/api/settings"), refreshAddonStatus(false)]);
    const ocr = cfg.ocr || {};
    document.getElementById("ocrEngineSelect").value = ocr.engine || "rapidocr";
    document.getElementById("ocrApiBaseUrlInput").value = ocr.api_base_url || "https://api.siliconflow.cn/v1";
    document.getElementById("ocrApiModelInput").value = ocr.api_model || "deepseek-ai/DeepSeek-OCR-2";
    document.getElementById("ocrApiKeyInput").value = ocr.api_key || "";
    document.getElementById("ocrHybridConfInput").value = String(ocr.hybrid_conf_threshold ?? 0.85);
    document.getElementById("ocrHybridTablesChk").checked = ocr.hybrid_upgrade_tables !== false;
    document.getElementById("ocrApiTestResult").textContent = "";

    const llm = cfg.llm || {};
    document.getElementById("llmModeSelect").value = llm.mode || "off";
    document.getElementById("llmBaseUrlInput").value = llm.base_url || "https://api.openai.com/v1";
    document.getElementById("llmModelInput").value = llm.model || "gpt-4o-mini";
    document.getElementById("llmApiKeyInput").value = llm.api_key || "";
    document.getElementById("llmLowConfInput").value = String(llm.low_conf_threshold ?? 0.85);
    document.getElementById("llmTestResult").textContent = "";
    renderAddonStatus(addonSt);
    updateActiveOcrPill(ocr);
    settingsDialog.showModal();
  } catch (err) {
    showToast(err.message, "danger");
  }
});

const recheckHwBtn = document.getElementById("recheckHwBtn");
recheckHwBtn.addEventListener("click", async () => {
  await withButtonLoading(recheckHwBtn, "检测中...", async () => {
    try {
      await refreshAddonStatus(true);
      showToast("已完成本机硬件检测", "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  });
});

document.getElementById("addonForceChk").addEventListener("change", () => {
  refreshAddonStatus(false).catch(() => {});
});

document.getElementById("installAddonBtn").addEventListener("click", async () => {
  try {
    const st = await apiJson("/api/ocr/addons/deepseek/install", {
      method: "POST",
      body: {
        use_cn_mirror: document.getElementById("addonMirrorChk").checked,
        force: document.getElementById("addonForceChk").checked,
      },
    });
    renderAddonStatus(st);
    showToast("DeepSeek-OCR 2 插件包已开始后台下载安装", "ok");
  } catch (err) {
    showToast(err.message, "warn");
  }
});

document.getElementById("cancelAddonBtn").addEventListener("click", async () => {
  const st = await apiJson("/api/ocr/addons/deepseek/cancel", { method: "POST" });
  renderAddonStatus(st);
});

document.getElementById("uninstallAddonBtn").addEventListener("click", async () => {
  const st = await apiJson("/api/ocr/addons/deepseek", { method: "DELETE" });
  renderAddonStatus(st);
  showToast("已卸载插件包并释放空间", "ok");
});

function readOcrForm() {
  return {
    engine: document.getElementById("ocrEngineSelect").value,
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
  out.textContent = "正在测试 DeepSeek 视觉 API 连通性...";
  await withButtonLoading(testOcrApiBtn, "测试中...", async () => {
    try {
      const o = readOcrForm();
      const res = await apiJson("/api/ocr/test-api", {
        method: "POST",
        body: { api_base_url: o.api_base_url, api_key: o.api_key, api_model: o.api_model },
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
      const res = await apiJson("/api/settings/test-llm", { method: "POST", body: readLlmForm() });
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
    if (payload.type === "addon_progress") {
      renderAddonStatus(payload.data);
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
