"""逐图署名组合求解。

规则：
- 每张图选一家“准用(approved)”馆；无准用来源的图列入 missing，本次不出表。
- 整册用到的馆数最少；并列时取“馆号升序序列”字典序最小的组合。
- 一张图在入选馆集合中有多家准用时，取最小馆号。
- 涉及的准用馆总数超过 16 家时拒绝求解（枚举安全边界）。
"""
from __future__ import annotations

from itertools import combinations

MAX_IMAGES = 100
MAX_LIBRARIES = 16


class SolveError(ValueError):
    pass


def _summary_of(rows: list) -> str:
    # 取该图非空摘要里更新时间最新的一条
    best = ""
    best_ts = -1.0
    for r in rows:
        if r["summary"] and r["updated_at"] >= best_ts:
            best, best_ts = r["summary"], r["updated_at"]
    return best


def build_candidate_map(sources: dict[str, list]) -> dict[str, list[str]]:
    """image_sha -> 该图全部准用馆号（升序、去重）。"""
    candidates: dict[str, list[str]] = {}
    for image_sha, rows in sources.items():
        libs = sorted({r["library_no"] for r in rows if r["status"] == "approved"})
        if libs:
            candidates[image_sha] = libs
    return candidates


def solve(selected_hashes: list[str], sources: dict[str, list]) -> dict:
    """返回 {entries, missing, libraries, library_count, eligible}。

    entries 仅在所有选中图都有准用来源时才会生成；否则只回 missing。
    """
    if not selected_hashes:
        raise SolveError("未勾选任何图片")
    if len(selected_hashes) > MAX_IMAGES:
        raise SolveError(f"最多勾选 {MAX_IMAGES} 张图，本次选择了 {len(selected_hashes)} 张")

    unknown = [h for h in selected_hashes if h not in sources]
    candidates = build_candidate_map({h: sources.get(h, []) for h in selected_hashes})
    missing = [h for h in selected_hashes if h not in candidates]
    if unknown or missing:
        # 缺准用来源：列出图片，本次不出表
        return {
            "feasible": False,
            "entries": [],
            "missing": missing,
            "libraries": [],
            "library_count": 0,
            "eligible": [],
        }

    eligible = sorted({lib for libs in candidates.values() for lib in libs})
    if len(eligible) > MAX_LIBRARIES:
        raise SolveError(
            f"涉及准用馆 {len(eligible)} 家，超过上限 {MAX_LIBRARIES} 家；"
            "请缩小选图范围或先补正台账"
        )

    chosen_set: tuple[str, ...] | None = None
    # 馆数从少到多；同一馆数下 combinations 按 eligible 升序枚举，
    # 首个可行组合即馆号升序序列字典序最小者。
    for size in range(1, len(eligible) + 1):
        for combo in combinations(eligible, size):
            combo_set = set(combo)
            if all(any(lib in combo_set for lib in candidates[h]) for h in selected_hashes):
                chosen_set = combo
                break
        if chosen_set is not None:
            break

    if chosen_set is None:  # 理论上不会发生（全集一定覆盖）
        raise SolveError("未找到可行的馆组合")

    entries = []
    for image_sha in selected_hashes:
        rows = sources[image_sha]
        # 多个入选馆准用 -> 取最小馆号
        library_no = min(lib for lib in candidates[image_sha] if lib in set(chosen_set))
        row = next(r for r in rows if r["library_no"] == library_no and r["status"] == "approved")
        entries.append(
            {
                "image_sha256": image_sha,
                "summary": _summary_of(rows),
                "library_no": library_no,
                "credit": row["credit"],
            }
        )

    return {
        "feasible": True,
        "entries": entries,
        "missing": [],
        "libraries": list(chosen_set),
        "library_count": len(chosen_set),
        "eligible": eligible,
    }


def same_table(result_a: dict, result_b: dict) -> bool:
    """两份结果是否构成同一份表（图序、馆号、署名完全一致）。"""
    if not (result_a.get("feasible") and result_b.get("feasible")):
        return False
    keys = ("image_sha256", "library_no", "credit")
    return [[e[k] for k in keys] for e in result_a["entries"]] == [
        [e[k] for k in keys] for e in result_b["entries"]
    ]
