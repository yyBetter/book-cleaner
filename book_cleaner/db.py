"""SQLite WAL state machine and 100k-scale indexed metadata store."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PAGE_STATUS_ORDER = (
    "CAPTURED",
    "CLEANING",
    "CLEANED",
    "OCR_RUNNING",
    "OCR_DONE",
    "LLM_RUNNING",
    "LLM_DONE",
    "READY",
    "NEEDS_REVIEW",
    "APPROVED",
    "FAILED",
)

DEFAULT_CLEAN_PROFILE: dict[str, Any] = {
    "split_spread": False,
    "auto_crop": True,
    "dewarp": True,
    "remove_fingers": True,
    "enhance_mode": "clean_color",
    "denoise": True,
    "deskew": True,
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "ocr": {
        "engine": "rapidocr",
        "strict_offline": True,
        "ollama_base_url": "http://127.0.0.1:11434",
        "api_base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "api_model": "deepseek-ai/DeepSeek-OCR-2",
        "hybrid_conf_threshold": 0.85,
        "hybrid_upgrade_tables": True,
        "timeout_s": 60.0,
    },
    "llm": {
        "mode": "off",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "low_conf_threshold": 0.85,
        "timeout_s": 45.0,
    },
    "capture_profile": dict(DEFAULT_CLEAN_PROFILE),
    "license": {
        "license_key": "",
    },
}

DEFAULT_EXCEL_RULES: dict[str, Any] = {
    "normalize_fullwidth": True,
    "normalize_dates": True,
    "strip_noise_symbols": True,
    "remove_running_headers": True,
    "deduplicate_entries": True,
    "min_confidence_warn": 0.82,
    "catboost_mode": False,
    "redact_pii": False,
    "custom_redact_terms": [],
}

DEFAULT_EXCEL_COLUMNS: list[dict[str, Any]] = [
    {"key": "book_title", "header": "书名/卷宗", "source_type": "builtin", "pattern": "book_title", "required": False},
    {"key": "page_no", "header": "页码", "source_type": "builtin", "pattern": "page_no", "required": False},
    {"key": "chapter", "header": "所属章节", "source_type": "builtin", "pattern": "chapter", "required": False},
    {"key": "entry_title", "header": "条目标题", "source_type": "builtin", "pattern": "entry_title", "required": False},
    {"key": "clean_content", "header": "清洗后正文", "source_type": "builtin", "pattern": "clean_content", "required": True},
    {"key": "dates_found", "header": "规范化日期", "source_type": "regex", "pattern": r"\b\d{4}-\d{2}-\d{2}\b", "required": False},
    {"key": "key_values", "header": "关键参数", "source_type": "kv", "pattern": "*", "required": False},
    {"key": "ocr_confidence", "header": "识别置信度", "source_type": "builtin", "pattern": "ocr_confidence", "required": False},
    {"key": "qa_status", "header": "质检状态", "source_type": "builtin", "pattern": "qa_status", "required": False},
]

CATBOOST_BATCH_RULES: dict[str, Any] = {
    **DEFAULT_EXCEL_RULES,
    "catboost_mode": True,
}

CATBOOST_BATCH_COLUMNS: list[dict[str, Any]] = [
    {"key": "batch_id", "header": "批次号", "source_type": "builtin", "pattern": "batch_id", "required": False},
    {"key": "record_dim", "header": "记录维度", "source_type": "builtin", "pattern": "record_dim", "required": False},
    {"key": "record_date", "header": "规范化日期", "source_type": "regex", "pattern": r"\b\d{4}-\d{2}-\d{2}\b", "required": False},
    {"key": "asset_id", "header": "设备/卷宗编号", "source_type": "builtin", "pattern": "isbn", "required": False},
    {"key": "page_no", "header": "页码", "source_type": "builtin", "pattern": "page_no", "required": False},
    {"key": "entry_title", "header": "记录小节/标题", "source_type": "builtin", "pattern": "entry_title", "required": False},
    {"key": "clean_content", "header": "SOP与过程正文", "source_type": "builtin", "pattern": "clean_content", "required": True},
    {"key": "key_values", "header": "参数键值对", "source_type": "kv", "pattern": "*", "required": False},
    {"key": "ocr_confidence", "header": "识别置信度", "source_type": "builtin", "pattern": "ocr_confidence", "required": False},
    {"key": "qa_status", "header": "质检状态", "source_type": "builtin", "pattern": "qa_status", "required": False},
]

MULTISHEET_CATBOOST_PACK_SHEETS: list[dict[str, Any]] = [
    {
        "sheet_id": "sheet_base",
        "name": "批次基础档案",
        "mode": "record",
        "match_keywords": ["基础信息", "投料", "配料", "批记录", "设备", "档案", "领料"],
        "fields": [
            {"key": "batch_id", "name": "批次号", "role": "meta_id", "agg": "last", "aliases": ["批号", "生产批号", "Lot", "batch_id"], "min_val": None, "max_val": None, "require_not_null": True},
            {"key": "record_date", "name": "记录日期", "role": "time_split", "agg": "last", "aliases": ["日期", "制定日期", "生产日期"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "asset_id", "name": "设备编号", "role": "cat_feature", "agg": "last", "aliases": ["罐号", "工位", "生产线", "机台", "设备/卷宗编号"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "material_lot", "name": "原料批次", "role": "cat_feature", "agg": "last", "aliases": ["料批", "培养基批号", "原料批号"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "feed_mass", "name": "投料量", "role": "num_feature", "agg": "last", "aliases": ["配料量", "投料(kg)", "投料量(kg)"], "min_val": 0.0, "max_val": 1000000.0, "require_not_null": False},
        ],
    },
    {
        "sheet_id": "sheet_process",
        "name": "过程运行记录",
        "mode": "table",
        "match_keywords": ["过程", "巡检", "运行", "阶段", "温度", "压力", "工序", "清晰度", "指标"],
        "fields": [
            {"key": "batch_id", "name": "批次号", "role": "meta_id", "agg": "last", "aliases": ["批号", "生产批号"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "time_point", "name": "记录时刻", "role": "time_split", "agg": "last", "aliases": ["时刻", "时间", "工龄", "阶段", "工序阶段"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "temp_c", "name": "温度", "role": "num_feature", "agg": "stats", "aliases": ["罐温", "温度(℃)", "Temp"], "min_val": 0.0, "max_val": 100.0, "require_not_null": False},
            {"key": "pressure_mpa", "name": "压力", "role": "num_feature", "agg": "stats", "aliases": ["罐压", "压力(MPa)", "风压"], "min_val": 0.0, "max_val": 10.0, "require_not_null": False},
            {"key": "do_pct", "name": "溶氧", "role": "num_feature", "agg": "stats", "aliases": ["DO", "溶解氧", "溶氧(%)"], "min_val": 0.0, "max_val": 100.0, "require_not_null": False},
            {"key": "ph_val", "name": "pH", "role": "num_feature", "agg": "stats", "aliases": ["酸碱度", "pH值"], "min_val": 0.0, "max_val": 14.0, "require_not_null": False},
            {"key": "target_metric", "name": "过程指标", "role": "num_feature", "agg": "stats", "aliases": ["目标指标", "补料速率", "转速"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "process_note", "name": "备注说明", "role": "text_feature", "agg": "join", "aliases": ["备注", "说明"], "min_val": None, "max_val": None, "require_not_null": False},
        ],
    },
    {
        "sheet_id": "sheet_lab",
        "name": "化验检测记录",
        "mode": "table",
        "match_keywords": ["化验", "检测", "采样", "取样", "样品", "含量", "纯度", "镜检", "残糖"],
        "fields": [
            {"key": "batch_id", "name": "批次号", "role": "meta_id", "agg": "last", "aliases": ["批号"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "sample_time", "name": "取样时刻", "role": "time_split", "agg": "last", "aliases": ["采样时间", "时刻", "时间"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "assay_conc", "name": "化验浓度", "role": "num_feature", "agg": "stats", "aliases": ["浓度", "残糖", "效价", "氨基氮"], "min_val": 0.0, "max_val": None, "require_not_null": False},
            {"key": "assay_purity", "name": "过程纯度", "role": "num_feature", "agg": "stats", "aliases": ["纯度", "组分占比", "含量(%)"], "min_val": 0.0, "max_val": 100.0, "require_not_null": False},
            {"key": "micro_check", "name": "镜检结果", "role": "cat_feature", "agg": "last", "aliases": ["镜检", "菌检", "无菌检查"], "min_val": None, "max_val": None, "require_not_null": False},
        ],
    },
    {
        "sheet_id": "sheet_sop",
        "name": "SOP与异常交接",
        "mode": "text",
        "match_keywords": ["SOP", "规程", "操作步骤", "质量门禁", "注意事项", "异常", "交接", "偏差", "数字化"],
        "fields": [
            {"key": "batch_id", "name": "批次号", "role": "meta_id", "agg": "last", "aliases": ["批号"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "record_date", "name": "记录日期", "role": "time_split", "agg": "last", "aliases": ["日期", "制定日期"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "section_title", "name": "记录小节", "role": "cat_feature", "agg": "last", "aliases": ["标题", "章节"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "sop_content", "name": "SOP与异常正文", "role": "text_feature", "agg": "join", "aliases": ["正文", "内容", "交接说明", "清洗后正文"], "min_val": None, "max_val": None, "require_not_null": False},
        ],
    },
    {
        "sheet_id": "sheet_results_y",
        "name": "批次结果指标_Y",
        "mode": "record",
        "match_keywords": ["结果", "放罐", "验收", "终检", "收率", "结算", "汇总", "产出", "成品"],
        "fields": [
            {"key": "batch_id", "name": "批次号", "role": "meta_id", "agg": "last", "aliases": ["批号"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "finish_date", "name": "完成日期", "role": "time_split", "agg": "last", "aliases": ["放罐日期", "验收日期", "日期"], "min_val": None, "max_val": None, "require_not_null": False},
            {"key": "y_main_output", "name": "y_主产出指标", "role": "target_y", "agg": "last", "aliases": ["最终效价", "终检效价", "主产出", "产出指标", "效价"], "min_val": 0.0, "max_val": None, "require_not_null": False},
            {"key": "y_purity", "name": "y_核心纯度", "role": "target_y", "agg": "last", "aliases": ["最终纯度", "核心纯度", "纯度", "组分占比"], "min_val": 0.0, "max_val": 100.0, "require_not_null": False},
            {"key": "y_yield", "name": "y_总收率", "role": "target_y", "agg": "last", "aliases": ["总收率", "收率", "得率", "回收率"], "min_val": 0.0, "max_val": 100.0, "require_not_null": False},
            {"key": "y_grade", "name": "y_质量判定", "role": "target_y", "agg": "last", "aliases": ["质量判定", "合格判定", "质量等级", "判定"], "min_val": None, "max_val": None, "require_not_null": False},
        ],
    },
]

LAB_NOTEBOOK_PACK_SHEETS: list[dict[str, Any]] = [
    {
        "sheet_id": "sheet_lab_sample",
        "name": "样品登记",
        "mode": "record",
        "match_keywords": ["样品", "登记", "实验编号", "课题", "受试物", "试剂", "Sample"],
        "fields": [
            {"key": "batch_id", "name": "实验编号", "role": "meta_id", "agg": "last", "aliases": ["批次号", "样品号", "课题编号", "Sample ID"]},
            {"key": "experiment_date", "name": "实验日期", "role": "time_split", "agg": "last", "aliases": ["日期", "登记日期", "检测日期"]},
            {"key": "sample_name", "name": "样品名称", "role": "cat_feature", "agg": "last", "aliases": ["样品", "试剂名称", "材料名称"]},
            {"key": "operator_name", "name": "实验人员", "role": "cat_feature", "agg": "last", "aliases": ["分析人", "检验员", "操作人"]},
            {"key": "sample_mass_mg", "name": "称样量", "role": "num_feature", "agg": "last", "aliases": ["样品质量", "称样量(mg)", "稀释倍数"]},
        ],
    },
    {
        "sheet_id": "sheet_lab_cond",
        "name": "实验条件",
        "mode": "table",
        "match_keywords": ["实验条件", "反应温度", "搅拌", "梯度", "色谱", "仪参", "试剂配方"],
        "fields": [
            {"key": "batch_id", "name": "实验编号", "role": "meta_id", "agg": "last", "aliases": ["批次号", "样品号"]},
            {"key": "time_point", "name": "采样时刻", "role": "time_split", "agg": "last", "aliases": ["时刻", "时间", "取样点", "阶段"]},
            {"key": "react_temp_c", "name": "反应温度", "role": "num_feature", "agg": "stats", "aliases": ["温度", "柱温", "温度(℃)"]},
            {"key": "ph_val", "name": "体系pH", "role": "num_feature", "agg": "stats", "aliases": ["pH", "酸碱度", "pH值"]},
            {"key": "flow_rate", "name": "流速/转速", "role": "num_feature", "agg": "stats", "aliases": ["流速", "转速", "搅拌速度"]},
            {"key": "cond_note", "name": "条件备注", "role": "text_feature", "agg": "join", "aliases": ["备注", "说明"]},
        ],
    },
    {
        "sheet_id": "sheet_lab_result_y",
        "name": "检测结果_Y",
        "mode": "record",
        "match_keywords": ["检测结果", "测定", "分析结果", "图谱", "峰面积", "收率", "纯度", "结论"],
        "fields": [
            {"key": "batch_id", "name": "实验编号", "role": "meta_id", "agg": "last", "aliases": ["批次号", "样品号"]},
            {"key": "report_date", "name": "报告日期", "role": "time_split", "agg": "last", "aliases": ["日期", "完成日期"]},
            {"key": "y_purity", "name": "y_主成分纯度", "role": "target_y", "agg": "last", "aliases": ["纯度", "主成分纯度", "HPLC纯度", "含量(%)"]},
            {"key": "y_yield", "name": "y_实验收率", "role": "target_y", "agg": "last", "aliases": ["收率", "产率", "回收率"]},
            {"key": "y_metric", "name": "y_核心指标值", "role": "target_y", "agg": "last", "aliases": ["测定值", "吸光度", "效价", "活性"]},
            {"key": "y_conclusion", "name": "y_检测结论", "role": "target_y", "agg": "last", "aliases": ["结论", "判定", "合格判定"]},
        ],
    },
    {
        "sheet_id": "sheet_lab_notes",
        "name": "实验备注",
        "mode": "text",
        "match_keywords": ["实验现象", "实验备注", "SOP", "异常现象", "图谱说明", "改进建议"],
        "fields": [
            {"key": "batch_id", "name": "实验编号", "role": "meta_id", "agg": "last", "aliases": ["批次号", "样品号"]},
            {"key": "note_date", "name": "记录日期", "role": "time_split", "agg": "last", "aliases": ["日期"]},
            {"key": "note_title", "name": "现象小节", "role": "cat_feature", "agg": "last", "aliases": ["标题", "章节"]},
            {"key": "note_content", "name": "实验现象与备注正文", "role": "text_feature", "agg": "join", "aliases": ["正文", "内容", "清洗后正文", "备注"]},
        ],
    },
]

FINANCE_CONTRACT_PACK_SHEETS: list[dict[str, Any]] = [
    {
        "sheet_id": "sheet_fin_contract",
        "name": "合同摘要",
        "mode": "record",
        "match_keywords": ["合同", "协议", "甲方", "乙方", "签约", "标的额", "合同编号", "台账"],
        "fields": [
            {"key": "batch_id", "name": "合同/单据编号", "role": "meta_id", "agg": "last", "aliases": ["批次号", "合同编号", "单号", "凭证号"]},
            {"key": "sign_date", "name": "签订日期", "role": "time_split", "agg": "last", "aliases": ["日期", "签约日期", "开票日期"]},
            {"key": "counterparty", "name": "交易对方", "role": "cat_feature", "agg": "last", "aliases": ["供应商", "客户名称", "乙方", "收款方"]},
            {"key": "contract_title", "name": "合同项目名称", "role": "cat_feature", "agg": "last", "aliases": ["项目名称", "标的物", "标题"]},
            {"key": "total_amount", "name": "合同总金额", "role": "num_feature", "agg": "last", "aliases": ["金额", "总价", "含税总额", "签约金额"]},
        ],
    },
    {
        "sheet_id": "sheet_fin_expense",
        "name": "费用明细表",
        "mode": "table",
        "match_keywords": ["费用明细", "报销", "预算", "采购清单", "单价", "数量", "小计", "税率"],
        "fields": [
            {"key": "batch_id", "name": "合同/单据编号", "role": "meta_id", "agg": "last", "aliases": ["批次号", "合同编号", "报销单号"]},
            {"key": "expense_date", "name": "发生日期", "role": "time_split", "agg": "last", "aliases": ["日期", "费用日期"]},
            {"key": "item_name", "name": "费用科目/品名", "role": "cat_feature", "agg": "last", "aliases": ["品名", "科目", "费用类别", "项目"]},
            {"key": "qty", "name": "数量", "role": "num_feature", "agg": "stats", "aliases": ["件数", "工时"]},
            {"key": "unit_price", "name": "单价", "role": "num_feature", "agg": "stats", "aliases": ["含税单价", "价格"]},
            {"key": "line_amount", "name": "明细金额", "role": "num_feature", "agg": "stats", "aliases": ["金额", "小计", "价税合计"]},
        ],
    },
    {
        "sheet_id": "sheet_fin_invoice",
        "name": "发票核对",
        "mode": "table",
        "match_keywords": ["发票", "税号", "校验码", "价税合计", "专用发票", "普票", "增值税"],
        "fields": [
            {"key": "batch_id", "name": "合同/单据编号", "role": "meta_id", "agg": "last", "aliases": ["批次号", "单号", "合同编号"]},
            {"key": "invoice_date", "name": "开票日期", "role": "time_split", "agg": "last", "aliases": ["日期"]},
            {"key": "invoice_no", "name": "发票号码", "role": "cat_feature", "agg": "last", "aliases": ["发票代码", "票号"]},
            {"key": "tax_amount", "name": "税额", "role": "num_feature", "agg": "stats", "aliases": ["增值税额"]},
            {"key": "invoice_total", "name": "价税合计", "role": "num_feature", "agg": "stats", "aliases": ["金额", "含税金额"]},
        ],
    },
    {
        "sheet_id": "sheet_fin_approval",
        "name": "审批备注",
        "mode": "text",
        "match_keywords": ["审批", "会签", "财务复核", "付款条件", "违约条款", "附件说明"],
        "fields": [
            {"key": "batch_id", "name": "合同/单据编号", "role": "meta_id", "agg": "last", "aliases": ["批次号", "单号", "合同编号"]},
            {"key": "approve_date", "name": "审批日期", "role": "time_split", "agg": "last", "aliases": ["日期"]},
            {"key": "section_title", "name": "条款/审批环节", "role": "cat_feature", "agg": "last", "aliases": ["标题", "章节"]},
            {"key": "approval_text", "name": "审批意见与付款条款", "role": "text_feature", "agg": "join", "aliases": ["正文", "内容", "清洗后正文", "备注"]},
        ],
    },
]


def utc_now_iso() -> str:
    """Return compact UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Database:
    """Thread-safe SQLite WAL repository supporting 100k+ pages."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=15000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA cache_size=-64000;")
            self._local.conn = conn
            with self._conn_lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        with self._conn_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
        if hasattr(self._local, "conn"):
            delattr(self._local, "conn")

    def _init_schema(self) -> None:
        conn = self._get_conn()
        with self._write_lock:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    isbn TEXT NOT NULL DEFAULT '',
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    seq_no INTEGER NOT NULL,
                    spread_side TEXT NOT NULL DEFAULT 'S',
                    source_type TEXT NOT NULL DEFAULT 'upload',
                    source_ref TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL,
                    dhash TEXT NOT NULL DEFAULT '',
                    raw_path TEXT NOT NULL DEFAULT '',
                    clean_path TEXT NOT NULL DEFAULT '',
                    thumb_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'CAPTURED',
                    blur_score REAL NOT NULL DEFAULT 0.0,
                    brightness REAL NOT NULL DEFAULT 0.0,
                    is_duplicate INTEGER NOT NULL DEFAULT 0,
                    ocr_confidence REAL NOT NULL DEFAULT 0.0,
                    ocr_text TEXT NOT NULL DEFAULT '',
                    clean_text TEXT NOT NULL DEFAULT '',
                    ocr_blocks_json TEXT NOT NULL DEFAULT '[]',
                    qa_flags_json TEXT NOT NULL DEFAULT '[]',
                    clean_meta_json TEXT NOT NULL DEFAULT '{}',
                    llm_status TEXT NOT NULL DEFAULT 'OFF',
                    error_msg TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    locked_by TEXT NOT NULL DEFAULT '',
                    locked_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pages_book_seq ON pages(book_id, seq_no);
                CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status, id);
                CREATE INDEX IF NOT EXISTS idx_pages_book_status ON pages(book_id, status);
                CREATE INDEX IF NOT EXISTS idx_pages_book_sha ON pages(book_id, sha256);

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    book_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    total_items INTEGER NOT NULL DEFAULT 0,
                    done_items INTEGER NOT NULL DEFAULT 0,
                    failed_items INTEGER NOT NULL DEFAULT 0,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    result_path TEXT NOT NULL DEFAULT '',
                    error_msg TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_book ON jobs(book_id, id DESC);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS excel_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    entry_sheet_name TEXT NOT NULL DEFAULT '正文结构化条目',
                    table_sheet_name TEXT NOT NULL DEFAULT '提取表格明细',
                    rules_json TEXT NOT NULL,
                    columns_json TEXT NOT NULL,
                    sheets_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS page_sheet_assignments (
                    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    template_id INTEGER NOT NULL REFERENCES excel_templates(id) ON DELETE CASCADE,
                    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                    sheet_id TEXT NOT NULL,
                    llm_rows_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (book_id, template_id, page_id)
                );

                CREATE INDEX IF NOT EXISTS idx_assign_book_tpl ON page_sheet_assignments(book_id, template_id);

                CREATE TABLE IF NOT EXISTS structured_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    template_id INTEGER,
                    row_kind TEXT NOT NULL,
                    page_id INTEGER,
                    page_no INTEGER NOT NULL DEFAULT 0,
                    table_index INTEGER NOT NULL DEFAULT 0,
                    row_index INTEGER NOT NULL DEFAULT 0,
                    data_json TEXT NOT NULL,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    row_hash TEXT NOT NULL DEFAULT '',
                    ocr_confidence REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_struct_book_kind ON structured_rows(book_id, row_kind, id);
                CREATE INDEX IF NOT EXISTS idx_struct_book_warn ON structured_rows(book_id, warnings_json);
                """
            )
            tpl_cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(excel_templates)").fetchall()
            }
            if "sheets_json" not in tpl_cols:
                conn.execute(
                    "ALTER TABLE excel_templates ADD COLUMN sheets_json TEXT NOT NULL DEFAULT '[]'"
                )
            now = utc_now_iso()
            for key, default_val in DEFAULT_SETTINGS.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO settings(key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (key, json.dumps(default_val, ensure_ascii=False), now),
                )
            cnt_row = conn.execute("SELECT COUNT(*) AS cnt FROM excel_templates").fetchone()
            if cnt_row and int(cnt_row["cnt"]) == 0:
                conn.execute(
                    """
                    INSERT INTO excel_templates(
                        name, description, entry_sheet_name, table_sheet_name,
                        rules_json, columns_json, sheets_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "通用书籍混合清洗模板",
                        "内置规则清洗 + 正文结构化条目 + 表格自动拆Sheet + 异常标黄",
                        "正文结构化条目",
                        "提取表格明细",
                        json.dumps(DEFAULT_EXCEL_RULES, ensure_ascii=False),
                        json.dumps(DEFAULT_EXCEL_COLUMNS, ensure_ascii=False),
                        "[]",
                        now,
                        now,
                    ),
                )
            cb_row = conn.execute(
                "SELECT 1 FROM excel_templates WHERE name = ? LIMIT 1",
                ("批次记录与 CatBoost 建模标准模板",),
            ).fetchone()
            if cb_row is None:
                conn.execute(
                    """
                    INSERT INTO excel_templates(
                        name, description, entry_sheet_name, table_sheet_name,
                        rules_json, columns_json, sheets_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "批次记录与 CatBoost 建模标准模板",
                        "复杂SOP+多维批次表格自动沉淀为标准化宽表与CatBoost字段契约",
                        "正文结构化条目",
                        "提取表格明细",
                        json.dumps(CATBOOST_BATCH_RULES, ensure_ascii=False),
                        json.dumps(CATBOOST_BATCH_COLUMNS, ensure_ascii=False),
                        "[]",
                        now,
                        now,
                    ),
                )
            pack_row = conn.execute(
                "SELECT id, sheets_json FROM excel_templates WHERE name = ? LIMIT 1",
                ("多表批次记录与多目标 CatBoost 模板包",),
            ).fetchone()
            if pack_row is None:
                conn.execute(
                    """
                    INSERT INTO excel_templates(
                        name, description, entry_sheet_name, table_sheet_name,
                        rules_json, columns_json, sheets_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "多表批次记录与多目标 CatBoost 模板包",
                        "适用：制造 / 医药 / 化工 / 生产批记录（多Sheet抽屉分拣 + 字段别名自学习 + 多目标 y CatBoost 宽表）",
                        "正文结构化条目",
                        "提取表格明细",
                        json.dumps(CATBOOST_BATCH_RULES, ensure_ascii=False),
                        json.dumps(CATBOOST_BATCH_COLUMNS, ensure_ascii=False),
                        json.dumps(MULTISHEET_CATBOOST_PACK_SHEETS, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            else:
                try:
                    existing_sheets = json.loads(pack_row["sheets_json"] or "[]")
                except json.JSONDecodeError:
                    existing_sheets = []
                if isinstance(existing_sheets, list) and existing_sheets:
                    default_bounds: dict[tuple[str, str], dict[str, Any]] = {}
                    for ds in MULTISHEET_CATBOOST_PACK_SHEETS:
                        sid = str(ds.get("sheet_id") or "")
                        for df in ds.get("fields") or []:
                            fk = str(df.get("key") or "")
                            if sid and fk:
                                default_bounds[(sid, fk)] = {
                                    "min_val": df.get("min_val"),
                                    "max_val": df.get("max_val"),
                                    "require_not_null": bool(df.get("require_not_null", False)),
                                }
                    changed = False
                    for es in existing_sheets:
                        sid = str(es.get("sheet_id") or "")
                        for ef in es.get("fields") or []:
                            fk = str(ef.get("key") or "")
                            preset = default_bounds.get((sid, fk))
                            if preset and ("min_val" not in ef and "max_val" not in ef):
                                ef["min_val"] = preset["min_val"]
                                ef["max_val"] = preset["max_val"]
                                if "require_not_null" not in ef:
                                    ef["require_not_null"] = preset["require_not_null"]
                                changed = True
                    if changed:
                        conn.execute(
                            "UPDATE excel_templates SET sheets_json = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(existing_sheets, ensure_ascii=False), now, int(pack_row["id"])),
                        )
            for tpl_name, tpl_desc, tpl_rules, tpl_sheets in (
                (
                    "实验室检测与研发实验本模板包",
                    "适用：研发实验本、质检化验单与小试记录（样品登记 / 实验条件 / 检测结果_Y / 实验备注）",
                    CATBOOST_BATCH_RULES,
                    LAB_NOTEBOOK_PACK_SHEETS,
                ),
                (
                    "财务凭证与合同台账模板包",
                    "适用：合同台账、采购费用明细、发票核对与审批凭证（合同摘要 / 费用明细表 / 发票核对 / 审批备注）",
                    {**DEFAULT_EXCEL_RULES, "catboost_mode": False},
                    FINANCE_CONTRACT_PACK_SHEETS,
                ),
            ):
                exists = conn.execute(
                    "SELECT 1 FROM excel_templates WHERE name = ? LIMIT 1",
                    (tpl_name,),
                ).fetchone()
                if exists is None:
                    conn.execute(
                        """
                        INSERT INTO excel_templates(
                            name, description, entry_sheet_name, table_sheet_name,
                            rules_json, columns_json, sheets_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tpl_name,
                            tpl_desc,
                            "正文结构化条目",
                            "提取表格明细",
                            json.dumps(tpl_rules, ensure_ascii=False),
                            json.dumps(DEFAULT_EXCEL_COLUMNS, ensure_ascii=False),
                            json.dumps(tpl_sheets, ensure_ascii=False),
                            now,
                            now,
                        ),
                    )

    @staticmethod
    def _row_to_book(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        try:
            prof = json.loads(d.get("profile_json") or "{}")
        except json.JSONDecodeError:
            prof = {}
        d["profile"] = {**DEFAULT_CLEAN_PROFILE, **prof}
        return d

    @staticmethod
    def _row_to_page(row: sqlite3.Row | None, include_blocks: bool = True) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for src, dst, default in (
            ("qa_flags_json", "qa_flags", []),
            ("clean_meta_json", "clean_meta", {}),
        ):
            try:
                d[dst] = json.loads(d.get(src) or json.dumps(default))
            except json.JSONDecodeError:
                d[dst] = default
        if include_blocks:
            try:
                d["ocr_blocks"] = json.loads(d.get("ocr_blocks_json") or "[]")
            except json.JSONDecodeError:
                d["ocr_blocks"] = []
        d["is_duplicate"] = bool(d.get("is_duplicate", 0))
        return d

    @staticmethod
    def _row_to_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        try:
            d["params"] = json.loads(d.get("params_json") or "{}")
        except json.JSONDecodeError:
            d["params"] = {}
        return d

    # ------------------------------------------------------------------
    # Books
    # ------------------------------------------------------------------
    def create_book(
        self,
        title: str,
        author: str = "",
        isbn: str = "",
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = {**DEFAULT_CLEAN_PROFILE, **(profile or {})}
        now = utc_now_iso()
        conn = self._get_conn()
        with self._write_lock:
            cur = conn.execute(
                """
                INSERT INTO books(title, author, isbn, profile_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    title.strip() or "未命名书籍",
                    author.strip(),
                    isbn.strip(),
                    json.dumps(merged, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            book_id = int(cur.lastrowid)
        book = self.get_book(book_id)
        assert book is not None
        return book

    def get_book(self, book_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM books WHERE id = ?", (int(book_id),)).fetchone()
        return self._row_to_book(row)

    def list_books(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM books ORDER BY id DESC").fetchall()
        books: list[dict[str, Any]] = []
        for row in rows:
            b = self._row_to_book(row)
            if b is not None:
                b["stats"] = self.get_book_stats(b["id"])
                books.append(b)
        return books

    def update_book(self, book_id: int, **fields: Any) -> dict[str, Any] | None:
        existing = self.get_book(book_id)
        if existing is None:
            return None
        updates: dict[str, Any] = {}
        for k in ("title", "author", "isbn"):
            if k in fields and fields[k] is not None:
                updates[k] = str(fields[k]).strip()
        if "profile" in fields and isinstance(fields["profile"], dict):
            merged = {**existing["profile"], **fields["profile"]}
            updates["profile_json"] = json.dumps(merged, ensure_ascii=False)
        if not updates:
            return existing
        updates["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [int(book_id)]
        conn = self._get_conn()
        with self._write_lock:
            conn.execute(f"UPDATE books SET {assignments} WHERE id = ?", params)
        return self.get_book(book_id)

    def delete_book(self, book_id: int) -> bool:
        conn = self._get_conn()
        with self._write_lock:
            cur = conn.execute("DELETE FROM books WHERE id = ?", (int(book_id),))
            return cur.rowcount > 0

    def ensure_default_book(self) -> dict[str, Any]:
        books = self.list_books()
        if books:
            return books[0]
        return self.create_book(title="默认书籍工作集 #1")

    # ------------------------------------------------------------------
    # Pages & Worker Queue
    # ------------------------------------------------------------------
    def next_seq_no(self, book_id: int) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COALESCE(MAX(seq_no), 0) AS max_seq FROM pages WHERE book_id = ?",
            (int(book_id),),
        ).fetchone()
        return int(row["max_seq"]) + 1 if row else 1

    def has_sha256(self, book_id: int, sha256: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM pages WHERE book_id = ? AND sha256 = ? LIMIT 1",
            (int(book_id), sha256),
        ).fetchone()
        return row is not None

    def get_recent_dhashes(self, book_id: int, limit: int = 5) -> list[str]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT dhash FROM pages
            WHERE book_id = ? AND dhash != ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(book_id), int(limit)),
        ).fetchall()
        return [str(r["dhash"]) for r in rows]

    def insert_page(
        self,
        *,
        book_id: int,
        seq_no: int | None = None,
        spread_side: str = "S",
        source_type: str = "upload",
        source_ref: str = "",
        sha256: str,
        dhash: str = "",
        raw_path: str = "",
        clean_path: str = "",
        thumb_path: str = "",
        status: str = "CAPTURED",
        blur_score: float = 0.0,
        brightness: float = 0.0,
        is_duplicate: bool = False,
        qa_flags: list[str] | None = None,
        clean_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        conn = self._get_conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                if seq_no is None:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(seq_no), 0) AS max_seq FROM pages WHERE book_id = ?",
                        (int(book_id),),
                    ).fetchone()
                    seq_val = (int(row["max_seq"]) + 1) if row else 1
                else:
                    seq_val = int(seq_no)

                cur = conn.execute(
                    """
                    INSERT INTO pages(
                        book_id, seq_no, spread_side, source_type, source_ref,
                        sha256, dhash, raw_path, clean_path, thumb_path, status,
                        blur_score, brightness, is_duplicate,
                        qa_flags_json, clean_meta_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(book_id),
                        seq_val,
                        spread_side,
                        source_type,
                        source_ref,
                        sha256,
                        dhash,
                        raw_path,
                        clean_path,
                        thumb_path,
                        status,
                        float(blur_score),
                        float(brightness),
                        1 if is_duplicate else 0,
                        json.dumps(qa_flags or [], ensure_ascii=False),
                        json.dumps(clean_meta or {}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                page_id = int(cur.lastrowid)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        page = self.get_page(page_id)
        assert page is not None
        return page

    def get_page(self, page_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM pages WHERE id = ?", (int(page_id),)).fetchone()
        return self._row_to_page(row, include_blocks=True)

    def update_page(self, page_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed_direct = {
            "seq_no",
            "raw_path",
            "clean_path",
            "thumb_path",
            "status",
            "blur_score",
            "brightness",
            "ocr_confidence",
            "ocr_text",
            "clean_text",
            "llm_status",
            "error_msg",
            "retry_count",
            "locked_by",
            "locked_at",
        }
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k in allowed_direct:
                updates[k] = v
            elif k == "is_duplicate":
                updates["is_duplicate"] = 1 if v else 0
            elif k == "qa_flags":
                updates["qa_flags_json"] = json.dumps(v or [], ensure_ascii=False)
            elif k == "ocr_blocks":
                updates["ocr_blocks_json"] = json.dumps(v or [], ensure_ascii=False)
            elif k == "clean_meta":
                updates["clean_meta_json"] = json.dumps(v or {}, ensure_ascii=False)
        if not updates:
            return self.get_page(page_id)
        updates["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [int(page_id)]
        conn = self._get_conn()
        with self._write_lock:
            conn.execute(f"UPDATE pages SET {assignments} WHERE id = ?", params)
        return self.get_page(page_id)

    def delete_page(self, page_id: int) -> dict[str, Any] | None:
        page = self.get_page(page_id)
        if page is None:
            return None
        conn = self._get_conn()
        with self._write_lock:
            conn.execute("DELETE FROM pages WHERE id = ?", (int(page_id),))
        return page

    def delete_pages_batch(self, book_id: int, page_ids: list[int]) -> list[dict[str, Any]]:
        clean_ids = sorted({int(pid) for pid in page_ids if int(pid) > 0})
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        conn = self._get_conn()
        with self._write_lock:
            rows = conn.execute(
                f"SELECT * FROM pages WHERE book_id = ? AND id IN ({placeholders})",
                [int(book_id), *clean_ids],
            ).fetchall()
            deleted = [
                p for p in (self._row_to_page(r, include_blocks=False) for r in rows) if p is not None
            ]
            if deleted:
                conn.execute(
                    f"DELETE FROM pages WHERE book_id = ? AND id IN ({placeholders})",
                    [int(book_id), *clean_ids],
                )
        return deleted

    def delete_flagged_pages(self, book_id: int) -> list[dict[str, Any]]:
        conn = self._get_conn()
        where = (
            "book_id = ? AND "
            "(qa_flags_json != '[]' OR is_duplicate = 1 OR status IN ('NEEDS_REVIEW', 'FAILED'))"
        )
        with self._write_lock:
            rows = conn.execute(f"SELECT * FROM pages WHERE {where}", (int(book_id),)).fetchall()
            deleted = [
                p for p in (self._row_to_page(r, include_blocks=False) for r in rows) if p is not None
            ]
            if deleted:
                conn.execute(f"DELETE FROM pages WHERE {where}", (int(book_id),))
        return deleted

    def clear_book_pages(self, book_id: int) -> list[dict[str, Any]]:
        conn = self._get_conn()
        with self._write_lock:
            rows = conn.execute("SELECT * FROM pages WHERE book_id = ?", (int(book_id),)).fetchall()
            deleted = [
                p for p in (self._row_to_page(r, include_blocks=False) for r in rows) if p is not None
            ]
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute("DELETE FROM structured_rows WHERE book_id = ?", (int(book_id),))
                conn.execute("DELETE FROM pages WHERE book_id = ?", (int(book_id),))
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return deleted

    def recover_stale_locks(self) -> dict[str, int]:
        """Reset any in-flight worker states after an unexpected restart."""
        transitions = {
            "CLEANING": "CAPTURED",
            "OCR_RUNNING": "CLEANED",
            "LLM_RUNNING": "OCR_DONE",
        }
        now = utc_now_iso()
        recovered: dict[str, int] = {}
        conn = self._get_conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                for stale_status, reset_status in transitions.items():
                    cur = conn.execute(
                        """
                        UPDATE pages
                        SET status = ?, locked_by = '', locked_at = '', updated_at = ?
                        WHERE status = ?
                        """,
                        (reset_status, now, stale_status),
                    )
                    recovered[stale_status] = int(cur.rowcount)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return recovered

    def claim_next_pages(
        self,
        from_status: str,
        to_status: str,
        worker_id: str,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        """Atomically claim up to `limit` queued pages for processing."""
        now = utc_now_iso()
        conn = self._get_conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                rows = conn.execute(
                    """
                    SELECT id FROM pages
                    WHERE status = ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (from_status, int(limit)),
                ).fetchall()
                if not rows:
                    conn.execute("COMMIT;")
                    return []
                ids = [int(r["id"]) for r in rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE pages
                    SET status = ?, locked_by = ?, locked_at = ?, updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [to_status, worker_id, now, now, *ids],
                )
                claimed_rows = conn.execute(
                    f"SELECT * FROM pages WHERE id IN ({placeholders}) ORDER BY id ASC",
                    ids,
                ).fetchall()
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return [
            p for p in (self._row_to_page(r, include_blocks=True) for r in claimed_rows) if p is not None
        ]

    def retry_failed_pages(self, book_id: int | None = None) -> int:
        now = utc_now_iso()
        conn = self._get_conn()
        with self._write_lock:
            if book_id is not None:
                cur = conn.execute(
                    """
                    UPDATE pages
                    SET status = CASE
                        WHEN clean_path != '' THEN 'CLEANED'
                        ELSE 'CAPTURED'
                    END,
                    error_msg = '',
                    locked_by = '',
                    locked_at = '',
                    updated_at = ?
                    WHERE status = 'FAILED' AND book_id = ?
                    """,
                    (now, int(book_id)),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE pages
                    SET status = CASE
                        WHEN clean_path != '' THEN 'CLEANED'
                        ELSE 'CAPTURED'
                    END,
                    error_msg = '',
                    locked_by = '',
                    locked_at = '',
                    updated_at = ?
                    WHERE status = 'FAILED'
                    """,
                    (now,),
                )
            return int(cur.rowcount)

    def _aggregate_counts(self, where_clause: str = "", params: tuple[Any, ...] = ()) -> dict[str, Any]:
        conn = self._get_conn()
        sql = f"""
            SELECT status, COUNT(*) AS cnt
            FROM pages
            {where_clause}
            GROUP BY status
        """
        rows = conn.execute(sql, params).fetchall()
        counts = {s: 0 for s in PAGE_STATUS_ORDER}
        total = 0
        for r in rows:
            s = str(r["status"])
            c = int(r["cnt"])
            counts[s] = c
            total += c

        completed = counts["READY"] + counts["APPROVED"] + counts["LLM_DONE"]
        pending_clean = counts["CAPTURED"] + counts["CLEANING"]
        pending_ocr = counts["CLEANED"] + counts["OCR_RUNNING"]
        pending_llm = counts["OCR_DONE"] + counts["LLM_RUNNING"]
        needs_review = counts["NEEDS_REVIEW"]
        failed = counts["FAILED"]
        return {
            "total": total,
            "by_status": counts,
            "completed": completed,
            "pending_clean": pending_clean,
            "pending_ocr": pending_ocr,
            "pending_llm": pending_llm,
            "needs_review": needs_review,
            "approved": counts["APPROVED"],
            "ready": counts["READY"],
            "failed": failed,
        }

    def get_book_stats(self, book_id: int) -> dict[str, Any]:
        return self._aggregate_counts("WHERE book_id = ?", (int(book_id),))

    def get_global_stats(self) -> dict[str, Any]:
        return self._aggregate_counts()

    def list_pages(
        self,
        book_id: int,
        *,
        status_filter: str | None = None,
        qa_filter: str | None = None,
        search: str | None = None,
        order: str = "asc",
        limit: int = 40,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = ["book_id = ?"]
        params: list[Any] = [int(book_id)]

        if status_filter and status_filter != "ALL":
            if status_filter == "COMPLETED":
                clauses.append("status IN ('READY', 'APPROVED', 'LLM_DONE')")
            elif status_filter == "PROCESSING":
                clauses.append(
                    "status IN ('CAPTURED', 'CLEANING', 'CLEANED', 'OCR_RUNNING', 'OCR_DONE', 'LLM_RUNNING')"
                )
            else:
                clauses.append("status = ?")
                params.append(status_filter)

        if qa_filter and qa_filter != "ALL":
            if qa_filter == "BLURRY":
                clauses.append("qa_flags_json LIKE '%blurry%'")
            elif qa_filter == "DUPLICATE":
                clauses.append("is_duplicate = 1")
            elif qa_filter == "LOW_CONF":
                clauses.append("qa_flags_json LIKE '%low_ocr_confidence%'")
            elif qa_filter == "ANY_QA":
                clauses.append("(qa_flags_json != '[]' OR is_duplicate = 1)")

        if search:
            q = f"%{search.strip()}%"
            clauses.append("(clean_text LIKE ? OR ocr_text LIKE ? OR source_ref LIKE ?)")
            params.extend([q, q, q])

        where_sql = " AND ".join(clauses)
        conn = self._get_conn()
        total_row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM pages WHERE {where_sql}",
            params,
        ).fetchone()
        total_count = int(total_row["cnt"]) if total_row else 0

        order_sql = "seq_no DESC, id DESC" if str(order).lower().strip() == "desc" else "seq_no ASC, id ASC"
        rows = conn.execute(
            f"""
            SELECT id, book_id, seq_no, spread_side, source_type, source_ref,
                   sha256, dhash, raw_path, clean_path, thumb_path, status,
                   blur_score, brightness, is_duplicate, ocr_confidence,
                   ocr_text, clean_text, qa_flags_json, clean_meta_json,
                   llm_status, error_msg, retry_count, created_at, updated_at
            FROM pages
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, int(limit), int(offset)],
        ).fetchall()
        pages = [
            p for p in (self._row_to_page(r, include_blocks=False) for r in rows) if p is not None
        ]
        return pages, total_count

    def list_export_pages(self, book_id: int) -> list[dict[str, Any]]:
        """Fetch all ordered pages of a book for export."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM pages
            WHERE book_id = ? AND status != 'FAILED'
            ORDER BY seq_no ASC, id ASC
            """,
            (int(book_id),),
        ).fetchall()
        return [p for p in (self._row_to_page(r, include_blocks=True) for r in rows) if p is not None]

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    def create_job(
        self,
        job_type: str,
        book_id: int | None = None,
        total_items: int = 0,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        conn = self._get_conn()
        with self._write_lock:
            cur = conn.execute(
                """
                INSERT INTO jobs(job_type, book_id, status, total_items, params_json, created_at, updated_at)
                VALUES (?, ?, 'PENDING', ?, ?, ?, ?)
                """,
                (
                    job_type,
                    book_id,
                    int(total_items),
                    json.dumps(params or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            job_id = int(cur.lastrowid)
        job = self.get_job(job_id)
        assert job is not None
        return job

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return self._row_to_job(row)

    def update_job(self, job_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {"status", "total_items", "done_items", "failed_items", "result_path", "error_msg"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_job(job_id)
        updates["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [int(job_id)]
        conn = self._get_conn()
        with self._write_lock:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", params)
        return self.get_job(job_id)

    def list_jobs(self, book_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
        conn = self._get_conn()
        if book_id is not None:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE book_id = ? ORDER BY id DESC LIMIT ?",
                (int(book_id), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [j for j in (self._row_to_job(r) for r in rows) if j is not None]

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        conn = self._get_conn()
        rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
        out = {k: dict(v) for k, v in DEFAULT_SETTINGS.items()}
        for r in rows:
            k = str(r["key"])
            try:
                val = json.loads(r["value_json"])
            except json.JSONDecodeError:
                val = {}
            if k in out and isinstance(out[k], dict) and isinstance(val, dict):
                out[k] = {**out[k], **val}
            else:
                out[k] = val
        return out

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get_settings()
        now = utc_now_iso()
        conn = self._get_conn()
        with self._write_lock:
            for k, v in patch.items():
                if k in current and isinstance(current[k], dict) and isinstance(v, dict):
                    merged = {**current[k], **v}
                else:
                    merged = v
                conn.execute(
                    """
                    INSERT INTO settings(key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (k, json.dumps(merged, ensure_ascii=False), now),
                )
        return self.get_settings()

    # ------------------------------------------------------------------
    # Excel Templates & Structured Data Rows
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_template(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        try:
            rules = json.loads(d.get("rules_json") or "{}")
        except json.JSONDecodeError:
            rules = {}
        try:
            cols = json.loads(d.get("columns_json") or "[]")
        except json.JSONDecodeError:
            cols = []
        try:
            sheets = json.loads(d.get("sheets_json") or "[]")
        except json.JSONDecodeError:
            sheets = []
        d["rules"] = {**DEFAULT_EXCEL_RULES, **rules}
        d["columns"] = cols
        d["sheets"] = sheets if isinstance(sheets, list) else []
        return d

    @staticmethod
    def _row_to_structured(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        try:
            d["data"] = json.loads(d.get("data_json") or "{}")
        except json.JSONDecodeError:
            d["data"] = {}
        try:
            d["warnings"] = json.loads(d.get("warnings_json") or "[]")
        except json.JSONDecodeError:
            d["warnings"] = []
        return d

    def list_excel_templates(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM excel_templates ORDER BY id ASC").fetchall()
        return [t for t in (self._row_to_template(r) for r in rows) if t is not None]

    def get_excel_template(self, template_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM excel_templates WHERE id = ?", (int(template_id),)).fetchone()
        return self._row_to_template(row)

    def get_default_excel_template(self) -> dict[str, Any]:
        templates = self.list_excel_templates()
        if templates:
            return templates[0]
        return self.create_excel_template(name="通用书籍混合清洗模板")

    def create_excel_template(
        self,
        *,
        name: str,
        description: str = "",
        entry_sheet_name: str = "正文结构化条目",
        table_sheet_name: str = "提取表格明细",
        rules: dict[str, Any] | None = None,
        columns: list[dict[str, Any]] | None = None,
        sheets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        merged_rules = {**DEFAULT_EXCEL_RULES, **(rules or {})}
        cols = columns if columns is not None else [dict(c) for c in DEFAULT_EXCEL_COLUMNS]
        sheets_list = sheets if isinstance(sheets, list) else []
        conn = self._get_conn()
        with self._write_lock:
            cur = conn.execute(
                """
                INSERT INTO excel_templates(
                    name, description, entry_sheet_name, table_sheet_name,
                    rules_json, columns_json, sheets_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip() or "自定义模板",
                    description.strip(),
                    entry_sheet_name.strip() or "正文结构化条目",
                    table_sheet_name.strip() or "提取表格明细",
                    json.dumps(merged_rules, ensure_ascii=False),
                    json.dumps(cols, ensure_ascii=False),
                    json.dumps(sheets_list, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            tpl_id = int(cur.lastrowid)
        tpl = self.get_excel_template(tpl_id)
        assert tpl is not None
        return tpl

    def update_excel_template(self, template_id: int, **fields: Any) -> dict[str, Any] | None:
        existing = self.get_excel_template(template_id)
        if existing is None:
            return None
        updates: dict[str, Any] = {}
        for k in ("name", "description", "entry_sheet_name", "table_sheet_name"):
            if k in fields and fields[k] is not None:
                updates[k] = str(fields[k]).strip()
        if "rules" in fields and isinstance(fields["rules"], dict):
            updates["rules_json"] = json.dumps(
                {**existing["rules"], **fields["rules"]}, ensure_ascii=False
            )
        if "columns" in fields and isinstance(fields["columns"], list):
            updates["columns_json"] = json.dumps(fields["columns"], ensure_ascii=False)
        if "sheets" in fields and isinstance(fields["sheets"], list):
            updates["sheets_json"] = json.dumps(fields["sheets"], ensure_ascii=False)
        if not updates:
            return existing
        updates["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [int(template_id)]
        conn = self._get_conn()
        with self._write_lock:
            conn.execute(f"UPDATE excel_templates SET {assignments} WHERE id = ?", params)
        return self.get_excel_template(template_id)

    def list_page_assignments(self, book_id: int, template_id: int) -> dict[int, dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT page_id, sheet_id, llm_rows_json, updated_at
            FROM page_sheet_assignments
            WHERE book_id = ? AND template_id = ?
            """,
            (int(book_id), int(template_id)),
        ).fetchall()
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            try:
                llm_rows = json.loads(r["llm_rows_json"] or "[]")
            except json.JSONDecodeError:
                llm_rows = []
            out[int(r["page_id"])] = {
                "page_id": int(r["page_id"]),
                "sheet_id": str(r["sheet_id"]),
                "llm_rows": llm_rows if isinstance(llm_rows, list) else [],
                "updated_at": str(r["updated_at"]),
            }
        return out

    def set_page_assignment(
        self,
        book_id: int,
        template_id: int,
        page_id: int,
        sheet_id: str,
        llm_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        sheet_clean = str(sheet_id or "").strip()
        conn = self._get_conn()
        now = utc_now_iso()
        with self._write_lock:
            if sheet_clean == "__auto__" and llm_rows is None:
                conn.execute(
                    """
                    DELETE FROM page_sheet_assignments
                    WHERE book_id = ? AND template_id = ? AND page_id = ?
                    """,
                    (int(book_id), int(template_id), int(page_id)),
                )
                return
            if llm_rows is None:
                existing = conn.execute(
                    """
                    SELECT llm_rows_json FROM page_sheet_assignments
                    WHERE book_id = ? AND template_id = ? AND page_id = ?
                    """,
                    (int(book_id), int(template_id), int(page_id)),
                ).fetchone()
                llm_json = str(existing["llm_rows_json"]) if existing else "[]"
            else:
                llm_json = json.dumps(llm_rows, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO page_sheet_assignments(
                    book_id, template_id, page_id, sheet_id, llm_rows_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id, template_id, page_id) DO UPDATE SET
                    sheet_id = excluded.sheet_id,
                    llm_rows_json = excluded.llm_rows_json,
                    updated_at = excluded.updated_at
                """,
                (int(book_id), int(template_id), int(page_id), sheet_clean, llm_json, now),
            )

    def learn_template_field_alias(
        self,
        template_id: int,
        sheet_id: str,
        field_key: str,
        source_alias: str,
    ) -> dict[str, Any]:
        tpl = self.get_excel_template(template_id)
        if tpl is None:
            raise KeyError(f"Excel template {template_id} not found")
        alias_clean = str(source_alias or "").strip()
        if not alias_clean:
            raise ValueError("字段别名不能为空")
        alias_norm = alias_clean.lower()

        sheets = [dict(s) for s in (tpl.get("sheets") or [])]
        target_sheet = next((s for s in sheets if s.get("sheet_id") == sheet_id), None)
        if target_sheet is None:
            raise KeyError(f"Sheet {sheet_id} not found in template {template_id}")

        updated_fields: list[dict[str, Any]] = []
        found_field = False
        for raw_f in target_sheet.get("fields") or []:
            f = dict(raw_f)
            aliases = [str(a).strip() for a in (f.get("aliases") or []) if str(a).strip()]
            # Remove conflicting alias from sibling fields so one source col maps cleanly
            aliases = [a for a in aliases if a.lower() != alias_norm]
            if f.get("key") == field_key:
                found_field = True
                if str(f.get("name", "")).strip().lower() != alias_norm:
                    aliases.append(alias_clean)
            f["aliases"] = aliases
            updated_fields.append(f)

        if not found_field:
            raise KeyError(f"Field {field_key} not found in sheet {sheet_id}")
        target_sheet["fields"] = updated_fields

        updated = self.update_excel_template(template_id, sheets=sheets)
        assert updated is not None
        return updated

    def delete_excel_template(self, template_id: int) -> bool:
        templates = self.list_excel_templates()
        if len(templates) <= 1:
            raise ValueError("至少需要保留一个 Excel 清洗模板")
        conn = self._get_conn()
        with self._write_lock:
            cur = conn.execute("DELETE FROM excel_templates WHERE id = ?", (int(template_id),))
            return cur.rowcount > 0

    def replace_structured_rows(
        self,
        book_id: int,
        template_id: int | None,
        rows: list[dict[str, Any]],
    ) -> int:
        now = utc_now_iso()
        conn = self._get_conn()
        payload = [
            (
                int(book_id),
                template_id,
                str(r.get("row_kind", "ENTRY")),
                r.get("page_id"),
                int(r.get("page_no", 0)),
                int(r.get("table_index", 0)),
                int(r.get("row_index", 0)),
                json.dumps(r.get("data") or {}, ensure_ascii=False),
                json.dumps(r.get("warnings") or [], ensure_ascii=False),
                str(r.get("row_hash", "")),
                float(r.get("ocr_confidence", 0.0)),
                now,
            )
            for r in rows
        ]
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute("DELETE FROM structured_rows WHERE book_id = ?", (int(book_id),))
                if payload:
                    conn.executemany(
                        """
                        INSERT INTO structured_rows(
                            book_id, template_id, row_kind, page_id, page_no,
                            table_index, row_index, data_json, warnings_json,
                            row_hash, ocr_confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        payload,
                    )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return len(payload)

    def list_structured_rows(
        self,
        book_id: int,
        *,
        kind: str = "ENTRY",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = ["book_id = ?"]
        params: list[Any] = [int(book_id)]
        k = (kind or "ENTRY").upper().strip()
        if k in ("ENTRY", "TABLE"):
            clauses.append("row_kind = ?")
            params.append(k)
        elif k == "WARNINGS":
            clauses.append("warnings_json != '[]'")

        where_sql = " AND ".join(clauses)
        conn = self._get_conn()
        total_row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM structured_rows WHERE {where_sql}",
            params,
        ).fetchone()
        total = int(total_row["cnt"]) if total_row else 0
        rows = conn.execute(
            f"""
            SELECT * FROM structured_rows
            WHERE {where_sql}
            ORDER BY id ASC
            LIMIT ? OFFSET ?
            """,
            [*params, int(limit), int(offset)],
        ).fetchall()
        items = [r for r in (self._row_to_structured(x) for x in rows) if r is not None]
        return items, total

    def list_all_structured_rows(self, book_id: int) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM structured_rows WHERE book_id = ? ORDER BY id ASC",
            (int(book_id),),
        ).fetchall()
        return [r for r in (self._row_to_structured(x) for x in rows) if r is not None]

    def get_structured_summary(self, book_id: int) -> dict[str, Any]:
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_rows,
                COALESCE(SUM(CASE WHEN row_kind = 'ENTRY' THEN 1 ELSE 0 END), 0) AS entries,
                COALESCE(SUM(CASE WHEN row_kind = 'TABLE' THEN 1 ELSE 0 END), 0) AS table_rows,
                COALESCE(SUM(CASE WHEN warnings_json != '[]' THEN 1 ELSE 0 END), 0) AS warning_rows,
                COUNT(DISTINCT CASE WHEN row_kind = 'TABLE' THEN page_no || '-' || table_index END) AS distinct_tables
            FROM structured_rows
            WHERE book_id = ?
            """,
            (int(book_id),),
        ).fetchone()
        if row is None:
            return {"total_rows": 0, "entries": 0, "table_rows": 0, "warning_rows": 0, "distinct_tables": 0}
        return {
            "total_rows": int(row["total_rows"]),
            "entries": int(row["entries"]),
            "table_rows": int(row["table_rows"]),
            "warning_rows": int(row["warning_rows"]),
            "distinct_tables": int(row["distinct_tables"]),
        }
