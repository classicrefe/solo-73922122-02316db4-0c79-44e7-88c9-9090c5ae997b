"""已确认表的现行台账复核：保留原署名，只标出撤回准用 / 署名有变。"""
from __future__ import annotations


def recheck_confirmed(payload: dict, sources: dict[str, list]) -> dict:
    annotations = {}
    withdrawn = changed = 0
    for entry in payload.get("entries", []):
        image_sha = entry["image_sha256"]
        rows = sources.get(image_sha, [])
        current = next(
            (r for r in rows if r["library_no"] == entry["library_no"] and r["status"] == "approved"),
            None,
        )
        if current is None:
            annotations[image_sha] = {
                "flag": "withdrawn",
                "label": "撤回准用",
                "current_credit": "",
            }
            withdrawn += 1
        elif current["credit"] != entry["credit"]:
            annotations[image_sha] = {
                "flag": "credit_changed",
                "label": "署名有变",
                "current_credit": current["credit"],
            }
            changed += 1
        else:
            annotations[image_sha] = {"flag": "ok", "label": "", "current_credit": current["credit"]}
    return {
        "annotations": annotations,
        "withdrawn_count": withdrawn,
        "changed_count": changed,
        "clean": withdrawn == 0 and changed == 0,
    }


def diff_results(snapshot: dict, current: dict) -> list[dict]:
    """确认前重算发现漂移时，逐图列出差异。"""
    if not current.get("feasible"):
        return [
            {"image_sha256": h, "kind": "missing", "detail": "现行台账已无准用来源"}
            for h in current.get("missing", [])
        ]
    cur_by_hash = {e["image_sha256"]: e for e in current["entries"]}
    diffs = []
    for old in snapshot["entries"]:
        new = cur_by_hash.get(old["image_sha256"])
        if new is None:
            diffs.append({"image_sha256": old["image_sha256"], "kind": "missing",
                          "detail": "现行台账已无准用来源"})
        elif new["library_no"] != old["library_no"]:
            diffs.append({"image_sha256": old["image_sha256"], "kind": "library",
                          "detail": f"馆号 {old['library_no']} → {new['library_no']}"})
        elif new["credit"] != old["credit"]:
            diffs.append({"image_sha256": old["image_sha256"], "kind": "credit",
                          "detail": f"署名 {old['credit'] or '（空）'!r} → {new['credit'] or '（空）'!r}"})
    return diffs
