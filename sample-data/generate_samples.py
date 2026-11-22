"""生成演示用来源台账 CSV（确定性假哈希，便于复现）。

运行：python3 sample-data/generate_samples.py
输出：sample-data/ledger_initial.csv、sample-data/ledger_correction.csv
"""
from __future__ import annotations

import csv
import hashlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def h(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


IMG_A = h("图01-青铜鼎")
IMG_B = h("图02-壁画")
IMG_C = h("图03-经卷")
IMG_D = h("图04-陶俑")
IMG_E = h("图05-金冠")
IMG_F = h("图06-漆器")

HEADER = ["image_sha256", "library_no", "status", "credit", "summary"]

INITIAL_ROWS = [
    # A：L01 与 L02 均可，字典序测试时 L02 单独可行应胜出场景由 B/C 构造
    [IMG_A, "L01", "approved", "甲馆藏 / 王晋 摄", "商代青铜鼎，正面全形"],
    [IMG_A, "L02", "approved", "乙馆藏 / 李远 摄", "商代青铜鼎，正面全形"],
    # B：L02、L03 可 → 单选 L02 可覆盖 A+B（最少 1 馆的字典序最优）
    [IMG_B, "L02", "approved", "乙馆藏 / 李远 摄", "北魏壁画，供养人局部"],
    [IMG_B, "L03", "approved", "丙馆藏 / 陈岚 摄", "北魏壁画，供养人局部"],
    # C：只在 L03、L04 有准用 → 与 A/B 组合时最少 2 馆，[L02,L03] 字典序最小
    [IMG_C, "L03", "approved", "丙馆藏 / 陈岚 摄", "唐代写经卷，卷首"],
    [IMG_C, "L04", "revoked",  "丁馆藏（已撤回）", "唐代写经卷，卷首"],
    # D：L01、L04 准用
    [IMG_D, "L01", "approved", "甲馆藏 / 王晋 摄", "汉代彩绘陶俑"],
    [IMG_D, "L04", "approved", "丁馆藏 / 周禾 摄", "汉代彩绘陶俑"],
    # E：仅 L05 准用，且 L01 有一条已撤回记录
    [IMG_E, "L05", "approved", "戊馆藏 / 林澈 摄", "辽代镂空金冠"],
    [IMG_E, "L01", "revoked",  "甲馆藏（已撤回）", "辽代镂空金冠"],
    # F：全部撤回 → 缺准用来源，不出表
    [IMG_F, "L02", "revoked",  "乙馆藏（已撤回）", "战国漆耳杯"],
    [IMG_F, "L05", "revoked",  "戊馆藏（已撤回）", "战国漆耳杯"],
]

# 补正台账：把 C 在 L04 的撤回改为准用（新增准用来源，触发组合可能变化），
# 并更正 E/L05 的署名（触发“署名有变”标注），另把 B/L02 撤回（触发确认漂移）。
CORRECTION_ROWS = [
    [IMG_C, "L04", "approved", "丁馆藏 / 周禾 摄", "唐代写经卷，卷首"],
    [IMG_E, "L05", "approved", "戊馆藏 / 林澈、苏明 摄", "辽代镂空金冠"],
    [IMG_B, "L02", "revoked",  "乙馆藏（已撤回）", "北魏壁画，供养人局部"],
]


def write(name: str, rows: list[list[str]]) -> None:
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)
    print("wrote", path, len(rows), "rows")


if __name__ == "__main__":
    write("ledger_initial.csv", INITIAL_ROWS)
    write("ledger_correction.csv", CORRECTION_ROWS)
