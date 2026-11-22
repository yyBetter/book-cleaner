"""Consistent, reproducible pre-export PII redaction (phones, ID cards, emails, signers, custom terms)."""

from __future__ import annotations

import re
from typing import Any


_SIGNER_LABELS = (
    "操作人",
    "操作员",
    "复核人",
    "审核人",
    "批准人",
    "签字人",
    "签名",
    "签字",
    "记录人",
    "检验员",
    "化验员",
    "负责人",
    "班长",
    "经办人",
    "领料人",
    "投料人",
    "分析人",
    "实验人员",
)

_SIGNER_FIELD_KEYWORDS = (
    *_SIGNER_LABELS,
    "姓名",
    "联系人",
    "operator",
    "reviewer",
    "inspector",
    "signer",
    "checker",
)

_NON_NAME_PLACEHOLDERS = {
    "-",
    "--",
    "无",
    "暂无",
    "待签",
    "未签",
    "待定",
    "正常",
    "合格",
    "异常",
    "通过",
    "n/a",
    "na",
    "null",
    "none",
}

_REDACT_TOKEN_RE = re.compile(r"^(?:PERSON|PHONE|IDCARD|EMAIL|TERM)_\d{2,}$")

_IDCARD_RE = re.compile(
    r"(?<![0-9A-Za-z])([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx])(?![0-9A-Za-z])"
)
_EMAIL_RE = re.compile(
    r"(?<![0-9A-Za-z._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![0-9A-Za-z])"
)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?86[\s\-]?)?(1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4})(?!\d)"
)

_LABEL_ALT = "|".join(re.escape(lb) for lb in _SIGNER_LABELS)
_CN_SIGNER_NAME = r"[\u4e00-\u9fff·]{2,4}"
_EN_SIGNER_NAME = r"[A-Za-z][A-Za-z.'-]{1,15}(?: [A-Za-z][A-Za-z.'-]{1,15})?"
_SIGNER_CLAUSE_RE = re.compile(
    rf"((?:{_LABEL_ALT})\s*[:：]\s*)(?!(?:PERSON|PHONE|IDCARD|EMAIL|TERM)_\d{{2,}}\b)"
    rf"({_CN_SIGNER_NAME}(?:\s*[、/&]\s*{_CN_SIGNER_NAME})*|{_EN_SIGNER_NAME})"
)
_SIGNER_SPLIT_RE = re.compile(r"(\s*[,，、/&]+\s*)")
_PIPE_SEP_ROW_RE = re.compile(r"^\|\s*[:\-\s|]+\|$")
_PIPE_KV_SIGNER_RE = re.compile(
    rf"(\|\s*(?:{_LABEL_ALT})\s*\|\s*)([^|\n]+?)(\s*\|)"
)


def parse_custom_redact_terms(raw: Any) -> list[str]:
    """Normalize custom redaction terms from list or delimited string."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,，;；\r\n]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        return []

    seen: set[str] = set()
    terms: list[str] = []
    for p in parts:
        cleaned = str(p or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            terms.append(cleaned)
    return terms


def is_signer_field_name(field_name: str) -> bool:
    """Return True when a column/field name represents a person/signer identifier."""
    fn = str(field_name or "").strip().lower()
    if not fn:
        return False
    return any(kw.lower() in fn for kw in _SIGNER_FIELD_KEYWORDS)


class ConsistentPIIRedactor:
    """Deterministic per-export PII redactor mapping identical entities to stable tokens."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        custom_terms: list[str] | str | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.custom_terms = parse_custom_redact_terms(custom_terms)
        self._maps: dict[str, dict[str, str]] = {
            "TERM": {},
            "IDCARD": {},
            "PHONE": {},
            "EMAIL": {},
            "PERSON": {},
        }
        self._counts: dict[str, int] = {k: 0 for k in self._maps}

        # Combine custom terms (longest-first alternation) so matches assign tokens in text order
        ordered_terms = sorted(self.custom_terms, key=len, reverse=True)
        if ordered_terms:
            alts = [
                f"(?i:{re.escape(t)})" if t.isascii() else re.escape(t)
                for t in ordered_terms
            ]
            self._custom_pattern: re.Pattern[str] | None = re.compile("|".join(alts))
        else:
            self._custom_pattern = None

    def _norm_key(self, category: str, raw_val: str) -> str:
        s = re.sub(r"\s+", " ", str(raw_val or "").strip())
        if category == "PHONE":
            digits = re.sub(r"\D", "", s)
            if len(digits) == 13 and digits.startswith("86"):
                digits = digits[2:]
            return digits or s
        if category in ("EMAIL", "IDCARD"):
            return s.lower()
        if category == "TERM" and s.isascii():
            return s.lower()
        return s

    def _token_for(self, category: str, raw_val: str) -> str:
        clean_raw = str(raw_val or "").strip()
        if not clean_raw or _REDACT_TOKEN_RE.match(clean_raw):
            return clean_raw
        key = self._norm_key(category, clean_raw)
        cat_map = self._maps[category]
        if key not in cat_map:
            cat_map[key] = f"{category}_{len(cat_map) + 1:02d}"
        self._counts[category] += 1
        return cat_map[key]

    def _mask_signer_body(self, body: str) -> str:
        chunks = _SIGNER_SPLIT_RE.split(body)
        out: list[str] = []
        for chunk in chunks:
            if not chunk or _SIGNER_SPLIT_RE.fullmatch(chunk):
                out.append(chunk)
                continue
            cleaned = chunk.strip()
            if not cleaned:
                out.append(chunk)
                continue
            if cleaned.lower() in _NON_NAME_PLACEHOLDERS or _REDACT_TOKEN_RE.match(cleaned):
                out.append(chunk)
                continue
            # If the clause already became a masked phone/email/id, keep token intact
            token = self._token_for("PERSON", cleaned)
            lead = chunk[: len(chunk) - len(chunk.lstrip())]
            trail = chunk[len(chunk.rstrip()) :]
            out.append(f"{lead}{token}{trail}")
        return "".join(out)

    def _redact_pipe_table_signer_cols(self, text: str) -> str:
        lines = text.splitlines()
        out_lines: list[str] = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            s = ln.strip()
            if (
                "|" in s
                and i + 1 < len(lines)
                and _PIPE_SEP_ROW_RE.match(lines[i + 1].strip())
            ):
                headers = [c.strip() for c in s.strip("|").split("|")]
                signer_cols = {idx for idx, h in enumerate(headers) if is_signer_field_name(h)}
                # Handle 2-column vertical KV table like "| 操作人 | 张三 |\n| --- | --- |"
                _non_name_headers = {"温度", "压力", "溶氧", "pH", "数值", "浓度", "纯度", "效价", "收率", "日期", "时刻", "备注", "说明"}
                is_kv_two_col = (
                    len(headers) == 2
                    and any(headers[0] == lb for lb in _SIGNER_LABELS)
                    and headers[1] not in _non_name_headers
                )
                if is_kv_two_col:
                    out_lines.append(_PIPE_KV_SIGNER_RE.sub(lambda m: f"{m.group(1)}{self._mask_signer_body(m.group(2))}{m.group(3)}", ln))
                else:
                    out_lines.append(ln)
                out_lines.append(lines[i + 1])
                i += 2
                while i < len(lines) and "|" in lines[i].strip():
                    row_s = lines[i].strip()
                    raw_cells = row_s.strip("|").split("|")
                    if len(raw_cells) == 2 and raw_cells[0].strip() in _SIGNER_LABELS:
                        out_lines.append(
                            _PIPE_KV_SIGNER_RE.sub(
                                lambda m: f"{m.group(1)}{self._mask_signer_body(m.group(2))}{m.group(3)}",
                                lines[i],
                            )
                        )
                    elif signer_cols:
                        new_cells: list[str] = []
                        for c_idx, cell_raw in enumerate(raw_cells):
                            if c_idx in signer_cols:
                                c_clean = cell_raw.strip()
                                if (
                                    c_clean
                                    and c_clean.lower() not in _NON_NAME_PLACEHOLDERS
                                    and not _REDACT_TOKEN_RE.match(c_clean)
                                ):
                                    new_cells.append(f" {self._mask_signer_body(c_clean)} ")
                                else:
                                    new_cells.append(cell_raw)
                            else:
                                new_cells.append(cell_raw)
                        out_lines.append("|" + "|".join(new_cells) + "|")
                    else:
                        out_lines.append(lines[i])
                    i += 1
                continue
            out_lines.append(
                _PIPE_KV_SIGNER_RE.sub(
                    lambda m: f"{m.group(1)}{self._mask_signer_body(m.group(2))}{m.group(3)}",
                    ln,
                )
            )
            i += 1
        return "\n".join(out_lines)

    def redact_text(self, text: str) -> str:
        """Apply consistent PII masking across arbitrary text."""
        if not self.enabled or not text:
            return text
        out = str(text)

        # 1. Custom user-defined sensitive terms first
        if self._custom_pattern is not None:
            out = self._custom_pattern.sub(lambda m: self._token_for("TERM", m.group(0)), out)

        # 2. 18-digit national ID cards
        out = _IDCARD_RE.sub(lambda m: self._token_for("IDCARD", m.group(1)), out)

        # 3. Email addresses
        out = _EMAIL_RE.sub(lambda m: self._token_for("EMAIL", m.group(1)), out)

        # 4. Mobile phone numbers
        out = _PHONE_RE.sub(lambda m: self._token_for("PHONE", m.group(0)), out)

        # 5. Labeled signer clauses (e.g. "操作人：张三")
        out = _SIGNER_CLAUSE_RE.sub(
            lambda m: f"{m.group(1)}{self._mask_signer_body(m.group(2))}",
            out,
        )

        # 6. Markdown pipe table signer columns (e.g. "| 操作人 | 张三 |" or column under header 操作人)
        if "|" in out:
            out = self._redact_pipe_table_signer_cols(out)

        return out

    def redact_cell(self, value: Any, *, field_name: str = "") -> Any:
        """Redact a structured table cell while preserving numeric/null types."""
        if not self.enabled or value is None or isinstance(value, (bool, int, float)):
            return value
        s = str(value)
        if not s.strip():
            return value

        # Always run general regex/term redaction first
        redacted = self.redact_text(s)

        # If this column explicitly holds person/signer names, consistently mask raw name values
        if is_signer_field_name(field_name):
            cleaned = redacted.strip()
            if (
                cleaned
                and cleaned.lower() not in _NON_NAME_PLACEHOLDERS
                and not _REDACT_TOKEN_RE.match(cleaned)
            ):
                return self._mask_signer_body(cleaned)
        return redacted

    def stats(self) -> dict[str, Any]:
        """Return de-identified redaction counts (never emits raw sensitive strings)."""
        by_cat = {k: int(v) for k, v in self._counts.items() if v > 0}
        unique = {k: len(m) for k, m in self._maps.items() if m}
        total = sum(self._counts.values())
        cat_labels = {
            "PERSON": "姓名/签字",
            "PHONE": "手机号",
            "IDCARD": "身份证号",
            "EMAIL": "邮箱",
            "TERM": "自定义词",
        }
        parts = [f"{cat_labels.get(k, k)} {v} 处" for k, v in by_cat.items()]
        return {
            "enabled": self.enabled,
            "total_redactions": int(total),
            "unique_entities": sum(unique.values()),
            "by_category": by_cat,
            "unique_by_category": unique,
            "summary_text": "、".join(parts) if parts else "0 处命中",
        }
