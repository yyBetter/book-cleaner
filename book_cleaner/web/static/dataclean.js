/** Stage 2 Multi-Sheet Template Pack, Visual Sorting, Alias Learning & CatBoost Controller. */

import {
  apiJson,
  closeTopLayer,
  el,
  enableDialogLightDismissFallback,
  showToast,
  wireTwoStepAction,
  withButtonLoading,
} from "/static/api.js";

const BUILTIN_FIELD_OPTIONS = [
  { value: "batch_id", label: "批次号(自动识别)" },
  { value: "record_dim", label: "记录维度(SOP/化验/过程)" },
  { value: "book_title", label: "书名/卷宗" },
  { value: "page_no", label: "页码" },
  { value: "chapter", label: "所属章节" },
  { value: "entry_title", label: "条目标题" },
  { value: "clean_content", label: "清洗后正文" },
  { value: "ocr_confidence", label: "识别置信度" },
  { value: "qa_status", label: "质检状态" },
  { value: "author", label: "作者" },
  { value: "isbn", label: "设备/卷宗编号" },
  { value: "source_ref", label: "来源文件名" },
];

const MODE_LABELS = {
  record: "单条记录表",
  table: "多行明细表",
  text: "SOP / 文本",
};

const ROLE_OPTIONS = [
  { value: "meta_id", label: "批次主键 (meta_id)" },
  { value: "time_split", label: "时间/日期列" },
  { value: "cat_feature", label: "类别特征 X" },
  { value: "num_feature", label: "数值特征 X" },
  { value: "text_feature", label: "文本特征 X" },
  { value: "target_y", label: "★ 预测目标 y" },
];

const ALIGN_STATUS_META = {
  matched_exact: { label: "列名精确匹配", cls: "pill pill-ok" },
  matched_alias: { label: "已记住别名", cls: "pill pill-ok" },
  matched_fuzzy: { label: "智能相似匹配", cls: "pill pill-ok" },
  unmatched: { label: "待选目标列", cls: "pill pill-warn" },
};

export function createDataCleanController(state, callbacks) {
  let templates = [];
  let activeTemplate = null;
  let matchData = null;
  let selectedDrawerId = null; // sheet_id | "__unassigned__" | "__ignored__" | "__catboost__"
  let activeSubTab = "mapping"; // "mapping" | "preview" | "schema"
  let schemaDraftSheet = null;

  // Legacy mode state
  let editingColumns = [];
  let previewKind = "ENTRY";
  let previewOffset = 0;
  const previewLimit = 40;
  let previewTotal = 0;

  // Shared topbar & export refs
  const tplSelect = document.getElementById("excelTemplateSelect");
  const exportTplSelect = document.getElementById("exportExcelTemplateSelect");
  const packWorkbench = document.getElementById("packWorkbench");
  const legacyWorkbench = document.getElementById("legacyCleanWorkbench");
  const coverageBadge = document.getElementById("packCoverageBadge");
  const packPresetPills = document.getElementById("packPresetPills");
  const packScenarioDesc = document.getElementById("packScenarioDesc");
  const newTemplatePresetSelect = document.getElementById("newTemplatePresetSelect");
  const newTemplatePresetHint = document.getElementById("newTemplatePresetHint");

  const PRESET_SHORT_LABELS = {
    "多表批次记录与多目标 CatBoost 模板包": "制造 / 批次记录 CatBoost (5表)",
    "实验室检测与研发实验本模板包": "实验室检测 / 研发实验本 (4表)",
    "财务凭证与合同台账模板包": "财务凭证 / 合同台账 (4表)",
  };

  function renderScenarioBar() {
    if (packScenarioDesc) {
      packScenarioDesc.textContent =
        (activeTemplate && activeTemplate.description) ||
        "点击上方模板下拉或左侧样板快速切换，10 秒对号入座";
    }
    if (!packPresetPills) return;
    packPresetPills.replaceChildren();
    const packTpls = templates.filter((t) => Array.isArray(t.sheets) && t.sheets.length > 0);
    for (const t of packTpls) {
      const isAct = activeTemplate && activeTemplate.id === t.id;
      const shortLabel = PRESET_SHORT_LABELS[t.name] || `${t.name} (${t.sheets.length}表)`;
      packPresetPills.appendChild(
        el("button", {
          type: "button",
          class: `btn btn-compact ${isAct ? "btn-primary" : "btn-soft"}`,
          title: t.description || t.name,
          text: shortLabel,
          onclick: () => {
            activeTemplate = t;
            tplSelect.value = String(t.id);
            exportTplSelect.value = String(t.id);
            selectedDrawerId = null;
            applyTemplateToForm(t);
            refreshPreview();
          },
        })
      );
    }
  }

  // Multi-sheet 3-col refs
  const packSheetList = document.getElementById("packSheetList");
  const packPagesHeading = document.getElementById("packPagesHeading");
  const packPagesCount = document.getElementById("packPagesCount");
  const packPageList = document.getElementById("packPageList");
  const packDetailMeta = document.getElementById("packDetailMeta");
  const subMappingPane = document.getElementById("packSubMapping");
  const subPreviewPane = document.getElementById("packSubPreview");
  const subSchemaPane = document.getElementById("packSubSchema");

  // Export tab dynamic summary refs
  const exportSheetTabStrip = document.getElementById("exportSheetTabStrip");
  const exportCoverageSummary = document.getElementById("exportCoverageSummary");
  const exportCatboostHint = document.getElementById("exportCatboostHint");

  // Legacy fallback refs
  const entrySheetInput = document.getElementById("entrySheetNameInput");
  const tableSheetInput = document.getElementById("tableSheetNameInput");
  const schemaColList = document.getElementById("schemaColList");
  const previewHead = document.getElementById("dcPreviewHead");
  const previewBody = document.getElementById("dcPreviewBody");
  const pagerInfo = document.getElementById("dcPagerInfo");
  const summaryNote = document.getElementById("dcSummaryNote");

  // Dialogs
  const newTemplateDialog = document.getElementById("newTemplateDialog");
  const cleanRulesDialog = document.getElementById("cleanRulesDialog");
  const addSheetDialog = document.getElementById("addSheetDialog");
  const ruleCatboostChk = document.getElementById("ruleCatboostModeChk");
  const ruleRedactPiiChk = document.getElementById("ruleRedactPiiChk");
  const ruleCustomRedactInput = document.getElementById("ruleCustomRedactInput");
  const exportRedactPiiChk = document.getElementById("exportRedactPiiChk");
  const exportCustomRedactInput = document.getElementById("exportCustomRedactInput");
  const exportRedactBadge = document.getElementById("exportRedactBadge");
  enableDialogLightDismissFallback(newTemplateDialog);
  enableDialogLightDismissFallback(cleanRulesDialog);
  enableDialogLightDismissFallback(addSheetDialog);

  function parseRedactTerms(raw) {
    if (Array.isArray(raw)) {
      return raw.map((x) => String(x || "").trim()).filter(Boolean);
    }
    return String(raw || "")
      .split(/[,，;；\r\n]+/)
      .map((x) => x.trim())
      .filter(Boolean);
  }

  function syncPrivacyUi(enabled, terms) {
    const isOn = Boolean(enabled);
    const termsArr = parseRedactTerms(terms);
    const termsStr = termsArr.join(", ");
    if (ruleRedactPiiChk) ruleRedactPiiChk.checked = isOn;
    if (ruleCustomRedactInput) ruleCustomRedactInput.value = termsStr;
    if (exportRedactPiiChk) exportRedactPiiChk.checked = isOn;
    if (exportCustomRedactInput) exportCustomRedactInput.value = termsStr;
    if (exportRedactBadge) {
      exportRedactBadge.textContent = isOn ? "已开启脱敏" : "未开启";
      exportRedactBadge.className = `pill ${isOn ? "pill-ok" : ""}`;
    }
  }

  function isPackTemplate(tpl = activeTemplate) {
    return Boolean(tpl && Array.isArray(tpl.sheets) && tpl.sheets.length > 0);
  }

  function syncCatboostHint(enabled) {
    if (exportCatboostHint) {
      exportCatboostHint.classList.toggle("hidden", !Boolean(enabled));
    }
  }

  if (ruleCatboostChk) {
    ruleCatboostChk.addEventListener("change", () => syncCatboostHint(ruleCatboostChk.checked));
  }

  async function savePrivacySettingFromExportTab() {
    const enabled = Boolean(exportRedactPiiChk && exportRedactPiiChk.checked);
    const terms = parseRedactTerms(exportCustomRedactInput ? exportCustomRedactInput.value : "");
    syncPrivacyUi(enabled, terms);
    if (!activeTemplate) return;
    try {
      const updated = await apiJson(`/api/excel-templates/${activeTemplate.id}`, {
        method: "PUT",
        body: { rules: { ...readFormRules(), redact_pii: enabled, custom_redact_terms: terms } },
      });
      activeTemplate.rules = updated.rules;
      const idx = templates.findIndex((t) => t.id === updated.id);
      if (idx >= 0) templates[idx] = updated;
      showToast(enabled ? "已开启导出前 PII 自动脱敏掩码" : "已关闭导出脱敏", "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  }

  if (exportRedactPiiChk) {
    exportRedactPiiChk.addEventListener("change", savePrivacySettingFromExportTab);
  }
  if (exportCustomRedactInput) {
    exportCustomRedactInput.addEventListener("change", savePrivacySettingFromExportTab);
  }

  function applyTemplateToForm(tpl) {
    if (!tpl) return;
    if (entrySheetInput) entrySheetInput.value = tpl.entry_sheet_name || "正文结构化条目";
    if (tableSheetInput) tableSheetInput.value = tpl.table_sheet_name || "提取表格明细";
    const r = tpl.rules || {};
    document.getElementById("ruleFullwidthChk").checked = r.normalize_fullwidth !== false;
    document.getElementById("ruleDatesChk").checked = r.normalize_dates !== false;
    document.getElementById("ruleHeadersChk").checked = r.remove_running_headers !== false;
    document.getElementById("ruleNoiseChk").checked = r.strip_noise_symbols !== false;
    document.getElementById("ruleDedupChk").checked = r.deduplicate_entries !== false;
    document.getElementById("ruleMinConfInput").value = String(r.min_confidence_warn ?? 0.82);
    if (ruleCatboostChk) ruleCatboostChk.checked = Boolean(r.catboost_mode);
    syncCatboostHint(Boolean(r.catboost_mode));
    syncPrivacyUi(Boolean(r.redact_pii), r.custom_redact_terms || []);

    editingColumns = (tpl.columns || []).map((c) => ({ ...c }));
    const isPack = isPackTemplate(tpl);
    if (packWorkbench) packWorkbench.classList.toggle("hidden", !isPack);
    if (legacyWorkbench) legacyWorkbench.classList.toggle("hidden", isPack);

    if (!isPack) {
      renderColumnEditor();
    } else {
      const sheetIds = (tpl.sheets || []).map((s) => s.sheet_id);
      if (
        !selectedDrawerId ||
        (!sheetIds.includes(selectedDrawerId) &&
          selectedDrawerId !== "__unassigned__" &&
          selectedDrawerId !== "__ignored__" &&
          selectedDrawerId !== "__catboost__")
      ) {
        selectedDrawerId = sheetIds[0] || "__catboost__";
      }
      schemaDraftSheet = null;
    }
    renderScenarioBar();
    renderExportPanel();
  }

  function renderTemplateSelectors(preferId = null) {
    tplSelect.replaceChildren();
    exportTplSelect.replaceChildren();
    for (const t of templates) {
      const packTag = Array.isArray(t.sheets) && t.sheets.length > 0 ? `[多表·${t.sheets.length}Sheet] ` : "";
      tplSelect.appendChild(el("option", { value: String(t.id), text: `${packTag}${t.name}` }));
      exportTplSelect.appendChild(el("option", { value: String(t.id), text: `${packTag}${t.name}` }));
    }
    if (preferId && templates.some((t) => t.id === preferId)) {
      activeTemplate = templates.find((t) => t.id === preferId);
    } else if (activeTemplate && templates.some((t) => t.id === activeTemplate.id)) {
      activeTemplate = templates.find((t) => t.id === activeTemplate.id);
    } else {
      // Prefer the multi-sheet visual pack on first visit so beginners see intuitive drawers
      activeTemplate = templates.find((t) => Array.isArray(t.sheets) && t.sheets.length > 0) || templates[0] || null;
    }
    if (activeTemplate) {
      tplSelect.value = String(activeTemplate.id);
      exportTplSelect.value = String(activeTemplate.id);
      applyTemplateToForm(activeTemplate);
    }
  }

  function readFormRules() {
    return {
      normalize_fullwidth: document.getElementById("ruleFullwidthChk").checked,
      normalize_dates: document.getElementById("ruleDatesChk").checked,
      remove_running_headers: document.getElementById("ruleHeadersChk").checked,
      strip_noise_symbols: document.getElementById("ruleNoiseChk").checked,
      deduplicate_entries: document.getElementById("ruleDedupChk").checked,
      min_confidence_warn: Number(document.getElementById("ruleMinConfInput").value || 0.82),
      catboost_mode: Boolean(ruleCatboostChk && ruleCatboostChk.checked),
      redact_pii: Boolean(ruleRedactPiiChk && ruleRedactPiiChk.checked),
      custom_redact_terms: parseRedactTerms(ruleCustomRedactInput ? ruleCustomRedactInput.value : ""),
    };
  }

  // ------------------------------------------------------------------
  // Export Tab 5 Dynamic Chips & Coverage Banner
  // ------------------------------------------------------------------
  function renderExportPanel() {
    if (!exportSheetTabStrip) return;
    exportSheetTabStrip.replaceChildren();
    if (!activeTemplate) return;

    const isPack = isPackTemplate(activeTemplate);
    const rules = activeTemplate.rules || {};
    if (!isPack) {
      exportSheetTabStrip.appendChild(
        el("span", { class: "sheet-tab-chip", text: `Sheet 1 · ${activeTemplate.entry_sheet_name || "正文结构化条目"}` })
      );
      exportSheetTabStrip.appendChild(
        el("span", { class: "sheet-tab-chip", text: `Sheet 2 · ${activeTemplate.table_sheet_name || "提取表格明细"}` })
      );
      exportSheetTabStrip.appendChild(el("span", { class: "sheet-tab-chip is-warn", text: "Sheet 3 · 异常待复核 (标黄)" }));
      exportSheetTabStrip.appendChild(el("span", { class: "sheet-tab-chip", text: "Sheet 4 · 清洗汇总报告" }));
      if (rules.catboost_mode) {
        exportSheetTabStrip.appendChild(el("span", { class: "sheet-tab-chip", text: "Sheet 5 · CatBoost_特征宽表" }));
        exportSheetTabStrip.appendChild(el("span", { class: "sheet-tab-chip", text: "Sheet 6 · CatBoost_字段契约" }));
      }
      if (exportCoverageSummary) {
        exportCoverageSummary.textContent = "当前为单表模板；如批次含 3~10+ 种单据/多个目标 y，建议切换「多表批次记录与多目标 CatBoost 模板包」或直接上传多 Sheet .xlsx。";
      }
      return;
    }

    const sheetsMeta = (matchData && matchData.template_id === activeTemplate.id ? matchData.sheets : activeTemplate.sheets) || [];
    sheetsMeta.forEach((s, idx) => {
      const pCount = Array.isArray(s.assigned_pages) ? s.assigned_pages.length : null;
      const yCount = (s.fields || []).filter((f) => f.role === "target_y").length;
      const txt = `${idx + 1}. ${s.name}${pCount !== null ? ` (${pCount}页)` : ""}${yCount > 0 ? ` ★${yCount}y` : ""}`;
      exportSheetTabStrip.appendChild(
        el("span", { class: `sheet-tab-chip${yCount > 0 ? " pill-ok" : ""}`, text: txt })
      );
    });
    exportSheetTabStrip.appendChild(el("span", { class: "sheet-tab-chip is-warn", text: "异常待复核 (标黄)" }));
    exportSheetTabStrip.appendChild(el("span", { class: "sheet-tab-chip", text: "清洗汇总报告" }));

    const cov = matchData && matchData.template_id === activeTemplate.id ? matchData.coverage : null;
    if (rules.catboost_mode) {
      const yCnt = cov ? (cov.target_y_names || []).length : 0;
      exportSheetTabStrip.appendChild(
        el("span", { class: "sheet-tab-chip pill-ok", text: `CatBoost_特征宽表${yCnt ? ` (${yCnt}个目标y)` : ""}` })
      );
      exportSheetTabStrip.appendChild(el("span", { class: "sheet-tab-chip", text: "CatBoost_字段契约" }));
    }

    if (exportCoverageSummary) {
      if (cov) {
        const yList = (cov.target_y_names || []).join("、") || "暂无";
        exportCoverageSummary.textContent =
          `当前工作集：已自动分拣 ${cov.matched_pages}/${cov.total_pages} 页（待确认 ${cov.unassigned_pages} 页）` +
          ` · 纸面字段对齐率 ${cov.alignment_pct}% · 覆盖 ${cov.total_batches} 个批次 · 识别目标 y：${yList}`;
      } else {
        exportCoverageSummary.textContent = `模板包已配置 ${sheetsMeta.length} 张子表；点击左侧「3 表格」或等待加载匹配预览。`;
      }
    }
  }

  // ------------------------------------------------------------------
  // Multi-Sheet Workbench Rendering
  // ------------------------------------------------------------------
  function getDrawerSheet() {
    if (!matchData || !selectedDrawerId || selectedDrawerId.startsWith("__")) return null;
    return (matchData.sheets || []).find((s) => s.sheet_id === selectedDrawerId) || null;
  }

  function renderSubTabsState() {
    document.querySelectorAll("[data-pack-subtab]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.packSubtab === activeSubTab);
    });
    subMappingPane.classList.toggle("hidden", activeSubTab !== "mapping");
    subPreviewPane.classList.toggle("hidden", activeSubTab !== "preview");
    subSchemaPane.classList.toggle("hidden", activeSubTab !== "schema");
  }

  function renderDrawerList() {
    packSheetList.replaceChildren();
    if (!matchData) {
      packSheetList.appendChild(el("div", { class: "muted-note", text: "正在加载模板抽屉..." }));
      return;
    }

    const sheets = matchData.sheets || [];
    for (const s of sheets) {
      const isAct = selectedDrawerId === s.sheet_id;
      const pageCount = (s.assigned_pages || []).length;
      const yCount = (s.fields || []).filter((f) => f.role === "target_y").length;
      const unalignedCnt = (s.column_mappings || []).filter((m) => m.status === "unmatched").length;

      const badges = [el("span", { class: "badge-mode", text: MODE_LABELS[s.mode] || s.mode })];
      if (yCount > 0) badges.push(el("span", { class: "badge-y", text: `★ ${yCount} 目标 y` }));
      if (unalignedCnt > 0) badges.push(el("span", { class: "pill pill-warn", style: "padding:1px 6px;font-size:11px;", text: `${unalignedCnt} 待对齐` }));

      const btn = el(
        "button",
        {
          type: "button",
          class: `pack-sheet-item${isAct ? " is-active" : ""}`,
          onclick: () => {
            selectedDrawerId = s.sheet_id;
            schemaDraftSheet = null;
            renderWorkbench();
          },
        },
        [
          el("div", { class: "pack-sheet-row1" }, [
            el("span", { class: "pack-sheet-title", text: s.name }),
            el("span", { class: "pack-sheet-count", text: `${pageCount} 页` }),
          ]),
          el("div", { class: "pack-sheet-badges" }, badges),
        ]
      );
      packSheetList.appendChild(btn);
    }

    packSheetList.appendChild(el("div", { class: "pack-drawer-sep" }));

    const unCnt = (matchData.unassigned_pages || []).length;
    packSheetList.appendChild(
      el(
        "button",
        {
          type: "button",
          class: `pack-sheet-item is-warn-drawer${selectedDrawerId === "__unassigned__" ? " is-active" : ""}`,
          onclick: () => {
            selectedDrawerId = "__unassigned__";
            schemaDraftSheet = null;
            renderWorkbench();
          },
        },
        [
          el("div", { class: "pack-sheet-row1" }, [
            el("span", { class: "pack-sheet-title", text: "待确认归类" }),
            el("span", {
              class: `pack-sheet-count${unCnt > 0 ? " pill-warn" : ""}`,
              text: `${unCnt} 页`,
            }),
          ]),
          el("div", { class: "muted-note", style: "font-size:11.5px;", text: unCnt > 0 ? "手动指派进某个子表或暂不导出" : "全部页面已找到归属" }),
        ]
      )
    );

    const igCnt = (matchData.ignored_pages || []).length;
    packSheetList.appendChild(
      el(
        "button",
        {
          type: "button",
          class: `pack-sheet-item${selectedDrawerId === "__ignored__" ? " is-active" : ""}`,
          onclick: () => {
            selectedDrawerId = "__ignored__";
            schemaDraftSheet = null;
            renderWorkbench();
          },
        },
        [
          el("div", { class: "pack-sheet-row1" }, [
            el("span", { class: "pack-sheet-title", text: "暂不导出" }),
            el("span", { class: "pack-sheet-count", text: `${igCnt} 页` }),
          ]),
        ]
      )
    );

    const cb = matchData.catboost_dataset || {};
    const cbBatches = (cb.rows || []).length;
    const cbYs = (cb.target_y_cols || []).length;
    packSheetList.appendChild(
      el(
        "button",
        {
          type: "button",
          class: `pack-sheet-item${selectedDrawerId === "__catboost__" ? " is-active" : ""}`,
          onclick: () => {
            selectedDrawerId = "__catboost__";
            activeSubTab = "preview";
            schemaDraftSheet = null;
            renderWorkbench();
          },
        },
        [
          el("div", { class: "pack-sheet-row1" }, [
            el("span", { class: "pack-sheet-title", text: "CatBoost 宽表预览" }),
            el("span", { class: "pack-sheet-count", text: `${cbBatches} 批` }),
          ]),
          el("div", { class: "pack-sheet-badges" }, [
            el("span", { class: "badge-y", text: `含 ${cbYs} 个目标 y` }),
            el("span", { class: "badge-mode", text: "1批次=1行" }),
          ]),
        ]
      )
    );
  }

  async function assignPageToDrawer(pageId, targetSheetId) {
    if (!state.currentBookId || !activeTemplate) return;
    try {
      const updated = await apiJson(`/api/books/${state.currentBookId}/page-assignments`, {
        method: "PUT",
        body: {
          template_id: activeTemplate.id,
          page_id: pageId,
          sheet_id: targetSheetId,
        },
      });
      matchData = updated;
      renderWorkbench();
      renderExportPanel();
      showToast("已更新页面归属子表并重新提取字段", "ok");
    } catch (err) {
      showToast(err.message, "danger");
    }
  }

  function renderPageList() {
    packPageList.replaceChildren();
    if (!matchData) return;

    const sheets = matchData.sheets || [];
    let pagesToRender = [];
    let heading = "② 归属本表的扫描页";

    if (selectedDrawerId === "__unassigned__") {
      pagesToRender = matchData.unassigned_pages || [];
      heading = "② 待确认归类页面";
    } else if (selectedDrawerId === "__ignored__") {
      pagesToRender = matchData.ignored_pages || [];
      heading = "② 暂不导出页面";
    } else if (selectedDrawerId === "__catboost__") {
      heading = "② CatBoost 建模总览";
      packPagesHeading.textContent = heading;
      const cb = matchData.catboost_dataset || {};
      packPagesCount.textContent = `${(cb.rows || []).length} 批次`;
      packPageList.appendChild(
        el("div", { class: "pack-page-card" }, [
          el("strong", { style: "font-size:13px;", text: "所有模板子表已汇总成 1 批次 = 1 行" }),
          el("div", {
            class: "muted-note",
            text: `右侧「写入数据预览」展示最终导出给 CatBoost 的宽表和字段契约；识别到的预测目标列：${(cb.target_y_cols || []).join("、") || "请在子表字段中将结果列标为 ★ 预测目标 y"}。`,
          }),
        ])
      );
      return;
    } else {
      const activeSheet = getDrawerSheet();
      pagesToRender = activeSheet ? activeSheet.assigned_pages || [] : [];
      heading = activeSheet ? `② 「${activeSheet.name}」扫描页` : "② 扫描页";
    }

    packPagesHeading.textContent = heading;
    packPagesCount.textContent = `${pagesToRender.length} 页`;

    if (!pagesToRender.length) {
      packPageList.appendChild(
        el("div", {
          class: "pack-empty-state",
          text:
            selectedDrawerId === "__unassigned__"
              ? "太棒了，当前工作集所有页面都已自动归类到对应子表！"
              : "当前抽屉暂无匹配页面；可从其他抽屉或「待确认归类」把页面移入这里。",
        })
      );
      return;
    }

    for (const p of pagesToRender) {
      const sel = el("select", { "aria-label": `调整第 ${p.seq_no} 页归属子表` });
      sel.appendChild(el("option", { value: "__auto__", text: "恢复自动认亲匹配" }));
      for (const s of sheets) {
        sel.appendChild(el("option", { value: s.sheet_id, text: `移入 → ${s.name}` }));
      }
      sel.appendChild(el("option", { value: "__ignore__", text: "暂不导出（移入忽略区）" }));

      if (selectedDrawerId === "__ignored__") {
        sel.value = "__ignore__";
      } else if (selectedDrawerId && !selectedDrawerId.startsWith("__") && p.match_source !== "auto") {
        sel.value = selectedDrawerId;
      } else if (selectedDrawerId && !selectedDrawerId.startsWith("__")) {
        sel.value = selectedDrawerId;
      } else {
        sel.value = "__auto__";
      }
      sel.addEventListener("change", () => {
        assignPageToDrawer(p.page_id, sel.value);
      });

      const matchBadgeText =
        p.match_source === "llm"
          ? "LLM 已填表"
          : p.match_source === "manual"
            ? "手动指定"
            : p.suggested_sheet_name && selectedDrawerId === "__unassigned__"
              ? `猜想: ${p.suggested_sheet_name}`
              : "自动认亲";

      const metaBadges = [
        el("span", { class: "badge-mode", text: matchBadgeText }),
      ];
      if (p.batch_id) {
        metaBadges.push(el("span", { class: "badge-mode mono", text: p.batch_id }));
      }
      metaBadges.push(
        el("span", { class: "muted-note", text: `${((p.ocr_confidence || 0) * 100).toFixed(0)}%` })
      );

      const controls = [sel];
      if (selectedDrawerId && !selectedDrawerId.startsWith("__")) {
        const aiBtn = el("button", {
          type: "button",
          class: "btn btn-compact",
          title: "调用设置里的大模型按当前子表字段抽取本页内容",
          text: "AI 填表",
        });
        aiBtn.addEventListener("click", async () => {
          await withButtonLoading(aiBtn, "抽取中...", async () => {
            try {
              const res = await apiJson(
                `/api/books/${state.currentBookId}/pages/${p.page_id}/llm-extract-sheet`,
                {
                  method: "POST",
                  body: { template_id: activeTemplate.id, sheet_id: selectedDrawerId },
                }
              );
              await refreshMatchPreview();
              showToast(`AI 已抽取 ${res.count} 行数据写入当前子表`, "ok");
            } catch (err) {
              showToast(err.message, "warn");
            }
          });
        });
        controls.push(aiBtn);
      }

      packPageList.appendChild(
        el("div", { class: "pack-page-card" }, [
          el("div", { class: "pack-page-top" }, [
            el("img", {
              class: "pack-page-thumb",
              src: p.thumb_url,
              alt: `第 ${p.seq_no} 页`,
              loading: "lazy",
            }),
            el("div", { class: "pack-page-info" }, [
              el("div", { class: "pack-page-title", title: p.title, text: `#${p.seq_no} ${p.title}` }),
              el("div", { class: "pack-page-sub" }, metaBadges),
            ]),
          ]),
          el("div", { class: "pack-page-controls" }, controls),
        ])
      );
    }
  }

  function renderMappingSubTab() {
    subMappingPane.replaceChildren();
    if (!matchData) return;

    const sheet = getDrawerSheet();
    if (!sheet) {
      subMappingPane.appendChild(
        el("div", {
          class: "pack-empty-state",
          text:
            selectedDrawerId === "__unassigned__"
              ? "在中间栏选择任意页面并切换「移入 → 对应子表」，随后点击左栏子表即可检查字段连线。"
              : "请在左栏点选一个具体的模板 Sheet 抽屉，查看纸面列名与模板列的自动对齐状态。",
        })
      );
      return;
    }

    const mappings = sheet.column_mappings || [];
    const fields = sheet.fields || [];
    const matchedCnt = mappings.filter((m) => m.status !== "unmatched").length;
    packDetailMeta.textContent = `子表「${sheet.name}」 · 已识别 ${mappings.length} 个纸面列，已对齐 ${matchedCnt} 列`;

    subMappingPane.appendChild(
      el("div", {
        class: "muted-note",
        text: "规则提示：左边是纸上扫出来的表头/参数名，右边是目标 Excel 的列名。如有黄标未对齐项，选一下目标列即自动记住别名，下一批直接绿灯对齐。",
      })
    );

    if (!mappings.length) {
      subMappingPane.appendChild(
        el("div", {
          class: "pack-empty-state",
          text: "该子表暂未收到扫描页，或页面中没有结构化列名（例如纯 SOP 说明文本页，可直接点上方「写入数据预览」查看正文提取）。",
        })
      );
      return;
    }

    const tbody = el("tbody");
    for (const m of mappings) {
      const sel = el("select", { class: "input", style: "padding:5px 8px;font-size:12.5px;" });
      sel.appendChild(el("option", { value: "", text: "-- 请选择要写入的目标列 --" }));
      for (const f of fields) {
        const ySuffix = f.role === "target_y" ? " [★目标 y]" : "";
        sel.appendChild(el("option", { value: f.key, text: `${f.name}${ySuffix}` }));
      }
      sel.appendChild(el("option", { value: "__new_field__", text: `+ 新增模板列「${m.source_col}」` }));
      sel.value = m.target_field_key || "";

      sel.addEventListener("change", async () => {
        const chosen = sel.value;
        if (!chosen || !activeTemplate) return;
        try {
          if (chosen === "__new_field__") {
            const newSheets = (activeTemplate.sheets || []).map((s) => {
              if (s.sheet_id !== sheet.sheet_id) return s;
              const nextKey = `f_${Date.now().toString(36).slice(-5)}`;
              const isY = m.source_col.toLowerCase().startsWith("y_") || /效价|纯度|收率|得率/.test(m.source_col);
              return {
                ...s,
                fields: [
                  ...(s.fields || []),
                  {
                    key: nextKey,
                    name: m.source_col,
                    role: isY ? "target_y" : "num_feature",
                    agg: s.mode === "table" && !isY ? "stats" : "last",
                    aliases: [],
                  },
                ],
              };
            });
            await apiJson(`/api/excel-templates/${activeTemplate.id}`, {
              method: "PUT",
              body: { sheets: newSheets },
            });
            await loadTemplates(activeTemplate.id);
            await refreshMatchPreview();
            showToast(`已将「${m.source_col}」加为子表新列`, "ok");
            return;
          }

          await apiJson(`/api/excel-templates/${activeTemplate.id}/learn-alias`, {
            method: "POST",
            body: {
              sheet_id: sheet.sheet_id,
              field_key: chosen,
              source_alias: m.source_col,
            },
          });
          await loadTemplates(activeTemplate.id);
          await refreshMatchPreview();
          showToast(`已记住别名「${m.source_col}」，后续同批次自动对齐`, "ok");
        } catch (err) {
          showToast(err.message, "danger");
        }
      });

      const stMeta = ALIGN_STATUS_META[m.status] || ALIGN_STATUS_META.unmatched;
      const sampleTxt = (m.sample_values || []).length ? `示例：${m.sample_values.join(" / ")}` : `出现 ${m.count} 页`;
      tbody.appendChild(
        el("tr", {}, [
          el("td", {}, [
            el("div", { style: "font-weight:600;", text: m.source_col }),
            el("div", { class: "muted-note", style: "font-size:11.5px;", text: sampleTxt }),
          ]),
          el("td", { class: "pack-map-arrow", text: "→" }),
          el("td", {}, [sel]),
          el("td", {}, [el("span", { class: stMeta.cls, text: stMeta.label })]),
        ])
      );
    }

    subMappingPane.appendChild(
      el("div", { class: "pack-map-wrap" }, [
        el("table", { class: "pack-map-table" }, [
          el("thead", {}, [
            el("tr", {}, [
              el("th", { text: "纸面识别列名" }),
              el("th", { style: "width:34px;", text: "" }),
              el("th", { text: "映射到 Excel 目标列（选择即记住别名）" }),
              el("th", { style: "width:128px;", text: "匹配状态" }),
            ]),
          ]),
          tbody,
        ]),
      ])
    );
  }

  function renderPreviewSubTab() {
    subPreviewPane.replaceChildren();
    if (!matchData) return;

    if (selectedDrawerId === "__catboost__") {
      const cb = matchData.catboost_dataset || {};
      const headers = cb.headers || [];
      const rows = cb.rows || [];
      const ySet = new Set(cb.target_y_cols || []);
      packDetailMeta.textContent = `CatBoost_特征宽表 · ${cb.total_row_count || rows.length} 个批次 · ${ySet.size} 个目标 y`;

      if (!rows.length) {
        subPreviewPane.appendChild(el("div", { class: "pack-empty-state", text: "暂无可用于组装 CatBoost 宽表的批次数据。" }));
        return;
      }

      const ths = headers.map((h) =>
        el("th", {
          class: ySet.has(h) ? "pack-target-y-th" : "",
          text: ySet.has(h) ? `★ ${h}` : h,
        })
      );
      const tbody = el("tbody");
      for (const r of rows) {
        const tds = headers.map((h) => {
          const v = r[h];
          const isNull = v === null || v === undefined || v === "";
          if (h === "review_required" && Number(v) === 1) {
            return el(
              "td",
              {
                class: "cell-alert-danger",
                title: "该批次存在数值越界、突变或原页低置信告警，已隔离禁止回流训练集",
              },
              [el("span", { class: "cell-alert-badge", text: "1 · 拦截复核" })]
            );
          }
          return el("td", {
            class: `cell-trunc${isNull ? " muted-note" : ""}`,
            title: isNull ? "缺失保持空值 NaN（供 CatBoost 原生处理）" : String(v),
            text: isNull ? "NaN" : String(v),
          });
        });
        tbody.appendChild(el("tr", {}, tds));
      }

      subPreviewPane.appendChild(
        el("div", {
          class: "muted-note",
          text: "绿色表头为预测目标列 (y)，训练时请勿作为输入特征 X；缺失指标保持真实空值/NaN，不填 0。",
        })
      );
      subPreviewPane.appendChild(
        el("div", { class: "dataclean-table-wrap scroller" }, [
          el("table", { class: "data-table" }, [el("thead", {}, [el("tr", {}, ths)]), tbody]),
        ])
      );
      return;
    }

    const sheet = getDrawerSheet();
    if (!sheet) {
      subPreviewPane.appendChild(
        el("div", { class: "pack-empty-state", text: "请先在左栏点选具体子表或「CatBoost 宽表预览」。" })
      );
      return;
    }

    const fields = sheet.fields || [];
    const rows = sheet.rows || [];
    packDetailMeta.textContent = `子表「${sheet.name}」 · 将写入 ${sheet.total_row_count ?? rows.length} 行数据`;

    if (!rows.length) {
      subPreviewPane.appendChild(el("div", { class: "pack-empty-state", text: "该子表当前没有归属页面或可写入的数据行。" }));
      return;
    }

    const ths = fields.map((f) =>
      el("th", {
        class: f.role === "target_y" ? "pack-target-y-th" : "",
        text: f.role === "target_y" ? `★ ${f.name}` : f.name,
      })
    );
    ths.push(el("th", { text: "来源页" }), el("th", { text: "抽取方式" }));

    const tbody = el("tbody");
    for (const r of rows) {
      const cellAlerts = r._cell_alerts || {};
      const tds = fields.map((f) => {
        const v = r[f.key];
        const isNull = v === null || v === undefined || v === "";
        const alertMsg = cellAlerts[f.key];
        if (alertMsg) {
          const badgeTxt = alertMsg.includes("越界") ? "越界" : alertMsg.includes("离群") ? "突变" : "告警";
          return el(
            "td",
            {
              class: "cell-alert-danger",
              title: alertMsg,
            },
            [
              el("span", { text: isNull ? "空值" : String(v) }),
              el("span", { class: "cell-alert-badge", text: badgeTxt }),
            ]
          );
        }
        return el("td", {
          class: `cell-trunc${isNull ? " muted-note" : ""}`,
          title: isNull ? "空" : String(v),
          text: isNull ? "-" : String(v),
        });
      });
      const warnTexts = r._warnings || [];
      tds.push(
        el("td", { text: `#${r._page_no || 0}` }),
        el("td", {
          title: warnTexts.join("；") || "",
          text: r._source === "llm" ? "AI填表" : "离线匹配",
        })
      );
      tbody.appendChild(el("tr", { class: warnTexts.length ? "row-warn" : "" }, tds));
    }

    subPreviewPane.appendChild(
      el("div", { class: "dataclean-table-wrap scroller" }, [
        el("table", { class: "data-table" }, [el("thead", {}, [el("tr", {}, ths)]), tbody]),
      ])
    );
  }

  function renderSchemaSubTab() {
    subSchemaPane.replaceChildren();
    if (!activeTemplate || !isPackTemplate(activeTemplate)) return;

    const liveSheet = getDrawerSheet();
    if (!liveSheet) {
      subSchemaPane.appendChild(
        el("div", { class: "pack-empty-state", text: "请在左栏选择一个子表抽屉，即可编辑该子表的列名、别名与「★ 预测目标 y」标记。" })
      );
      return;
    }

    if (!schemaDraftSheet || schemaDraftSheet.sheet_id !== liveSheet.sheet_id) {
      schemaDraftSheet = {
        sheet_id: liveSheet.sheet_id,
        name: liveSheet.name || "",
        mode: liveSheet.mode || "record",
        keywordsStr: (liveSheet.match_keywords || []).join(", "),
        fields: (liveSheet.fields || []).map((f) => ({
          ...f,
          min_val: f.min_val ?? null,
          max_val: f.max_val ?? null,
          require_not_null: Boolean(f.require_not_null),
          aliasesStr: (f.aliases || []).join(", "),
        })),
      };
    }

    packDetailMeta.textContent = `编辑子表「${schemaDraftSheet.name}」 · 共 ${schemaDraftSheet.fields.length} 列`;

    const nameInp = el("input", { class: "input", type: "text", value: schemaDraftSheet.name });
    nameInp.addEventListener("input", () => {
      schemaDraftSheet.name = nameInp.value;
    });

    const modeSel = el("select", { class: "input" }, [
      el("option", { value: "record", text: "单条记录表 (1页=1行)" }),
      el("option", { value: "table", text: "多行明细表 (1页=多行)" }),
      el("option", { value: "text", text: "SOP / 文本批注" }),
    ]);
    modeSel.value = schemaDraftSheet.mode;
    modeSel.addEventListener("change", () => {
      schemaDraftSheet.mode = modeSel.value;
    });

    const kwInp = el("input", {
      class: "input",
      type: "text",
      value: schemaDraftSheet.keywordsStr,
      placeholder: "逗号隔开，如：化验, 样品, 纯度",
    });
    kwInp.addEventListener("input", () => {
      schemaDraftSheet.keywordsStr = kwInp.value;
    });

    function parseOptionalBound(rawVal) {
      const s = String(rawVal ?? "").trim();
      if (!s) return null;
      const n = Number(s);
      return Number.isFinite(n) ? n : null;
    }

    const fieldListBox = el("div", { class: "pack-field-list" });
    function redrawFields() {
      fieldListBox.replaceChildren();
      schemaDraftSheet.fields.forEach((f, idx) => {
        const fnInp = el("input", { class: "input", type: "text", value: f.name || "", placeholder: "Excel 列名" });
        fnInp.addEventListener("input", () => {
          f.name = fnInp.value;
        });

        const roleSel = el("select", { class: "input" });
        for (const opt of ROLE_OPTIONS) {
          roleSel.appendChild(el("option", { value: opt.value, text: opt.label }));
        }
        roleSel.value = ROLE_OPTIONS.some((o) => o.value === f.role) ? f.role : "num_feature";
        roleSel.addEventListener("change", () => {
          f.role = roleSel.value;
          redrawFields();
        });

        const isNumericRole = f.role === "num_feature" || f.role === "target_y";
        let minControl;
        let maxControl;
        if (isNumericRole) {
          minControl = el("input", {
            class: "input pack-bound-inp",
            type: "number",
            step: "any",
            value: f.min_val !== null && f.min_val !== undefined ? String(f.min_val) : "",
            placeholder: "不限",
            "aria-label": `${f.name || "字段"} 最小值`,
          });
          minControl.addEventListener("input", () => {
            f.min_val = parseOptionalBound(minControl.value);
          });

          maxControl = el("input", {
            class: "input pack-bound-inp",
            type: "number",
            step: "any",
            value: f.max_val !== null && f.max_val !== undefined ? String(f.max_val) : "",
            placeholder: "不限",
            "aria-label": `${f.name || "字段"} 最大值`,
          });
          maxControl.addEventListener("input", () => {
            f.max_val = parseOptionalBound(maxControl.value);
          });
        } else {
          minControl = el("span", { class: "pack-bound-na muted-note", text: "—" });
          maxControl = el("span", { class: "pack-bound-na muted-note", text: "—" });
        }

        const aliasInp = el("input", {
          class: "input",
          type: "text",
          value: f.aliasesStr || "",
          placeholder: "纸面常见别名（逗号隔开）",
        });
        aliasInp.addEventListener("input", () => {
          f.aliasesStr = aliasInp.value;
        });

        const delFieldBtn = el("button", {
          type: "button",
          class: "btn btn-sm btn-danger-ghost",
          text: "删",
          onclick: () => {
            schemaDraftSheet.fields.splice(idx, 1);
            redrawFields();
          },
        });

        fieldListBox.appendChild(
          el("div", { class: "pack-field-row" }, [fnInp, roleSel, minControl, maxControl, aliasInp, delFieldBtn])
        );
      });
    }
    redrawFields();

    const addFieldBtn = el("button", {
      type: "button",
      class: "btn btn-sm",
      text: "+ 新增字段列",
      onclick: () => {
        schemaDraftSheet.fields.push({
          key: `f_${Date.now().toString(36).slice(-5)}`,
          name: `新字段${schemaDraftSheet.fields.length + 1}`,
          role: "num_feature",
          agg: schemaDraftSheet.mode === "table" ? "stats" : "last",
          min_val: null,
          max_val: null,
          require_not_null: false,
          aliasesStr: "",
        });
        redrawFields();
      },
    });

    const delSheetBtn = el("button", {
      type: "button",
      class: "btn btn-sm btn-danger-ghost",
      text: "删除此子表",
    });
    wireTwoStepAction(delSheetBtn, "确认删除子表？", async () => {
      if ((activeTemplate.sheets || []).length <= 1) {
        showToast("多表模板至少保留 1 个子表", "warn");
        return;
      }
      const nextSheets = (activeTemplate.sheets || []).filter((s) => s.sheet_id !== schemaDraftSheet.sheet_id);
      await apiJson(`/api/excel-templates/${activeTemplate.id}`, {
        method: "PUT",
        body: { sheets: nextSheets },
      });
      selectedDrawerId = nextSheets[0] ? nextSheets[0].sheet_id : "__unassigned__";
      schemaDraftSheet = null;
      await loadTemplates(activeTemplate.id);
      await refreshMatchPreview();
      showToast("已移除子表并刷新分拣结果", "ok");
    });

    const saveSheetBtn = el("button", {
      type: "button",
      class: "btn btn-primary btn-sm",
      text: "保存子表设置并重新匹配",
    });
    saveSheetBtn.addEventListener("click", async () => {
      await withButtonLoading(saveSheetBtn, "保存中...", async () => {
        try {
          const cleanName = schemaDraftSheet.name.trim() || "未命名子表";
          const kws = schemaDraftSheet.keywordsStr
            .split(/[,，、\s]+/)
            .map((x) => x.trim())
            .filter(Boolean);
          const cleanFields = schemaDraftSheet.fields
            .filter((f) => (f.name || "").trim())
            .map((f, i) => {
              const role = f.role || "num_feature";
              const isNum = role === "num_feature" || role === "target_y";
              return {
                key: f.key || `f_${i + 1}`,
                name: f.name.trim(),
                role,
                agg: role === "num_feature" && schemaDraftSheet.mode === "table" ? "stats" : role === "text_feature" ? "join" : "last",
                min_val: isNum ? f.min_val ?? null : null,
                max_val: isNum ? f.max_val ?? null : null,
                require_not_null: Boolean(f.require_not_null),
                aliases: (f.aliasesStr || "")
                  .split(/[,，]+/)
                  .map((a) => a.trim())
                  .filter(Boolean),
              };
            });
          const updatedSheets = (activeTemplate.sheets || []).map((s) =>
            s.sheet_id === schemaDraftSheet.sheet_id
              ? {
                  ...s,
                  name: cleanName,
                  mode: schemaDraftSheet.mode,
                  match_keywords: kws,
                  fields: cleanFields,
                }
              : s
          );
          await apiJson(`/api/excel-templates/${activeTemplate.id}`, {
            method: "PUT",
            body: { sheets: updatedSheets },
          });
          schemaDraftSheet = null;
          await loadTemplates(activeTemplate.id);
          await refreshMatchPreview();
          showToast(`子表「${cleanName}」设置已保存并生效`, "ok");
        } catch (err) {
          showToast(err.message, "danger");
        }
      });
    });

    subSchemaPane.appendChild(
      el("div", { class: "pack-schema-card" }, [
        el("div", { class: "pack-schema-meta-grid" }, [
          el("div", { class: "field-row", style: "margin:0;" }, [el("label", { text: "子表名称" }), nameInp]),
          el("div", { class: "field-row", style: "margin:0;" }, [el("label", { text: "数据形态" }), modeSel]),
          el("div", { class: "field-row", style: "margin:0;" }, [el("label", { text: "认亲关键词（自动分拣依据）" }), kwInp]),
        ]),
        el("div", { class: "toolbar-between" }, [
          el("strong", { class: "muted-note", text: "目标 Excel 列配置（如需多个目标 y，把对应列标为「★ 预测目标 y」）" }),
          addFieldBtn,
        ]),
        el("div", { class: "pack-field-head", "aria-hidden": "true" }, [
          el("span", { text: "Excel 列名" }),
          el("span", { text: "字段角色 (X / y)" }),
          el("span", { text: "最小值" }),
          el("span", { text: "最大值" }),
          el("span", { text: "已记住别名词典" }),
          el("span", { text: "" }),
        ]),
        fieldListBox,
        el("div", { class: "toolbar-between", style: "padding-top:6px;border-top:1px solid var(--border);" }, [
          delSheetBtn,
          saveSheetBtn,
        ]),
      ])
    );
  }

  function renderWorkbench() {
    if (!isPackTemplate()) return;
    if (coverageBadge && matchData && matchData.coverage) {
      const c = matchData.coverage;
      const warn = c.unassigned_pages > 0;
      coverageBadge.className = `pill ${warn ? "pill-warn" : "pill-ok"}`;
      coverageBadge.textContent = `已归类 ${c.matched_pages}/${c.total_pages} 页 · 对齐率 ${c.alignment_pct}%`;
    }
    renderDrawerList();
    renderPageList();
    renderSubTabsState();
    if (activeSubTab === "mapping") renderMappingSubTab();
    else if (activeSubTab === "preview") renderPreviewSubTab();
    else renderSchemaSubTab();
  }

  async function refreshMatchPreview() {
    if (!state.currentBookId || !activeTemplate) return;
    try {
      const qs = new URLSearchParams({ template_id: String(activeTemplate.id) });
      matchData = await apiJson(`/api/books/${state.currentBookId}/match-preview?${qs.toString()}`);
      if (isPackTemplate()) {
        renderWorkbench();
      }
      renderExportPanel();
    } catch (err) {
      showToast(err.message, "warn");
    }
  }

  // ------------------------------------------------------------------
  // Legacy Single-Sheet Fallback Editor
  // ------------------------------------------------------------------
  function renderColumnEditor() {
    if (!schemaColList) return;
    schemaColList.replaceChildren();
    editingColumns.forEach((col, idx) => {
      const headerInp = el("input", {
        class: "input",
        type: "text",
        value: col.header || "",
        placeholder: "Excel 列名",
      });
      headerInp.addEventListener("input", () => {
        col.header = headerInp.value;
        if (!col.key) col.key = `col_${idx + 1}`;
      });

      const typeSel = el("select", { class: "input" }, [
        el("option", { value: "builtin", text: "内置字段" }),
        el("option", { value: "regex", text: "正则提取" }),
        el("option", { value: "kv", text: "键值对(键:值)" }),
      ]);
      typeSel.value = col.source_type || "builtin";

      const patternHolder = el("div");
      function renderPatternControl() {
        patternHolder.replaceChildren();
        if (typeSel.value === "builtin") {
          const sel = el("select", { class: "input" });
          for (const opt of BUILTIN_FIELD_OPTIONS) {
            sel.appendChild(el("option", { value: opt.value, text: opt.label }));
          }
          sel.value = BUILTIN_FIELD_OPTIONS.some((o) => o.value === col.pattern)
            ? col.pattern
            : "clean_content";
          col.pattern = sel.value;
          sel.addEventListener("change", () => {
            col.pattern = sel.value;
          });
          patternHolder.appendChild(sel);
        } else {
          const inp = el("input", {
            class: "input",
            type: "text",
            value: col.pattern || "",
            placeholder: typeSel.value === "regex" ? "正则表达式" : "键名(*=全部键值)",
          });
          inp.addEventListener("input", () => {
            col.pattern = inp.value;
          });
          patternHolder.appendChild(inp);
        }
      }
      renderPatternControl();
      typeSel.addEventListener("change", () => {
        col.source_type = typeSel.value;
        col.pattern = typeSel.value === "builtin" ? "clean_content" : typeSel.value === "kv" ? "*" : "";
        renderPatternControl();
      });

      const reqChk = el("input", { type: "checkbox", title: "必填校验" });
      reqChk.checked = Boolean(col.required);
      reqChk.addEventListener("change", () => {
        col.required = reqChk.checked;
      });
      const reqWrap = el("label", { style: "display:flex;align-items:center;gap:4px;font-size:12px;" }, [
        reqChk,
        el("span", { text: "必填" }),
      ]);

      const delBtn = el("button", {
        type: "button",
        class: "btn btn-sm btn-danger",
        text: "删",
        onclick: () => {
          editingColumns.splice(idx, 1);
          renderColumnEditor();
        },
      });

      schemaColList.appendChild(
        el("div", { class: "schema-row" }, [headerInp, typeSel, patternHolder, reqWrap, delBtn])
      );
    });
  }

  async function saveCurrentTemplate() {
    if (!activeTemplate) return null;
    const sanitizedCols = editingColumns
      .filter((c) => (c.header || "").trim())
      .map((c, idx) => ({
        key: c.key || `col_${idx + 1}`,
        header: c.header.trim(),
        source_type: c.source_type || "builtin",
        pattern: c.pattern || "",
        required: Boolean(c.required),
      }));

    const updated = await apiJson(`/api/excel-templates/${activeTemplate.id}`, {
      method: "PUT",
      body: {
        entry_sheet_name: entrySheetInput ? entrySheetInput.value.trim() || "正文结构化条目" : "正文结构化条目",
        table_sheet_name: tableSheetInput ? tableSheetInput.value.trim() || "提取表格明细" : "提取表格明细",
        rules: readFormRules(),
        columns: sanitizedCols,
      },
    });
    await loadTemplates(updated.id);
    return updated;
  }

  function renderSummary(s) {
    if (!s) return;
    document.getElementById("dcKpiEntries").textContent = String(s.entries || 0);
    document.getElementById("dcKpiTables").textContent = String(s.distinct_tables || 0);
    document.getElementById("dcKpiTableRows").textContent = String(s.table_rows || 0);
    document.getElementById("dcKpiWarnings").textContent = String(s.warning_rows || 0);
    if (callbacks.onCleaned) callbacks.onCleaned(s);
  }

  async function refreshPreview() {
    if (!state.currentBookId) return;
    if (isPackTemplate()) {
      await refreshMatchPreview();
      return;
    }
    const qs = new URLSearchParams({
      kind: previewKind,
      limit: String(previewLimit),
      offset: String(previewOffset),
    });
    const res = await apiJson(`/api/books/${state.currentBookId}/structured-rows?${qs.toString()}`);
    renderSummary(res.summary);
    const items = res.items || [];
    previewTotal = res.total || 0;
    const startIdx = previewTotal === 0 ? 0 : previewOffset + 1;
    const endIdx = Math.min(previewTotal, previewOffset + items.length);
    pagerInfo.textContent = `${startIdx}-${endIdx} / ${previewTotal}`;

    previewHead.replaceChildren();
    previewBody.replaceChildren();

    if (!items.length) {
      previewBody.appendChild(
        el("tr", {}, [
          el("td", {
            colspan: "6",
            class: "empty-state",
            text: "暂无结构化清洗结果，请点击左侧「运行结构化清洗与拆表」",
          }),
        ])
      );
      return;
    }

    if (previewKind === "TABLE") {
      const maxCols = Math.max(...items.map((r) => ((r.data && r.data.cells) || []).length), 3);
      const hCells = [el("th", { text: "页码" }), el("th", { text: "表#" }), el("th", { text: "行类型" })];
      for (let i = 1; i <= maxCols; i += 1) hCells.push(el("th", { text: `列${i}` }));
      hCells.push(el("th", { text: "质检备注" }));
      previewHead.appendChild(el("tr", {}, hCells));

      for (const r of items) {
        const warns = r.warnings || [];
        const d = r.data || {};
        const cells = d.cells || [];
        const tds = [
          el("td", { text: String(r.page_no) }),
          el("td", { text: String(r.table_index) }),
          el("td", { text: d.is_header ? "表头" : "数据" }),
        ];
        for (let i = 0; i < maxCols; i += 1) {
          tds.push(el("td", { class: "cell-trunc", text: String(cells[i] || ""), title: String(cells[i] || "") }));
        }
        tds.push(el("td", { text: warns.join("; ") || "正常" }));
        previewBody.appendChild(el("tr", { class: warns.length ? "row-warn" : "" }, tds));
      }
      return;
    }

    if (previewKind === "WARNINGS") {
      previewHead.appendChild(
        el("tr", {}, [
          el("th", { text: "类型" }),
          el("th", { text: "页码" }),
          el("th", { text: "告警原因" }),
          el("th", { text: "内容摘要" }),
          el("th", { text: "置信度" }),
        ])
      );
      for (const r of items) {
        const d = r.data || {};
        const content =
          r.row_kind === "TABLE"
            ? (d.cells || []).join(" | ")
            : d.clean_content || Object.values(d).filter(Boolean).join(" | ");
        previewBody.appendChild(
          el("tr", { class: "row-warn" }, [
            el("td", { text: r.row_kind === "TABLE" ? "表格行" : "正文条目" }),
            el("td", { text: String(r.page_no) }),
            el("td", { text: (r.warnings || []).join("; ") }),
            el("td", { class: "cell-trunc", text: content, title: content }),
            el("td", { text: `${((r.ocr_confidence || 0) * 100).toFixed(1)}%` }),
          ])
        );
      }
      return;
    }

    const cols = (activeTemplate && activeTemplate.columns) || [];
    const hCells = cols.map((c) => el("th", { text: c.header || c.key }));
    hCells.push(el("th", { text: "质检备注" }));
    previewHead.appendChild(el("tr", {}, hCells));

    for (const r of items) {
      const warns = r.warnings || [];
      const d = r.data || {};
      const tds = cols.map((c) => {
        const val = String(d[c.key || c.header] ?? "");
        return el("td", { class: "cell-trunc", text: val, title: val });
      });
      tds.push(el("td", { text: warns.join("; ") || "通过" }));
      previewBody.appendChild(el("tr", { class: warns.length ? "row-warn" : "" }, tds));
    }
  }

  async function loadTemplates(preferId = null) {
    const res = await apiJson("/api/excel-templates");
    templates = res.items || [];
    renderTemplateSelectors(preferId);
  }

  // ------------------------------------------------------------------
  // Event Bindings
  // ------------------------------------------------------------------
  tplSelect.addEventListener("change", () => {
    const id = Number(tplSelect.value);
    activeTemplate = templates.find((t) => t.id === id) || null;
    if (activeTemplate) {
      exportTplSelect.value = String(activeTemplate.id);
      selectedDrawerId = null;
      applyTemplateToForm(activeTemplate);
      refreshPreview();
    }
  });

  exportTplSelect.addEventListener("change", () => {
    const id = Number(exportTplSelect.value);
    activeTemplate = templates.find((t) => t.id === id) || activeTemplate;
    if (activeTemplate) {
      tplSelect.value = String(activeTemplate.id);
      selectedDrawerId = null;
      applyTemplateToForm(activeTemplate);
      refreshPreview();
    }
  });

  document.querySelectorAll("[data-pack-subtab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      activeSubTab = btn.dataset.packSubtab;
      renderWorkbench();
    });
  });

  const refreshMatchBtn = document.getElementById("refreshMatchBtn");
  if (refreshMatchBtn) {
    refreshMatchBtn.addEventListener("click", async () => {
      await withButtonLoading(refreshMatchBtn, "匹配中...", async () => {
        await refreshPreview();
        showToast("已重新按模板规则完成分拣与对齐", "ok");
      });
    });
  }

  const openCleanRulesBtn = document.getElementById("openCleanRulesBtn");
  if (openCleanRulesBtn) {
    openCleanRulesBtn.addEventListener("click", () => {
      if (activeTemplate) applyTemplateToForm(activeTemplate);
      cleanRulesDialog.showModal();
    });
  }

  const saveRulesDialogBtn = document.getElementById("saveRulesDialogBtn");
  if (saveRulesDialogBtn) {
    saveRulesDialogBtn.addEventListener("click", async () => {
      if (!activeTemplate) return;
      await withButtonLoading(saveRulesDialogBtn, "保存中...", async () => {
        try {
          await apiJson(`/api/excel-templates/${activeTemplate.id}`, {
            method: "PUT",
            body: { rules: readFormRules() },
          });
          await closeTopLayer(cleanRulesDialog);
          await loadTemplates(activeTemplate.id);
          await refreshPreview();
          showToast("清洗规则与 CatBoost 设置已更新", "ok");
        } catch (err) {
          showToast(err.message, "danger");
        }
      });
    });
  }

  const addSheetBtn = document.getElementById("addSheetBtn");
  if (addSheetBtn) {
    addSheetBtn.addEventListener("click", () => {
      document.getElementById("addSheetForm").reset();
      addSheetDialog.showModal();
    });
  }

  const addSheetForm = document.getElementById("addSheetForm");
  if (addSheetForm) {
    addSheetForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!activeTemplate) return;
      const submitBtn = e.target.querySelector('button[type="submit"]');
      await withButtonLoading(submitBtn, "添加中...", async () => {
        try {
          const sName = document.getElementById("newSheetNameInput").value.trim() || "自定义子表";
          const sMode = document.getElementById("newSheetModeSelect").value || "record";
          const sKws = document
            .getElementById("newSheetKeywordsInput")
            .value.split(/[,，、\s]+/)
            .map((x) => x.trim())
            .filter(Boolean);
          const rawFields = document
            .getElementById("newSheetFieldsInput")
            .value.split(/[,，]+/)
            .map((x) => x.trim())
            .filter(Boolean);

          const fields = (rawFields.length ? rawFields : ["批次号", "数值"]).map((fn, idx) => {
            const fl = fn.toLowerCase();
            let role = "num_feature";
            if (fn.includes("批") || fl === "batch_id") role = "meta_id";
            else if (fl.startsWith("y_") || /效价|纯度|收率|得率|判定/.test(fn)) role = "target_y";
            else if (fn.includes("日期") || fn.includes("时刻") || fn.includes("时间")) role = "time_split";
            else if (fn.includes("备注") || fn.includes("说明") || fn.includes("正文") || fl.includes("sop")) role = "text_feature";
            return {
              key: role === "meta_id" ? "batch_id" : `f_${Date.now().toString(36).slice(-4)}_${idx + 1}`,
              name: fn,
              role,
              agg: role === "num_feature" && sMode === "table" ? "stats" : role === "text_feature" ? "join" : "last",
              aliases: [],
            };
          });

          const newSid = `sheet_${Date.now().toString(36).slice(-6)}`;
          const nextSheets = [
            ...(activeTemplate.sheets || []),
            { sheet_id: newSid, name: sName.slice(0, 31), mode: sMode, match_keywords: sKws, fields },
          ];
          await apiJson(`/api/excel-templates/${activeTemplate.id}`, {
            method: "PUT",
            body: { sheets: nextSheets },
          });
          await closeTopLayer(addSheetDialog);
          selectedDrawerId = newSid;
          activeSubTab = "schema";
          await loadTemplates(activeTemplate.id);
          await refreshMatchPreview();
          showToast(`已添加子表抽屉「${sName}」`, "ok");
        } catch (err) {
          showToast(err.message, "danger");
        }
      });
    });
  }

  const convertPackBtn = document.getElementById("convertPackBtn");
  if (convertPackBtn) {
    convertPackBtn.addEventListener("click", async () => {
      if (!activeTemplate) return;
      const entryFields = [
        { key: "batch_id", name: "批次号", role: "meta_id", agg: "last", aliases: ["批号"] },
        { key: "record_date", name: "记录日期", role: "time_split", agg: "last", aliases: ["日期"] },
        { key: "entry_title", name: "记录小节", role: "cat_feature", agg: "last", aliases: ["标题", "章节"] },
        { key: "clean_content", name: "正文内容", role: "text_feature", agg: "join", aliases: ["正文", "说明"] },
      ];
      const nextSheets = [
        {
          sheet_id: "sheet_entries",
          name: activeTemplate.entry_sheet_name || "正文与批次档案",
          mode: "text",
          match_keywords: ["SOP", "正文", "说明", "批记录"],
          fields: entryFields,
        },
        {
          sheet_id: "sheet_tables",
          name: activeTemplate.table_sheet_name || "过程与检测表格",
          mode: "table",
          match_keywords: ["过程", "指标", "检测", "阶段", "温度"],
          fields: [
            { key: "batch_id", name: "批次号", role: "meta_id", agg: "last", aliases: ["批号"] },
            { key: "time_point", name: "时刻阶段", role: "time_split", agg: "last", aliases: ["时刻", "阶段", "工序阶段"] },
            { key: "metric_val", name: "过程指标", role: "num_feature", agg: "stats", aliases: ["目标指标", "指标"] },
            { key: "note", name: "备注说明", role: "text_feature", agg: "join", aliases: ["备注", "说明"] },
          ],
        },
      ];
      await apiJson(`/api/excel-templates/${activeTemplate.id}`, {
        method: "PUT",
        body: { sheets: nextSheets, rules: { ...readFormRules(), catboost_mode: true } },
      });
      await loadTemplates(activeTemplate.id);
      await refreshMatchPreview();
      showToast("已升级为多 Sheet 可视化模板包", "ok");
    });
  }

  const deleteTemplateBtn = document.getElementById("deleteTemplateBtn");
  wireTwoStepAction(deleteTemplateBtn, "确认删除？", async () => {
    if (!activeTemplate) return;
    try {
      await apiJson(`/api/excel-templates/${activeTemplate.id}`, { method: "DELETE" });
      selectedDrawerId = null;
      await loadTemplates();
      await refreshPreview();
      showToast("已删除模板", "ok");
    } catch (err) {
      showToast(err.message, "warn");
    }
  });

  function populateNewTemplatePresets() {
    if (!newTemplatePresetSelect) return;
    newTemplatePresetSelect.replaceChildren();
    const packs = templates.filter((t) => Array.isArray(t.sheets) && t.sheets.length > 0);
    for (const t of packs) {
      const shortLabel = PRESET_SHORT_LABELS[t.name] || t.name;
      newTemplatePresetSelect.appendChild(
        el("option", { value: String(t.id), text: `${shortLabel}` })
      );
    }
    newTemplatePresetSelect.appendChild(
      el("option", { value: "__blank__", text: "空白单子表起步（仅保留批次号 + 目标 y）" })
    );
    if (activeTemplate && isPackTemplate(activeTemplate)) {
      newTemplatePresetSelect.value = String(activeTemplate.id);
    }
    updateNewTemplatePresetHint();
  }

  function updateNewTemplatePresetHint() {
    if (!newTemplatePresetSelect || !newTemplatePresetHint) return;
    const val = newTemplatePresetSelect.value;
    if (val === "__blank__") {
      newTemplatePresetHint.textContent = "新建仅含 1 张「批次信息与结果_Y」的精简多表骨架，适合从零自由添加子表。";
      return;
    }
    const found = templates.find((t) => String(t.id) === val);
    newTemplatePresetHint.textContent = found
      ? found.description || `复制「${found.name}」的 ${(found.sheets || []).length} 张子表结构与字段映射规则。`
      : "";
  }

  if (newTemplatePresetSelect) {
    newTemplatePresetSelect.addEventListener("change", updateNewTemplatePresetHint);
  }

  document.getElementById("newTemplateBtn").addEventListener("click", () => {
    document.getElementById("newTemplateForm").reset();
    populateNewTemplatePresets();
    newTemplateDialog.showModal();
  });

  document.getElementById("newTemplateForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    await withButtonLoading(submitBtn, "创建中...", async () => {
      try {
        const presetVal = newTemplatePresetSelect ? newTemplatePresetSelect.value : "";
        const presetTpl =
          presetVal && presetVal !== "__blank__"
            ? templates.find((t) => String(t.id) === presetVal) || null
            : null;
        const baseSheets =
          presetTpl && Array.isArray(presetTpl.sheets) && presetTpl.sheets.length > 0
            ? JSON.parse(JSON.stringify(presetTpl.sheets))
            : presetVal === "__blank__" || !(activeTemplate && isPackTemplate(activeTemplate))
              ? [
                  {
                    sheet_id: "sheet_1",
                    name: "批次信息与结果_Y",
                    mode: "record",
                    match_keywords: ["批次", "结果", "记录"],
                    fields: [
                      { key: "batch_id", name: "批次号", role: "meta_id", agg: "last", aliases: ["批号"] },
                      { key: "record_date", name: "记录日期", role: "time_split", agg: "last", aliases: ["日期"] },
                      { key: "y_output", name: "y_主产出", role: "target_y", agg: "last", aliases: ["产出", "效价"] },
                    ],
                  },
                ]
              : JSON.parse(JSON.stringify(activeTemplate.sheets));
        const baseRules =
          presetTpl && presetTpl.rules
            ? { ...readFormRules(), ...presetTpl.rules }
            : { ...readFormRules(), catboost_mode: true };
        const descInputVal = document.getElementById("newTemplateDescInput").value.trim();
        const created = await apiJson("/api/excel-templates", {
          method: "POST",
          body: {
            name: document.getElementById("newTemplateNameInput").value,
            description: descInputVal || (presetTpl ? presetTpl.description : ""),
            rules: baseRules,
            columns: editingColumns,
            sheets: baseSheets,
          },
        });
        await closeTopLayer(newTemplateDialog);
        selectedDrawerId = null;
        await loadTemplates(created.id);
        await refreshPreview();
        showToast(`已创建多表模板包「${created.name}」`, "ok");
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });

  document.getElementById("importXlsxHeaderInput").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      fd.append("force_pack", "1");
      const created = await apiJson("/api/excel-templates/from-xlsx", { method: "POST", body: fd });
      selectedDrawerId = null;
      await loadTemplates(created.id);
      await refreshPreview();
      showToast(`已从 ${file.name} 解析并导入 ${(created.sheets || []).length} 张子表模板`, "ok");
    } catch (err) {
      showToast(err.message, "danger");
    } finally {
      e.target.value = "";
    }
  });

  const dcExportXlsxBtn = document.getElementById("dcExportXlsxBtn");
  dcExportXlsxBtn.addEventListener("click", async () => {
    if (!state.currentBookId) return;
    await withButtonLoading(dcExportXlsxBtn, "提交导出...", async () => {
      try {
        const curRules = (activeTemplate && activeTemplate.rules) || {};
        const res = await apiJson(`/api/books/${state.currentBookId}/export`, {
          method: "POST",
          body: {
            format: "xlsx",
            template_id: activeTemplate ? activeTemplate.id : null,
            redact_pii: Boolean(curRules.redact_pii),
            custom_redact_terms: curRules.custom_redact_terms || [],
          },
        });
        const p = (res.job && res.job.params) || {};
        if (p.limited_by_license) {
          showToast(
            `免费体验版单次导出前 ${p.free_export_page_limit || 5} 页（本次 ${p.exported_page_count}/${p.original_page_count} 页），点击右上角激活解锁不限页与 CatBoost 全量导出`,
            "warn"
          );
        } else {
          showToast(`多 Sheet Excel 导出任务 #${res.job.id} 已启动，请稍候前往导出历史下载`, "ok");
        }
        callbacks.onExportTriggered();
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });

  const jumpBtn = document.getElementById("jumpToDatacleanBtn");
  if (jumpBtn) {
    jumpBtn.addEventListener("click", () => {
      const tab4 = document.getElementById("tabBtn-dataclean");
      if (tab4) tab4.click();
    });
  }

  // Legacy-only bindings
  const addSchemaColBtn = document.getElementById("addSchemaColBtn");
  if (addSchemaColBtn) {
    addSchemaColBtn.addEventListener("click", () => {
      editingColumns.push({
        key: `custom_${Date.now().toString().slice(-4)}`,
        header: `自定义列${editingColumns.length + 1}`,
        source_type: "kv",
        pattern: "",
        required: false,
      });
      renderColumnEditor();
    });
  }

  const saveTemplateBtn = document.getElementById("saveTemplateBtn");
  if (saveTemplateBtn) {
    saveTemplateBtn.addEventListener("click", async () => {
      await withButtonLoading(saveTemplateBtn, "保存中...", async () => {
        try {
          await saveCurrentTemplate();
          showToast("Excel 清洗模板已保存", "ok");
        } catch (err) {
          showToast(err.message, "danger");
        }
      });
    });
  }

  const runDataCleanBtn = document.getElementById("runDataCleanBtn");
  if (runDataCleanBtn) {
    runDataCleanBtn.addEventListener("click", async () => {
      if (!state.currentBookId) return;
      await withButtonLoading(runDataCleanBtn, "清洗拆表中...", async () => {
        try {
          const saved = await saveCurrentTemplate();
          const res = await apiJson(`/api/books/${state.currentBookId}/data-clean/run`, {
            method: "POST",
            body: { template_id: saved ? saved.id : null },
          });
          const s = res.summary;
          summaryNote.textContent = `已完成 ${s.pages_processed} 页结构化清洗 · 通过率 ${s.pass_rate_pct}% · 去重 ${s.dedup_removed} 条`;
          previewOffset = 0;
          await refreshPreview();
          showToast(`清洗完成：${s.entries} 条正文，${s.distinct_tables} 张表（${s.table_rows} 行）`, "ok");
        } catch (err) {
          showToast(err.message, "danger");
        }
      });
    });
  }

  document.querySelectorAll("[data-dc-kind]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-dc-kind]").forEach((b) => b.classList.remove("is-active", "btn-primary"));
      btn.classList.add("is-active", "btn-primary");
      previewKind = btn.dataset.dcKind;
      previewOffset = 0;
      refreshPreview();
    });
  });

  const dcPrevBtn = document.getElementById("dcPrevPageBtn");
  if (dcPrevBtn) {
    dcPrevBtn.addEventListener("click", () => {
      if (previewOffset > 0) {
        previewOffset = Math.max(0, previewOffset - previewLimit);
        refreshPreview();
      }
    });
  }

  const dcNextBtn = document.getElementById("dcNextPageBtn");
  if (dcNextBtn) {
    dcNextBtn.addEventListener("click", () => {
      if (previewOffset + previewLimit < previewTotal) {
        previewOffset += previewLimit;
        refreshPreview();
      }
    });
  }

  return {
    loadTemplates,
    refreshPreview,
    getActiveTemplateId: () => (activeTemplate ? activeTemplate.id : null),
  };
}
