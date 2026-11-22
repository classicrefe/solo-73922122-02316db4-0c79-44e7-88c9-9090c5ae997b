"""Flask 应用：逐图署名复核与出表。"""
from __future__ import annotations

import json
import os
import secrets
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
    delete_draft,
    fetch_sources,
    get_confirmed,
    get_draft,
    list_confirmed,
    prune_drafts,
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
            with db() as conn:
                report = import_ledger(conn, raw)
        except LedgerError as exc:
            flash(f"台账导入失败：{exc}", "error")
            return redirect(url_for("index"))
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
                abort(404)
            result = json.loads(draft["result_json"])
            hashes = json.loads(draft["hashes_json"])
            # 实时对照现行台账，提示是否已漂移
            sources = fetch_sources(conn, hashes)
            current = solve(hashes, sources)
            drift = None if same_table(result, current) else diff_results(result, current)
        return render_template(
            "result.html",
            token=token,
            result=result,
            drift=drift,
        )

    # ---------- 确认（现行台账须仍能得出同一份表） ----------
    @app.route("/confirm/<token>", methods=["POST"])
    def confirm(token: str):
        with db() as conn:
            draft = get_draft(conn, token)
            if draft is None:
                abort(404)
            snapshot = json.loads(draft["result_json"])
            hashes = json.loads(draft["hashes_json"])
            current = solve(hashes, fetch_sources(conn, hashes))
            if not same_table(snapshot, current):
                diffs = diff_results(snapshot, current)
                return render_template(
                    "recompute.html",
                    token=token,
                    diffs=diffs,
                    current=current,
                )
            table_id = save_confirmed(conn, snapshot)
            delete_draft(conn, token)
        flash(f"署名表 #{table_id} 已确认保存（现行台账复核一致）。", "success")
        return redirect(url_for("show_table", table_id=table_id))

    @app.route("/draft/<token>/discard", methods=["POST"])
    def discard_draft(token: str):
        with db() as conn:
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
