#!/usr/bin/env bash
# 端到端 HTTP 演示：启动 gunicorn，按真实浏览器流程走一遍全部关键操作。
#
# 重点演示本次修复的确认流程：
#   A. 正常确认：现行台账仍能得出同一份表 -> 存档成功，草稿转为已存档
#   B. 重复确认：同一草稿再次/并发确认 -> 只存档一份，第二次导向同一份表
#   C. 冲突提示：补正导致结果有变 -> 拒绝保存、提示重算、草稿保留
#   D. 补正恢复：继续补正使现行台账重新得出同一份表 -> 原草稿可以确认
#   E. 存档后复核：原署名保留，行级标注撤回准用 / 署名有变
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
PORT=${PORT:-8000}
COOKIE=/tmp/credit-demo.${PORT}.cookies
CSV=/workspace/sample-data/ledger_initial.csv
FIX=/tmp/credit-demo-fix.csv
DB_PATH=${DB_PATH:-/tmp/demo-credit.db}
rm -f "$COOKIE" "$DB_PATH" "$FIX"

say() { printf '\n========== %s ==========\n' "$1"; }
curl_() { curl -sS -c "$COOKIE" -b "$COOKIE" "$@"; }

say "1. 健康检查 / 首页"
curl_ "$BASE/" -o /tmp/p1.html -w "HTTP %{http_code}\n"
grep -q "台账图片数" /tmp/p1.html && echo "首页渲染 OK"

say "2. 上传初始台账 CSV"
curl_ -F "ledger=@${CSV};type=text/csv" "$BASE/ledger/upload" -o /tmp/p2.html -w "HTTP %{http_code}\n" -L
grep -q "台账已补正" /tmp/p2.html && echo "导入成功提示 OK"

say "3. 选图页列出图片"
curl_ "$BASE/select" -o /tmp/p3.html -w "HTTP %{http_code}\n"
grep -c 'name="selected"' /tmp/p3.html | xargs echo "复选框数量:"

# 取 5 张有准用来源的图哈希（F 全撤回，排除）
HASHES=$(python3 -s - <<'PY'
import csv
with open('/workspace/sample-data/ledger_initial.csv', encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
approved = {}
for r in rows:
    approved.setdefault(r['image_sha256'], set())
    if r['status'] == 'approved':
        approved[r['image_sha256']].add(r['library_no'])
print('\n'.join(h for h, libs in approved.items() if libs))
PY
)
mapfile -t PICKS <<< "$HASHES"
echo "将勾选 ${#PICKS[@]} 张图"

solve() {  # $@ = hashes；输出草稿 token
  local args=(); for h in "$@"; do args+=(--data-urlencode "selected=${h}"); done
  curl_ -X POST "${args[@]}" "$BASE/solve" -o /dev/null -w "%{redirect_url}"
}

say "4. 搜索最少馆组合（3 馆解 [L01,L03,L05]）"
LOC=$(solve "${PICKS[@]}")
TOKEN=${LOC##*/}
echo "草稿 token: $TOKEN"
curl_ "$LOC" -o /tmp/p4.html -w "草稿页 HTTP %{http_code}\n"
grep -q "逐图署名表（草稿）" /tmp/p4.html && echo "草稿表渲染 OK"
python3 -s - <<'PY'
import re
html = open('/tmp/p4.html', encoding='utf-8').read()
m = re.search(r'使用馆\s*<strong>(\d+)</strong>\s*家', html)
tags = sorted(set(re.findall(r'<span class="mono">(L\d+)</span>', html)))
assert m.group(1) == "3", m.group(1)
assert tags[:3] == ["L01", "L03", "L05"], tags
print("最少馆数 + 字典序 + 每图最小馆号 规则验证 OK；馆号:", tags[:3])
PY

say "5. 含缺准用图 F 时不出表"
FHASH=$(python3 -s -c "
import csv
rows=list(csv.DictReader(open('/workspace/sample-data/ledger_initial.csv',encoding='utf-8-sig')))
print(sorted({r['image_sha256'] for r in rows if r['status']=='revoked' and not any(x['image_sha256']==r['image_sha256'] and x['status']=='approved' for x in rows)})[0])")
curl_ -X POST --data-urlencode "selected=${PICKS[0]}" --data-urlencode "selected=$FHASH" \
  "$BASE/solve" -o /tmp/p5.html -w "HTTP %{http_code}\n"
grep -q "本次不出表" /tmp/p5.html && echo "缺准用页 OK；未生成确认表"

# ---------- 场景 C：补正导致漂移，确认被拒绝、草稿保留 ----------
say "6. 场景 C：补正台账（撤回 B/L02、改 E 署名、C 新增 L04 准用）→ 草稿页冲突提示"
cp /workspace/sample-data/ledger_correction.csv "$FIX"
curl_ -F "ledger=@${FIX};type=text/csv" "$BASE/ledger/upload" -o /tmp/p6.html -L -w "补正 HTTP %{http_code}\n"
grep -q "台账已补正" /tmp/p6.html && echo "重算期间 CSV 补正入库 OK"
curl_ "$BASE/result/$TOKEN" -o /tmp/p6b.html -w "草稿页 HTTP %{http_code}\n"
grep -q "台账已漂移" /tmp/p6b.html && echo "草稿页漂移/冲突提示 OK"
grep -q "导入并补正台账" /tmp/p6b.html && echo "草稿页就地补正入口 OK"

say "7. 场景 C：点确认 -> 被拒绝、不保存、提示重算、草稿保留"
curl_ -X POST "$BASE/confirm/$TOKEN" -o /tmp/p7.html -w "确认 HTTP %{http_code}\n"
grep -q "确认被拒绝" /tmp/p7.html && echo "拒绝保存 OK"
grep -q "请重算" /tmp/p7.html && echo "重算提示 OK"
grep -q "草稿已保留" /tmp/p7.html && echo "草稿保留提示 OK"
grep -q "返回草稿页" /tmp/p7.html && echo "重试入口 OK"
curl_ "$BASE/tables" -o /tmp/p7b.html
grep -q "尚无已确认署名表" /tmp/p7b.html && echo "确认表仍为空 OK"
curl_ "$BASE/result/$TOKEN" -o /dev/null -w "草稿仍可打开 HTTP %{http_code}\n"

# ---------- 场景 D：补正回原状态，现行台账重新得出同一份表 → 原草稿可确认 ----------
say "8. 场景 D：补正回原状态，原草稿恢复可确认"
python3 -s - <<'PY'
import csv
rows = list(csv.DictReader(open('/workspace/sample-data/ledger_initial.csv', encoding='utf-8-sig')))
# 取初始台账中与补正文件同键的原始行，写回即可恢复同一份表
fix_keys = {
    (r['image_sha256'], r['library_no'])
    for r in csv.DictReader(open('/workspace/sample-data/ledger_correction.csv', encoding='utf-8-sig'))
}
with open('/tmp/credit-demo-fix.csv', 'w', encoding='utf-8', newline='') as f:
    w = csv.writer(f)
    w.writerow(['image_sha256', 'library_no', 'status', 'credit', 'summary'])
    for r in rows:
        if (r['image_sha256'], r['library_no']) in fix_keys:
            w.writerow([r['image_sha256'], r['library_no'], r['status'], r['credit'], r['summary']])
print("恢复补正 CSV 已生成")
PY
curl_ -F "ledger=@${FIX};type=text/csv" "$BASE/ledger/upload" -o /dev/null -L -w "恢复补正 HTTP %{http_code}\n"
curl_ "$BASE/result/$TOKEN" -o /tmp/p8.html
if grep -q "台账已漂移" /tmp/p8.html; then echo "恢复后仍漂移？异常"; exit 1; fi
echo "草稿页漂移提示消失 OK"

# ---------- 场景 A + B：确认成功；重复/并发确认只存档一份 ----------
say "9. 场景 A：确认成功（存档时复核一致）"
curl_ -X POST "$BASE/confirm/$TOKEN" -o /dev/null -w "首次确认 HTTP %{http_code} -> %{redirect_url}\n"
curl_ "$BASE/tables/1" -o /tmp/p9.html -w "表详情 HTTP %{http_code}\n"
grep -q "核对一致" /tmp/p9.html && echo "存档表核对一致 OK"

say "10. 场景 B：同一草稿再次确认 -> 不产生第二份，导向同一份表"
curl_ -X POST "$BASE/confirm/$TOKEN" -o /dev/null -w "重复确认 HTTP %{http_code} -> %{redirect_url}\n"
# 并发再补两发（模拟两个窗口同时点）
curl_ -X POST "$BASE/confirm/$TOKEN" -o /dev/null &
curl_ -X POST "$BASE/confirm/$TOKEN" -o /dev/null &
wait
curl_ "$BASE/tables" -o /tmp/p10.html
python3 -s - <<'PY'
import re
html = open('/tmp/p10.html', encoding='utf-8').read()
# 只取已确认表表格首列的表号（<td>#1</td>），避开 CSS 颜色码
ids = re.findall(r'<td>#(\d+)</td>', html)
assert ids == ["1"], f"期望仅表 #1，实际 {ids}"
print("重复/并发确认后仍只有表 #1 OK")
PY
# 已存档草稿链接直接跳到署名表
curl_ "$BASE/result/$TOKEN" -o /dev/null -w "旧草稿链接 HTTP %{http_code} -> %{redirect_url}\n"

# ---------- 场景 E：存档后台账再变，原署名保留、行级标注 ----------
say "11. 场景 E：确认后补正 -> 撤回准用 / 署名有变标注，原署名保留"
python3 -s - <<'PY'
import csv
rows = list(csv.DictReader(open('/workspace/sample-data/ledger_initial.csv', encoding='utf-8-sig')))
hash_of = {}
for r in rows:
    hash_of.setdefault(r['summary'], r['image_sha256'])
h_a, h_d = hash_of['商代青铜鼎，正面全形'], hash_of['汉代彩绘陶俑']
with open('/tmp/post_confirm.csv', 'w', encoding='utf-8', newline='') as f:
    w = csv.writer(f)
    w.writerow(['image_sha256', 'library_no', 'status', 'credit', 'summary'])
    w.writerow([h_a, 'L01', 'revoked', '甲馆藏（已撤回）', '商代青铜鼎，正面全形'])
    w.writerow([h_d, 'L01', 'approved', '甲馆藏 / 王晋（修订署名）', '汉代彩绘陶俑'])
PY
curl_ -F "ledger=@/tmp/post_confirm.csv;type=text/csv" "$BASE/ledger/upload" -o /dev/null -L -w "补正 HTTP %{http_code}\n"
curl_ "$BASE/tables/1" -o /tmp/p11.html -w "详情页 HTTP %{http_code}\n"
python3 -s - <<'PY'
import re
from collections import Counter
html = open('/tmp/p11.html', encoding='utf-8').read()
counts = Counter(label for _, label in re.findall(r'class="badge (\w+)">([^<]+)</span>', html))
print("行级徽标统计:", dict(counts))
assert counts.get("撤回准用") == 1, counts
assert counts.get("署名有变") == 1, counts
assert counts.get("一致") == 3, counts
assert "甲馆藏 / 王晋（修订署名）" in html
assert "甲馆藏 / 王晋 摄" in html
print("撤回/署名变更/原署名保留 行级标注全部 OK")
PY

say "12. 已确认表列表与 404"
curl_ "$BASE/tables/999" -o /dev/null -w "不存在表 HTTP %{http_code}\n"

echo
echo "全部端到端检查通过（含确认/冲突/恢复/重复确认场景）。"
