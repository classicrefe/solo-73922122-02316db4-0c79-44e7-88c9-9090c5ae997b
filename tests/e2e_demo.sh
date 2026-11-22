#!/usr/bin/env bash
# 端到端 HTTP 演示：启动 gunicorn，按真实浏览器流程走一遍全部关键操作。
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
PORT=${PORT:-8000}
COOKIE=/tmp/credit-demo.${PORT}.cookies
CSV=/workspace/sample-data/ledger_initial.csv
FIX=/workspace/sample-data/ledger_correction.csv
DB_PATH=${DB_PATH:-/tmp/demo-credit.db}
rm -f "$COOKIE" "$DB_PATH"

say() { printf '\n========== %s ==========\n' "$1"; }

say "1. 健康检查 / 首页"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/" -o /tmp/p1.html -w "HTTP %{http_code}\n"
grep -q "台账图片数" /tmp/p1.html && echo "首页渲染 OK"

say "2. 上传初始台账 CSV（12 行）"
curl -sS -c "$COOKIE" -b "$COOKIE" -F "ledger=@${CSV};type=text/csv" \
  "$BASE/ledger/upload" -o /tmp/p2.html -w "HTTP %{http_code}\n" -L
grep -q "台账已补正" /tmp/p2.html && echo "导入成功提示 OK"

say "3. 选图页列出 6 张图"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/select" -o /tmp/p3.html -w "HTTP %{http_code}\n"
grep -c 'name="selected"' /tmp/p3.html | xargs echo "复选框数量:"

# 取出 5 张有准用来源的图哈希（F 全撤回，排除）
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

say "4. 搜索最少馆组合"
ARGS=(); for h in "${PICKS[@]}"; do ARGS+=(--data-urlencode "selected=${h}"); done
LOC=$(curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "${ARGS[@]}" \
  "$BASE/solve" -o /dev/null -w "%{redirect_url}")
echo "跳转: $LOC"
TOKEN=${LOC##*/}
curl -sS -c "$COOKIE" -b "$COOKIE" "$LOC" -o /tmp/p4.html -w "HTTP %{http_code}\n"
grep -q "逐图署名表（草稿）" /tmp/p4.html && echo "草稿表渲染 OK"
python3 -s - <<'PY'
import re
html = open('/tmp/p4.html', encoding='utf-8').read()
m = re.search(r'使用馆\s*<strong>(\d+)</strong>\s*家', html)
print("求解馆数:", m.group(1))
tags = sorted(set(re.findall(r'<span class="mono">(L\d+)</span>', html)))
print("入选馆号(升序):", tags)
# E 仅 L05、C 仅 L03、D∈{L01,L04} 且 L04 未被其他图需要 → 最少 3 馆，字典序最小 [L01,L03,L05]
assert m.group(1) == "3", "A/B/C/D/E 的最少解应为 3 馆"
assert tags[:3] == ["L01", "L03", "L05"], tags
# A 在入选馆中仅 L01（L02 未入选），取最小馆号
assert "甲馆藏 / 王晋 摄" in html
print("最少馆数 + 字典序 + 每图最小馆号 规则验证 OK")
PY

say "5. 含缺准用图 F 时不出表"
FHASH=$(python3 -s -c "
import csv
rows=list(csv.DictReader(open('/workspace/sample-data/ledger_initial.csv',encoding='utf-8-sig')))
print(sorted({r['image_sha256'] for r in rows if r['status']=='revoked' and not any(x['image_sha256']==r['image_sha256'] and x['status']=='approved' for x in rows)})[0])")
curl -sS -c "$COOKIE" -b "$COOKIE" -X POST \
  --data-urlencode "selected=${PICKS[0]}" --data-urlencode "selected=$FHASH" \
  "$BASE/solve" -o /tmp/p5.html -w "HTTP %{http_code}\n"
grep -q "本次不出表" /tmp/p5.html && echo "缺准用页 OK；未生成确认表"

say "6. 搜索后补正台账（撤回 B/L02、改 E 署名、C 新增 L04 准用）"
curl -sS -c "$COOKIE" -b "$COOKIE" -F "ledger=@${FIX};type=text/csv" \
  "$BASE/ledger/upload" -o /tmp/p6.html -L -w "HTTP %{http_code}\n"
grep -q "台账已补正" /tmp/p6.html && echo "补正成功 OK"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/result/$TOKEN" -o /tmp/p6b.html
grep -q "台账已漂移" /tmp/p6b.html && echo "草稿页漂移提示 OK"

say "7. 确认应被拒绝、不保存，并给出重算表"
curl -sS -c "$COOKIE" -b "$COOKIE" -X POST \
  "$BASE/confirm/$TOKEN" -o /tmp/p7.html -w "HTTP %{http_code}\n"
grep -q "确认被拒绝" /tmp/p7.html && echo "拒绝保存 OK"
grep -q "请重算" /tmp/p7.html && echo "重算提示 OK"

say "8. 放弃漂移草稿，按新台账重新搜索并确认"
curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "$BASE/draft/$TOKEN/discard" -o /dev/null -w "放弃草稿 HTTP %{http_code}\n"
ARGS=(); for h in "${PICKS[@]}"; do ARGS+=(--data-urlencode "selected=${h}"); done
LOC2=$(curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "${ARGS[@]}" \
  "$BASE/solve" -o /dev/null -w "%{redirect_url}")
TOKEN2=${LOC2##*/}
curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "$BASE/confirm/$TOKEN2" -o /dev/null -w "确认 HTTP %{http_code} -> %{redirect_url}\n"

say "9. 查询已确认表：确认时台账未再变化，应全部一致并保留原署名"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/tables/1" -o /tmp/p9.html -w "HTTP %{http_code}\n"
grep -q "核对一致" /tmp/p9.html && echo "一致性提示 OK"
grep -q "戊馆藏 / 林澈、苏明 摄" /tmp/p9.html && echo "表内署名保留 OK"

say "9b. 确认后再补正：撤回 A/L01、变更 D/L01 署名（哈希按摘要动态查全量）"
python3 -s - <<'PY'
import csv
rows = list(csv.DictReader(open('/workspace/sample-data/ledger_initial.csv', encoding='utf-8-sig')))
hash_of = {}
for r in rows:
    hash_of.setdefault(r['summary'], r['image_sha256'])
h_a, h_d = hash_of['商代青铜鼎，正面全形'], hash_of['汉代彩绘陶俑']
assert len(h_a) == len(h_d) == 64
with open('/tmp/post_confirm.csv', 'w', encoding='utf-8', newline='') as f:
    w = csv.writer(f)
    w.writerow(['image_sha256', 'library_no', 'status', 'credit', 'summary'])
    w.writerow([h_a, 'L01', 'revoked', '甲馆藏（已撤回）', '商代青铜鼎，正面全形'])
    w.writerow([h_d, 'L01', 'approved', '甲馆藏 / 王晋（修订署名）', '汉代彩绘陶俑'])
print("post_confirm.csv 已生成，哈希均为 64 位")
PY
curl -sS -c "$COOKIE" -b "$COOKIE" -F "ledger=@/tmp/post_confirm.csv;type=text/csv" \
  "$BASE/ledger/upload" -o /dev/null -L -w "补正 HTTP %{http_code}\n"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/tables/1" -o /tmp/p9b.html -w "详情页 HTTP %{http_code}\n"
python3 -s - <<'PY'
import re
html = open('/tmp/p9b.html', encoding='utf-8').read()
badges = re.findall(r'class="badge (\w+)">([^<]+)</span>', html)
from collections import Counter
counts = Counter(label for _, label in badges)
print("行级徽标统计:", dict(counts))
assert counts.get("撤回准用") == 1, counts
assert counts.get("署名有变") == 1, counts
assert counts.get("一致") == 3, counts
assert "甲馆藏 / 王晋（修订署名）" in html, "现行署名对照缺失"
assert html.count("甲馆藏 / 王晋 摄") >= 1, "原署名未保留"
assert "该馆已无有效准用记录" in html
print("撤回/署名变更/原署名保留 行级标注全部 OK")
PY

say "10. 已确认表列表与 404"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/tables" -o /tmp/p10.html -w "列表 HTTP %{http_code}\n"
grep -q "#1" /tmp/p10.html && echo "列表含表 #1 OK"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/tables/999" -o /dev/null -w "不存在表 HTTP %{http_code}\n"

echo
echo "全部端到端检查通过。"
