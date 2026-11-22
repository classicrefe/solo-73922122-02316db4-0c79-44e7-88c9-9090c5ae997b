"""端到端自测：求解规则、CSV 导入、Web 流程、确认漂移、存档核对。

运行（从仓库根目录）：
    PYTHONPATH=/tmp/pylibs/lib/python3.11/site-packages python3 -I -m unittest -v tests.test_app
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.ledger import LedgerError, import_ledger, parse_ledger  # noqa: E402
from app.solver import MAX_LIBRARIES, SolveError, same_table, solve  # noqa: E402
from app.storage import connect, fetch_sources, init_db  # noqa: E402

CSV_HEADER = "image_sha256,library_no,status,credit,summary\n"


def csv_bytes(rows: list[tuple]) -> bytes:
    out = CSV_HEADER
    for r in rows:
        out += ",".join(f'"{v}"' for v in r) + "\n"
    return out.encode("utf-8")


def row(lib, status, credit="署名-" + "x", summary="摘要-x"):
    return lib, status, credit, summary


class SolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = connect(self.tmp.name)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _load(self, rows):
        import_ledger(self.conn, csv_bytes(rows))
        hashes = sorted({r[0] for r in rows})
        return fetch_sources(self.conn, hashes)

    def test_minimum_libraries_and_lexicographic_tie(self):
        # A: L01/L02 可；B: L02/L03 可；C: L03 可
        # 最少 2 馆；并列 [L01,L03] 与 [L02,L03]，字典序取 [L01,L03]
        rows = [
            ("A", *row("L01", "approved", "cA1")),
            ("A", *row("L02", "approved", "cA2")),
            ("B", *row("L02", "approved", "cB2")),
            ("B", *row("L03", "approved", "cB3")),
            ("C", *row("L03", "approved", "cC3")),
        ]
        sources = self._load(rows)
        result = solve(["A", "B", "C"], sources)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["libraries"], ["L01", "L03"])
        self.assertEqual(result["library_count"], 2)
        # A 在入选馆中有 L01（与 L02 二选一，取最小馆号）
        entry_a = next(e for e in result["entries"] if e["image_sha256"] == "A")
        self.assertEqual(entry_a["library_no"], "L01")
        self.assertEqual(entry_a["credit"], "cA1")
        entry_b = next(e for e in result["entries"] if e["image_sha256"] == "B")
        self.assertEqual(entry_b["library_no"], "L03")

    def test_single_library_solution_picked_lexicographically(self):
        # A: L02/L03；B: L02 → 单馆 L02 即可
        rows = [
            ("A", *row("L02", "approved")),
            ("A", *row("L03", "approved")),
            ("B", *row("L02", "approved")),
        ]
        result = solve(["A", "B"], self._load(rows))
        self.assertEqual(result["libraries"], ["L02"])

    def test_missing_approved_source_blocks_table(self):
        rows = [
            ("A", *row("L01", "approved")),
            ("F", *row("L02", "revoked")),  # 仅撤回
        ]
        result = solve(["A", "F"], self._load(rows))
        self.assertFalse(result["feasible"])
        self.assertEqual(result["missing"], ["F"])
        self.assertEqual(result["entries"], [])

    def test_unknown_hash_treated_as_missing(self):
        rows = [("A", *row("L01", "approved"))]
        result = solve(["A", "GHOST"], self._load(rows))
        self.assertFalse(result["feasible"])
        self.assertEqual(result["missing"], ["GHOST"])

    def test_over_16_libraries_rejected(self):
        rows = []
        for i in range(MAX_LIBRARIES + 1):
            rows.append(("A", *row(f"L{i:02d}", "approved")))
        with self.assertRaises(SolveError):
            solve(["A"], self._load(rows))

    def test_over_100_images_rejected(self):
        with self.assertRaises(SolveError):
            solve([f"img{i}" for i in range(101)], {})

    def test_numeric_library_ordering_is_string_order(self):
        # 馆号是字符串：L10 字典序在 L2 之前
        rows = [
            ("A", *row("L2", "approved")),
            ("A", *row("L10", "approved")),
            ("B", *row("L2", "approved")),
        ]
        result = solve(["A", "B"], self._load(rows))
        self.assertEqual(result["libraries"], ["L2"])
        rows2 = [
            ("A", *row("L10", "approved")),
            ("B", *row("L2", "approved")),
            ("B", *row("L10", "approved")),
        ]
        result2 = solve(["A", "B"], self._load(rows2))
        self.assertEqual(result2["libraries"], ["L10"])  # 两个单馆解，字典序 L10 < L2

    def test_same_table_credit_change_detected(self):
        rows = [("A", *row("L01", "approved", "旧署名"))]
        sources = self._load(rows)
        r1 = solve(["A"], sources)
        import_ledger(self.conn, csv_bytes([("A", "L01", "approved", "新署名", "摘要-x")]))
        r2 = solve(["A"], fetch_sources(self.conn, ["A"]))
        self.assertFalse(same_table(r1, r2))


class LedgerCsvTests(unittest.TestCase):
    def test_chinese_headers_and_status_aliases(self):
        raw = "图像sha256,馆号,准用状态,署名,摘要\nH1,G1,准用,署名甲,摘要一\nH2,G2,撤回,署名乙,摘要二\n".encode()
        records = parse_ledger(raw)
        self.assertEqual(records[0]["status"], "approved")
        self.assertEqual(records[1]["status"], "revoked")

    def test_missing_column_raises(self):
        with self.assertRaises(LedgerError):
            parse_ledger(b"image_sha256,library_no,status\nH1,G1,approved\n")

    def test_bad_status_raises_with_line_number(self):
        with self.assertRaises(LedgerError) as ctx:
            parse_ledger(CSV_HEADER.encode() + b'H1,G1,pending,c,s\n')
        self.assertIn("第 2 行", str(ctx.exception))

    def test_duplicate_key_in_file_raises(self):
        raw = CSV_HEADER.encode() + b'H1,G1,approved,c,s\nH1,G1,revoked,c2,s2\n'
        with self.assertRaises(LedgerError):
            parse_ledger(raw)

    def test_upsert_corrects_existing_record(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            conn = connect(tmp.name)
            init_db(conn)
            import_ledger(conn, csv_bytes([("H1", "G1", "approved", "旧", "摘要1")]))
            import_ledger(conn, csv_bytes([("H1", "G1", "revoked", "新", "摘要2")]))
            rows = fetch_sources(conn, ["H1"])["H1"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "revoked")
            self.assertEqual(rows[0]["credit"], "新")
            conn.close()
        finally:
            os.unlink(tmp.name)


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.app = create_app(db_path=self.tmp.name)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        self.rows = [
            ("imgA", "L01", "approved", "甲馆-甲署名", "青铜鼎"),
            ("imgA", "L02", "approved", "乙馆-乙署名", "青铜鼎"),
            ("imgB", "L02", "approved", "乙馆-乙署名", "壁画"),
            ("imgB", "L03", "approved", "丙馆-丙署名", "壁画"),
            ("imgC", "L03", "approved", "丙馆-丙署名", "经卷"),
            ("imgC", "L04", "revoked", "丁馆-撤回", "经卷"),
            ("imgD", "L05", "approved", "戊馆-戊署名", "金冠"),
            ("imgF", "L02", "revoked", "乙馆-撤回", "漆器"),
        ]
        self._upload(self.rows)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _upload(self, rows):
        data = {"ledger": (io.BytesIO(csv_bytes(rows)), "ledger.csv")}
        return self.client.post("/ledger/upload", data=data, content_type="multipart/form-data",
                                follow_redirects=True)

    def _solve(self, selected):
        from werkzeug.datastructures import MultiDict
        return self.client.post(
            "/solve",
            data=MultiDict([("selected", h) for h in selected]),
            follow_redirects=False,
        )

    def test_index_and_select_pages_render(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        page = self.client.get("/select")
        self.assertEqual(page.status_code, 200)
        self.assertIn("imgA".encode(), page.data)

    def test_full_flow_confirm_and_query_clean(self):
        resp = self._solve(["imgA", "imgB", "imgC"])
        self.assertEqual(resp.status_code, 302)
        token = resp.headers["Location"].rsplit("/", 1)[-1]
        page = self.client.get(f"/result/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("青铜鼎".encode(), page.data)
        # 最少 2 馆，字典序 [L01,L03]
        self.assertIn("L01".encode(), page.data)
        self.assertIn("L03".encode(), page.data)

        resp = self.client.post(f"/confirm/{token}", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/tables/1", resp.headers["Location"])

        detail = self.client.get("/tables/1")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("核对一致".encode(), detail.data)
        # 原署名保留
        self.assertIn("甲馆-甲署名".encode(), detail.data)
        # 草稿已删除
        self.assertEqual(self.client.get(f"/result/{token}").status_code, 404)

    def test_missing_source_shows_images_and_no_table(self):
        resp = self._solve(["imgA", "imgF"])
        self.assertEqual(resp.status_code, 200)  # 直接渲染 missing 页，不跳转
        self.assertIn("本次不出表".encode(), resp.data)
        self.assertIn("imgF".encode(), resp.data)
        # 没有产生任何已确认表
        self.assertNotIn(b"/tables/1", self.client.get("/").data)

    def test_drift_blocks_confirm_and_recompute_page(self):
        resp = self._solve(["imgA", "imgB", "imgC"])
        token = resp.headers["Location"].rsplit("/", 1)[-1]
        # 搜索后补正：撤回 imgB/L02，并改 imgC 在 L03 的署名
        self._upload([
            ("imgB", "L02", "revoked", "乙馆-撤回", "壁画"),
            ("imgC", "L03", "approved", "丙馆-署名已更正", "经卷"),
        ])
        # 草稿页应提示漂移且确认按钮禁用
        page = self.client.get(f"/result/{token}")
        self.assertIn("台账已漂移".encode(), page.data)
        # 确认被拒绝，进入重算页，不保存
        rejected = self.client.post(f"/confirm/{token}", follow_redirects=False)
        self.assertEqual(rejected.status_code, 200)
        self.assertIn("确认被拒绝".encode(), rejected.data)
        self.assertIn("本次".encode(), rejected.data)
        self.assertIn("不保存".encode(), rejected.data)
        # 确认表仍为空
        self.assertIn("尚无已确认署名表".encode(), self.client.get("/tables").data)
        # 草稿仍在，可放弃
        self.assertEqual(self.client.get(f"/result/{token}").status_code, 200)

    def test_recheck_marks_withdrawn_and_credit_changed(self):
        # 先确认一版
        resp = self._solve(["imgD"])
        token = resp.headers["Location"].rsplit("/", 1)[-1]
        self.client.post(f"/confirm/{token}")
        # 台账补正：撤回 D/L05，再补一条；另改 A 的署名（不在表内，不应被标注）
        self._upload([
            ("imgD", "L05", "revoked", "戊馆-撤回", "金冠"),
            ("imgA", "L01", "approved", "甲馆-署名更新", "青铜鼎"),
        ])
        page = self.client.get("/tables/1").data
        self.assertIn("撤回准用".encode(), page)
        self.assertIn("张撤回准用".encode(), page)
        self.assertIn(b"<strong>1</strong>", page)
        # 原署名仍保留在页面
        self.assertIn("戊馆-戊署名".encode(), page)

        # 再确认第二版（A），然后改 A/L01 署名，验证“署名有变”
        resp2 = self._solve(["imgA"])
        token2 = resp2.headers["Location"].rsplit("/", 1)[-1]
        self.client.post(f"/confirm/{token2}")
        self._upload([("imgA", "L01", "approved", "甲馆-又改了", "青铜鼎")])
        page2 = self.client.get("/tables/2").data
        self.assertIn("署名有变".encode(), page2)
        self.assertIn("甲馆-又改了".encode(), page2)       # 现行署名对照
        self.assertIn("甲馆-署名更新".encode(), page2)     # 原署名保留
        self.assertNotIn("核对一致".encode(), page2)

    def test_upload_bad_csv_flashes_error(self):
        data = {"ledger": (io.BytesIO(b"a,b,c\n1,2,3\n"), "bad.csv")}
        page = self.client.post("/ledger/upload", data=data,
                                content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("台账导入失败".encode(), page.data)

    def test_no_selection_flashes_error(self):
        page = self.client.post("/solve", data={}, follow_redirects=True)
        self.assertIn("未勾选".encode(), page.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
