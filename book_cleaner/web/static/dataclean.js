/** Stage 2 Data Cleaning & Custom Excel Schema Controller. */

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
  { value: "book_title", label: "书名/卷宗" },
  { value: "page_no", label: "页码" },
  { value: "chapter", label: "所属章节" },
  { value: "entry_title", label: "条目标题" },
  { value: "clean_content", label: "清洗后正文" },
  { value: "ocr_confidence", label: "识别置信度" },
  { value: "qa_status", label: "质检状态" },
  { value: "author", label: "作者" },
  { value: "isbn", label: "ISBN/编号" },
  { value: "source_ref", label: "来源文件名" },
];

export function createDataCleanController(state, callbacks) {
  let templates = [];
  let activeTemplate = null;
  let editingColumns = [];
  let previewKind = "ENTRY";
  let previewOffset = 0;
  const previewLimit = 40;
  let previewTotal = 0;

  const tplSelect = document.getElementById("excelTemplateSelect");
  const exportTplSelect = document.getElementById("exportExcelTemplateSelect");
  const entrySheetInput = document.getElementById("entrySheetNameInput");
  const tableSheetInput = document.getElementById("tableSheetNameInput");
  const schemaColList = document.getElementById("schemaColList");
  const previewHead = document.getElementById("dcPreviewHead");
  const previewBody = document.getElementById("dcPreviewBody");
  const pagerInfo = document.getElementById("dcPagerInfo");
  const summaryNote = document.getElementById("dcSummaryNote");
  const newTemplateDialog = document.getElementById("newTemplateDialog");
  enableDialogLightDismissFallback(newTemplateDialog);

  function renderTemplateSelectors(preferId = null) {
    tplSelect.replaceChildren();
    exportTplSelect.replaceChildren();
    for (const t of templates) {
      tplSelect.appendChild(el("option", { value: String(t.id), text: t.name }));
      exportTplSelect.appendChild(el("option", { value: String(t.id), text: t.name }));
    }
    if (preferId && templates.some((t) => t.id === preferId)) {
      activeTemplate = templates.find((t) => t.id === preferId);
    } else if (activeTemplate && templates.some((t) => t.id === activeTemplate.id)) {
      activeTemplate = templates.find((t) => t.id === activeTemplate.id);
    } else {
      activeTemplate = templates[0] || null;
    }
    if (activeTemplate) {
      tplSelect.value = String(activeTemplate.id);
      exportTplSelect.value = String(activeTemplate.id);
      applyTemplateToForm(activeTemplate);
    }
  }

  function applyTemplateToForm(tpl) {
    entrySheetInput.value = tpl.entry_sheet_name || "正文结构化条目";
    tableSheetInput.value = tpl.table_sheet_name || "提取表格明细";
    const r = tpl.rules || {};
    document.getElementById("ruleFullwidthChk").checked = r.normalize_fullwidth !== false;
    document.getElementById("ruleDatesChk").checked = r.normalize_dates !== false;
    document.getElementById("ruleHeadersChk").checked = r.remove_running_headers !== false;
    document.getElementById("ruleNoiseChk").checked = r.strip_noise_symbols !== false;
    document.getElementById("ruleDedupChk").checked = r.deduplicate_entries !== false;
    document.getElementById("ruleMinConfInput").value = String(r.min_confidence_warn ?? 0.82);

    editingColumns = (tpl.columns || []).map((c) => ({ ...c }));
    renderColumnEditor();
  }

  function readFormRules() {
    return {
      normalize_fullwidth: document.getElementById("ruleFullwidthChk").checked,
      normalize_dates: document.getElementById("ruleDatesChk").checked,
      remove_running_headers: document.getElementById("ruleHeadersChk").checked,
      strip_noise_symbols: document.getElementById("ruleNoiseChk").checked,
      deduplicate_entries: document.getElementById("ruleDedupChk").checked,
      min_confidence_warn: Number(document.getElementById("ruleMinConfInput").value || 0.82),
    };
  }

  function renderColumnEditor() {
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

  async function loadTemplates(preferId = null) {
    const res = await apiJson("/api/excel-templates");
    templates = res.items || [];
    renderTemplateSelectors(preferId);
  }

  async function saveCurrentTemplate() {
    if (!activeTemplate) return;
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
        entry_sheet_name: entrySheetInput.value.trim() || "正文结构化条目",
        table_sheet_name: tableSheetInput.value.trim() || "提取表格明细",
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
      const hCells = [
        el("th", { text: "页码" }),
        el("th", { text: "表#" }),
        el("th", { text: "行类型" }),
      ];
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

    // ENTRY view using current active template columns
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

  // Event bindings
  tplSelect.addEventListener("change", () => {
    const id = Number(tplSelect.value);
    activeTemplate = templates.find((t) => t.id === id) || null;
    if (activeTemplate) {
      exportTplSelect.value = String(activeTemplate.id);
      applyTemplateToForm(activeTemplate);
    }
  });

  exportTplSelect.addEventListener("change", () => {
    const id = Number(exportTplSelect.value);
    activeTemplate = templates.find((t) => t.id === id) || activeTemplate;
    if (activeTemplate) {
      tplSelect.value = String(activeTemplate.id);
      applyTemplateToForm(activeTemplate);
    }
  });

  document.getElementById("addSchemaColBtn").addEventListener("click", () => {
    editingColumns.push({
      key: `custom_${Date.now().toString().slice(-4)}`,
      header: `自定义列${editingColumns.length + 1}`,
      source_type: "kv",
      pattern: "",
      required: false,
    });
    renderColumnEditor();
  });

  const saveTemplateBtn = document.getElementById("saveTemplateBtn");
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

  const deleteTemplateBtn = document.getElementById("deleteTemplateBtn");
  wireTwoStepAction(deleteTemplateBtn, "确认删除？", async () => {
    if (!activeTemplate) return;
    try {
      await apiJson(`/api/excel-templates/${activeTemplate.id}`, { method: "DELETE" });
      await loadTemplates();
      showToast("已删除模板", "ok");
    } catch (err) {
      showToast(err.message, "warn");
    }
  });

  document.getElementById("newTemplateBtn").addEventListener("click", () => {
    document.getElementById("newTemplateForm").reset();
    newTemplateDialog.showModal();
  });

  document.getElementById("newTemplateForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    await withButtonLoading(submitBtn, "创建中...", async () => {
      try {
        const created = await apiJson("/api/excel-templates", {
          method: "POST",
          body: {
            name: document.getElementById("newTemplateNameInput").value,
            description: document.getElementById("newTemplateDescInput").value,
            entry_sheet_name: entrySheetInput.value.trim() || "正文结构化条目",
            table_sheet_name: tableSheetInput.value.trim() || "提取表格明细",
            rules: readFormRules(),
            columns: editingColumns,
          },
        });
        await closeTopLayer(newTemplateDialog);
        await loadTemplates(created.id);
        showToast(`已创建模板「${created.name}」`, "ok");
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
      const created = await apiJson("/api/excel-templates/from-xlsx", { method: "POST", body: fd });
      await loadTemplates(created.id);
      showToast(`已从 ${file.name} 导入 ${created.columns.length} 列模板`, "ok");
    } catch (err) {
      showToast(err.message, "danger");
    } finally {
      e.target.value = "";
    }
  });

  const runDataCleanBtn = document.getElementById("runDataCleanBtn");
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

  const dcExportXlsxBtn = document.getElementById("dcExportXlsxBtn");
  dcExportXlsxBtn.addEventListener("click", async () => {
    if (!state.currentBookId) return;
    await withButtonLoading(dcExportXlsxBtn, "提交导出...", async () => {
      try {
        const saved = await saveCurrentTemplate();
        const res = await apiJson(`/api/books/${state.currentBookId}/export`, {
          method: "POST",
          body: { format: "xlsx", template_id: saved ? saved.id : null },
        });
        showToast(`Excel 导出任务 #${res.job.id} 已启动，请稍候前往导出历史下载`, "ok");
        callbacks.onExportTriggered();
      } catch (err) {
        showToast(err.message, "danger");
      }
    });
  });

  document.querySelectorAll("[data-dc-kind]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-dc-kind]").forEach((b) => b.classList.remove("is-active", "btn-primary"));
      btn.classList.add("is-active", "btn-primary");
      previewKind = btn.dataset.dcKind;
      previewOffset = 0;
      refreshPreview();
    });
  });

  document.getElementById("dcPrevPageBtn").addEventListener("click", () => {
    if (previewOffset > 0) {
      previewOffset = Math.max(0, previewOffset - previewLimit);
      refreshPreview();
    }
  });

  document.getElementById("dcNextPageBtn").addEventListener("click", () => {
    if (previewOffset + previewLimit < previewTotal) {
      previewOffset += previewLimit;
      refreshPreview();
    }
  });

  return {
    loadTemplates,
    refreshPreview,
    getActiveTemplateId: () => (activeTemplate ? activeTemplate.id : null),
  };
}
