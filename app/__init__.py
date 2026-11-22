"""Flask 应用：逐图署名复核与出表。"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .ledger import LedgerError, import_ledger
from .recheck import diff_results, recheck_confirmed
from .solver import MAX_IMAGES, SolveError, same_table, solve
from .storage import (
    all_images,
    claim_draft_for_confirm,
    delete_draft,
    fetch_sources,
    get_confirmed,
    get_draft,
    get_draft_any_state,
    immediate_session,
    list_confirmed,
    mark_draft_archived,
    prune_drafts,
    release_draft_claim,
    save_confirmed,
    save_draft,
    session,
    stats,
)


# 演示环境用东八区展示时间戳
CN_TZ = timezone(timedelta(hours=8))


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    app.config["DB_PATH"] = db_path

    @app.template_filter("as_dt")
    def as_dt(value: float) -> str:
        return datetime.fromtimestamp(float(value), tz=CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

    def db():
        return session(app.config.get("DB_PATH"))

    # ---------- 首页 / 台账总览 ----------
    @app.route("/")
    def index():
        with db() as conn:
            prune_drafts(conn)
            confirmed = [dict(r, libraries=json.loads(r["libraries"]))
                         for r in list_confirmed(conn)]
            return render_template(
                "index.html",
                stats=stats(conn),
                confirmed=confirmed,
            )

    # ---------- 上传台账 CSV（搜索组合期间随时可补正） ----------
    @app.route("/ledger/upload", methods=["POST"])
    def upload_ledger():
        file = request.files.get("ledger")
        if file is None or not file.filename:
            flash("请选择要上传的来源台账 CSV 文件", "error")
            return redirect(url_for("index"))
        try:
            raw = file.read()
            # 台账补正走立即写锁，与确认存档互斥：
            # 重算馆组合期间 CSV 补正仍可入库，但不会插进"复核→存档"的原子窗口。
            with immediate_session(app.config.get("DB_PATH")) as conn:
                report = import_ledger(conn, raw)
        except LedgerError as exc:
            flash(f"台账导入失败：{exc}", "error")
            return redirect(url_for("index"))
        except sqlite3.Error as exc:
            flash(f"台账写入失败，数据未入库，请重试：{exc}", "error")
            return redirect(request.referrer or url_for("index"))
        flash(
            f"台账已补正：{report['total']} 条（准用 {report['approved']}、撤回 {report['revoked']}），"
            "同一 (图像, 馆号) 的旧记录已被更正。",
            "success",
        )
        return redirect(request.referrer or url_for("index"))

    # ---------- 选图 ----------
    @app.route("/select")
    def select_images():
        with db() as conn:
            images = all_images(conn)
            sources = fetch_sources(conn, [r["image_sha256"] for r in images])
        # 附带每张图准用馆数，便于编目员判断
        enriched = []
        for row in images:
            approved_libs = sorted(
                {r["library_no"] for r in sources.get(row["image_sha256"], []) if r["status"] == "approved"}
            )
            enriched.append({**dict(row), "approved_libs": approved_libs})
        return render_template("select.html", images=enriched, max_images=MAX_IMAGES)

    # ---------- 求解 ----------
    @app.route("/solve", methods=["POST"])
    def do_solve():
        selected = request.form.getlist("selected")
        try:
            if len(selected) > MAX_IMAGES:
                raise SolveError(f"最多勾选 {MAX_IMAGES} 张图，本次选择了 {len(selected)} 张")
            if not selected:
                raise SolveError("未勾选任何图片")
            # 去重保序
            seen: set[str] = set()
            selected = [h for h in selected if not (h in seen or seen.add(h))]
            with db() as conn:
                sources = fetch_sources(conn, selected)
                result = solve(selected, sources)
                if not result["feasible"]:
                    missing_rows = fetch_sources(conn, result["missing"])
                    return render_template(
                        "missing.html",
                        missing=result["missing"],
                        missing_rows=missing_rows,
                    )
                token = secrets.token_urlsafe(12)
                save_draft(conn, token, selected, result)
            return redirect(url_for("show_result", token=token))
        except SolveError as exc:
            flash(str(exc), "error")
            return redirect(url_for("select_images"))

    # ---------- 草稿结果 ----------
    @app.route("/result/<token>")
    def show_result(token: str):
        with db() as conn:
            draft = get_draft(conn, token)
            if draft is None:
                # 已存档的草稿：直接转到对应署名表，而不是 404
                any_state = get_draft_any_state(conn, token)
                if any_state is not None and any_state["status"] == "archived":
                    return redirect(url_for("show_table", table_id=any_state["confirmed_id"]))
                abort(404)
            result = json.loads(draft["result_json"])
            hashes = json.loads(draft["hashes_json"])
            # 实时对照现行台账，提示是否已漂移（仅提示；最终以确认时锁内复核为准）
            sources = fetch_sources(conn, hashes)
            current = solve(hashes, sources)
            drift = None if same_table(result, current) else diff_results(result, current)
        return render_template(
            "result.html",
            token=token,
            result=result,
            drift=drift,
        )

    # ---------- 确认（现行台账须仍能得出同一份表；一份草稿最多存档一次） ----------
    @app.route("/confirm/<token>", methods=["POST"])
    def confirm(token: str):
        try:
            # BEGIN IMMEDIATE：整个"认领→复核→存档"持有写锁，
            # 台账补正与其它确认请求只能在锁外排队，杜绝双窗口重复存档与旧表入档。
            with immediate_session(app.config.get("DB_PATH")) as conn:
                draft_row = get_draft_any_state(conn, token)
                if draft_row is None:
                    abort(404)
                if draft_row["status"] == "archived":
                    # 另一个窗口已确认成功：直接导向已存档的同一份表，不再重复入档
                    table_id = draft_row["confirmed_id"]
                    flash("该草稿已在另一窗口确认存档，无需重复确认。", "success")
                    return redirect(url_for("show_table", table_id=table_id))
                if draft_row["status"] == "confirming":
                    # 自愈：上次确认进程中途崩溃才会残留 confirming
                    # （其事务已随进程死亡回滚，存档不可能已写入），转回 draft 重新认领
                    release_draft_claim(conn, token)
                # 原子认领：并发的第二个确认请求会在这里拿到 None
                draft = claim_draft_for_confirm(conn, token)
                if draft is None:
                    # 另一窗口正在确认中：不重复存档，提示稍后查看
                    flash("该草稿正在另一窗口确认中，请稍后在已确认表查询结果，请勿重复提交。", "error")
                    return redirect(url_for("show_result", token=token))

                snapshot = json.loads(draft["result_json"])
                hashes = json.loads(draft["hashes_json"])
                # 以存档生效时的现行台账为准重算（锁内无补正可插入）
                current = solve(hashes, fetch_sources(conn, hashes))
                if not same_table(snapshot, current):
                    # 结果有变：释放认领、保留草稿，提示重算
                    release_draft_claim(conn, token)
                    diffs = diff_results(snapshot, current)
                    return render_template(
                        "recompute.html",
                        token=token,
                        diffs=diffs,
                        current=current,
                    )
                # 复核一致才存档；写入失败会抛异常 → 事务回滚 → 认领释放、草稿原样保留
                table_id = save_confirmed(conn, snapshot)
                mark_draft_archived(conn, token, table_id)
        except sqlite3.Error as exc:
            # 写入失败：整个事务已回滚，草稿仍为 draft，明确告知可重试
            app.logger.exception("confirm failed for draft %s", token)
            flash(f"存档写入失败，草稿已保留，请稍后重试确认：{exc}", "error")
            return redirect(url_for("show_result", token=token))
        flash(f"署名表 #{table_id} 已确认保存（存档时现行台账复核一致）。", "success")
        return redirect(url_for("show_table", table_id=table_id))

    @app.route("/draft/<token>/discard", methods=["POST"])
    def discard_draft(token: str):
        with immediate_session(app.config.get("DB_PATH")) as conn:
            draft = get_draft_any_state(conn, token)
            if draft is None:
                abort(404)
            if draft["status"] == "archived":
                flash("该草稿已确认存档，不能放弃。", "error")
                return redirect(url_for("show_table", table_id=draft["confirmed_id"]))
            # 确认中的草稿由确认事务持有写锁，走到这里时必然已是 draft/archived
            delete_draft(conn, token)
        flash("草稿已放弃。", "success")
        return redirect(url_for("select_images"))

    # ---------- 已确认表列表 / 详情（保留原署名，标注变更） ----------
    @app.route("/tables")
    def tables_index():
        with db() as conn:
            rows = list_confirmed(conn)
        items = [
            {**dict(r), "libraries": json.loads(r["libraries"]), "payload": json.loads(r["payload_json"])}
            for r in rows
        ]
        return render_template("tables.html", tables=items)

    @app.route("/tables/<int:table_id>")
    def show_table(table_id: int):
        with db() as conn:
            row = get_confirmed(conn, table_id)
            if row is None:
                abort(404)
            payload = json.loads(row["payload_json"])
            sources = fetch_sources(conn, [e["image_sha256"] for e in payload["entries"]])
            report = recheck_confirmed(payload, sources)
        return render_template(
            "table_detail.html",
            table_id=table_id,
            payload=payload,
            report=report,
            confirmed_at=row["confirmed_at"],
        )

    @app.errorhandler(413)
    def too_large(_e):
        flash("上传文件超过 16MB 上限。", "error")
        return redirect(url_for("index"))

    return app
