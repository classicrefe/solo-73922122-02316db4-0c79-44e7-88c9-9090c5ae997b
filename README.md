# 展览图录 · 逐图署名复核系统

供图馆复核展览图录逐图署名的 Flask 应用：上传来源台账 CSV，按 **图像 SHA-256 + 馆号**
在 SQLite 中增补/更正准用状态与署名；编目员勾选图片后，系统搜索**使用馆数最少**的
署名组合并出表；确认时强制复核现行台账；已确认表支持存档查询并标注后续台账变化。

## 业务规则实现

1. **台账增补/更正**：CSV 按 `(image_sha256, library_no)` 唯一键 upsert（`app/ledger.py`、
   `app/storage.py`）。支持中英文表头与 `approved/revoked`、`准用/撤回` 状态别名；
   文件内重复键、缺列、非法状态、非 UTF-8 等均带行号报错，不入库。
2. **选图上限**：至多 100 张图（前端勾选限制 + 后端 `solver.MAX_IMAGES` 双重校验）。
3. **求解规则**（`app/solver.py`）：
   - 每张图仅能选一家 `approved` 馆；存在无准用来源的图时，列出这些图片，**本次不出表**；
   - 目标为整册使用馆数最少；馆数并列时，取**馆号升序序列字典序最小**的组合
     （按馆数 1..n 枚举 `combinations`，首个可行解即字典序最小）；
   - 一张图在入选馆集合中有多家准用时，取**最小馆号**及其署名；
   - 涉及的准用馆总数超过 16 家直接拒绝求解（枚举安全边界）。
4. **搜索期间可继续补正台账**：草稿以一次性 token 存 SQLite（24 小时自动清理），
   草稿页实时对照现行台账，发现漂移即展示逐图差异，并可在草稿页/重算页就地上传补正。
5. **确认强制复核（已修复并发安全）**（`app/__init__.py` 的 `/confirm`、`app/storage.py`）：
   - 确认事务以 `BEGIN IMMEDIATE` 立即获取写锁，整个"认领草稿 → 按现行台账重算 → 存档"
     在一个事务内原子完成；台账 CSV 补正同样走写锁事务，**重算馆组合期间补正仍可入库**，
     但无法插入"复核→存档"的窗口，杜绝旧表入档；
   - 草稿引入状态机 `draft → confirming → archived`，确认时用条件 UPDATE 原子认领，
     **一份草稿最多存档一次**：两个窗口同时/先后确认同一草稿，只有一份署名表入库，
     后续请求直接导向已存档的同一份表（并发压力测试 8 线程 × 5 轮验证通过）；
   - 仍以存档生效时的现行台账为准，沿用图序、馆号、署名逐图比对（`same_table`）：
     补正后仍能得出同一份表，草稿即可确认；结果有变则拒绝保存、进入重算页提示重算，
     **草稿保留为 draft 可重试**；
   - 存档写入失败（如磁盘错误）时整个事务回滚，草稿原样保留并提示重试；
     进程崩溃残留的 `confirming` 草稿会在下次确认时自愈回 `draft`；
   - 已存档草稿行保留备查（记录对应表号），不再物理删除，旧草稿链接自动跳转到署名表。
6. **已确认表查询**（`app/recheck.py`）：存档内容永不改写，保留确认时的原馆号、原署名；
   仅逐图标注 `撤回准用`（原馆号已无有效准用记录）与 `署名有变`（同馆同图署名已改，
   备注列显示现行署名）。

## 目录结构

```
app/
  __init__.py      Flask 路由与页面装配
  storage.py       SQLite 连接、schema、upsert、草稿/确认表存取
  ledger.py        台账 CSV 解析、校验与导入
  solver.py        最少馆组合求解（字典序 tie-break、每图最小馆号）
  recheck.py       确认前漂移比对、确认后存档复核
  templates/       7 个 Jinja2 页面（中文界面）
wsgi.py            gunicorn 入口
tests/test_app.py  29 个单元/集成测试（unittest，含并发与故障恢复场景）
tests/e2e_demo.sh  真实 HTTP 端到端演示脚本（确认/冲突/恢复/重复确认）
sample-data/       演示台账生成脚本与两份 CSV
Dockerfile, docker-compose.yml, requirements.txt
```

## Docker Compose 启动

```bash
docker compose up --build
# 浏览器访问 http://localhost:8000
```

SQLite 数据库存放在命名卷 `credit-data`（容器内 `/data/credit.db`），重建容器不丢数据。
生产环境请通过环境变量覆盖 `FLASK_SECRET_KEY`。

## 本地（无 Docker）运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python wsgi.py        # 或 gunicorn -b 0.0.0.0:8000 wsgi:app
```

## 演示流程

```bash
python3 sample-data/generate_samples.py     # 生成 ledger_initial.csv / ledger_correction.csv
```

1. 首页上传 `sample-data/ledger_initial.csv`（6 张图、12 条来源，含撤回记录）；
2. “选图出表”勾选前 5 张图（第 6 张全部撤回），得到 3 馆解 `[L01, L03, L05]`，进入草稿页；
3. 若同时勾选全部撤回的图，页面列出缺准用图片且不出表；
4. 上传 `ledger_correction.csv` 补正台账：草稿页显示漂移与就地补正入口，点确认被拒绝并
   进入重算页（**不保存、草稿保留**）；
5. 按重算页提示继续补正（脚本 `tests/e2e_demo.sh` 会把补正涉及的键恢复为初始值），
   现行台账重新得出同一份表后，**同一张草稿直接确认成功**，无需重新搜索；
6. 对同一草稿再次/并发点确认：不会产生第二份表，统一跳转到已存档的署名表；
7. 确认后再改台账，在“已确认表查询”中可见 `撤回准用` / `署名有变` 行级标注，原署名保留。

## 自测

```bash
# 单元 + 集成（29 项，含并发双确认、漂移恢复、写入失败保留草稿、旧库迁移、崩溃自愈）
PYTHONPATH=. python3 -m unittest -v tests.test_app

# 端到端 HTTP 演示（需先在 $PORT 启动服务）
PORT=8000 BASE=http://127.0.0.1:8000 bash tests/e2e_demo.sh
```

> 受限环境若系统 Python 未装依赖，可把依赖装到独立目录后用：
> `PYTHONPATH=/tmp/pylibs:/workspace python3 -P -m unittest tests.test_app`
> （`-P` 不把当前目录隐式塞进 `sys.path`，但保留显式 `PYTHONPATH`）。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CREDIT_DB_PATH` | `./data/credit.db` | SQLite 文件路径（容器内为 `/data/credit.db`） |
| `FLASK_SECRET_KEY` | 开发占位值 | 会话/flash 签名密钥，生产必须覆盖 |
