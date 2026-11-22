"""来源台账 CSV 解析与校验。

支持的表头（中/英文别名均可，首行识别）：
    image_sha256 / 图像sha256 / sha256
    library_no   / 馆号
    status       / 准用状态 / 状态      （approved|revoked，或 准用|撤回）
    credit       / 署名
    summary      / 摘要
"""
from __future__ import annotations

import csv
import io

from .storage import upsert_source

HEADER_ALIASES = {
    "image_sha256": {"image_sha256", "sha256", "图像sha256", "图像 sha256", "图片sha256", "哈希", "hash"},
    "library_no": {"library_no", "library", "馆号", "馆藏号", "馆藏馆号"},
    "status": {"status", "准用状态", "状态", "许可状态"},
    "credit": {"credit", "署名", "版权署名", "credit_line"},
    "summary": {"summary", "摘要", "图片摘要", "说明"},
}
REQUIRED = ("image_sha256", "library_no", "status", "credit", "summary")

STATUS_MAP = {
    "approved": "approved",
    "revoked": "revoked",
    "准用": "approved",
    "已准用": "approved",
    "同意": "approved",
    "授权": "approved",
    "撤回": "revoked",
    "撤回准用": "revoked",
    "撤销": "revoked",
    "已撤回": "revoked",
    "拒绝": "revoked",
    "未准用": "revoked",
}


class LedgerError(ValueError):
    pass


def _norm_header(name: str) -> str:
    return (name or "").strip().lstrip("﻿").lower().replace(" ", "")


def parse_ledger(raw_bytes: bytes) -> list[dict]:
    """解析台账，返回规范化字典列表；不合法直接抛 LedgerError（带行号）。"""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LedgerError(f"文件不是 UTF-8 编码：{exc}") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise LedgerError("CSV 为空，缺少表头")

    col_index: dict[str, int] = {}
    for idx, raw_name in enumerate(header):
        key = _norm_header(raw_name)
        for canonical, aliases in HEADER_ALIASES.items():
            if key in {_norm_header(a) for a in aliases}:
                col_index.setdefault(canonical, idx)
    missing = [c for c in REQUIRED if c not in col_index]
    if missing:
        raise LedgerError(
            "表头缺少必需列：" + "、".join(missing)
            + f"。当前表头：{', '.join(h.strip() for h in header)}"
        )

    records: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not (c or "").strip() for c in row):
            continue
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        image_sha = row[col_index["image_sha256"]].strip()
        library_no = row[col_index["library_no"]].strip()
        raw_status = row[col_index["status"]].strip().lower()
        credit = row[col_index["credit"]].strip()
        summary = row[col_index["summary"]].strip()

        if not image_sha:
            raise LedgerError(f"第 {line_no} 行：图像 SHA-256 不能为空")
        if not library_no:
            raise LedgerError(f"第 {line_no} 行：馆号不能为空")
        if raw_status not in STATUS_MAP:
            raise LedgerError(
                f"第 {line_no} 行：状态值 {raw_status!r} 无法识别，"
                "请使用 approved/revoked 或 准用/撤回"
            )
        key = (image_sha, library_no)
        if key in seen:
            raise LedgerError(f"第 {line_no} 行：(图像 {image_sha[:12]}…, 馆号 {library_no}) 在文件内重复")
        seen.add(key)
        records.append(
            {
                "image_sha256": image_sha,
                "library_no": library_no,
                "status": STATUS_MAP[raw_status],
                "credit": credit,
                "summary": summary,
            }
        )

    if not records:
        raise LedgerError("CSV 没有任何数据行")
    return records


def import_ledger(conn, raw_bytes: bytes) -> dict:
    """解析并 upsert 进 SQLite，返回统计。"""
    records = parse_ledger(raw_bytes)
    approved = revoked = 0
    for rec in records:
        upsert_source(conn, **rec)
        if rec["status"] == "approved":
            approved += 1
        else:
            revoked += 1
    return {"total": len(records), "approved": approved, "revoked": revoked}
