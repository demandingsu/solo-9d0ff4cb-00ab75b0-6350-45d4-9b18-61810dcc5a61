"""跨界复核的 Flask 接口测试：上传核对、快照不可变、错误提示、不改动现行数据。"""

import io

import pytest

from app.app import create_app
from app.db import get_db, load_accum, load_edges
from app.flow import (
    compute_accumulation,
    dump_flow_csv,
    parse_flow_csv,
)

from tests.test_check import new_snake_edges, old_stripe_edges


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "check.db"))
    app.config.update(TESTING=True)
    return app.test_client()


def _upload(client, edges, name="new.csv"):
    return client.post(
        "/api/check",
        data={"file": (io.BytesIO(dump_flow_csv(edges).encode()), name)},
        content_type="multipart/form-data",
    )


def _import(client, edges):
    return client.post(
        "/api/import",
        data={"file": (io.BytesIO(dump_flow_csv(edges).encode()), "old.csv")},
        content_type="multipart/form-data",
    )


def test_check_returns_groups_and_keeps_current_graph_untouched(client):
    old = old_stripe_edges()
    new = new_snake_edges(old)
    assert _import(client, old).status_code == 200

    before_flow = client.get("/download/flow.csv").data.decode()
    before_accum = client.get("/download/accumulation.csv").data.decode()

    resp = _upload(client, new)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["cell_count"] == 15
    assert body["group_count"] == 2
    assert body["flagged_cell_count"] == 3
    assert body["outlet_changed_count"] == 4
    assert body["report_id"] >= 1

    # 现行流向表与汇水表逐字节未变
    assert client.get("/download/flow.csv").data.decode() == before_flow
    assert client.get("/download/accumulation.csv").data.decode() == before_accum


def test_report_is_immutable_snapshot_when_current_graph_is_corrected(client):
    old = old_stripe_edges()
    new = new_snake_edges(old)
    _import(client, old)
    report_id = _upload(client, new).get_json()["report_id"]

    # 事后更正现行图（新边 (4,0)->(4,1) 与新图里的 (4,1)->(4,0) 无关，
    # 现行图中 (4,1)->(4,0) 仍存在会成环，因此同时把 (4,1) 改指 (3,1)）
    r = client.post(
        "/api/correct",
        json={
            "changes": [
                {"row": 4, "col": 0, "down_row": 4, "down_col": 1},
                {"row": 4, "col": 1, "down_row": 3, "down_col": 1},
            ]
        },
    )
    assert r.status_code == 200

    detail = client.get(f"/api/reports/{report_id}").get_json()
    assert detail["ok"] is True
    # 报告内现行图快照仍是核对当时的版本（(4,0) 当时指向 (3,0)）
    snap_old = {
        (c["row"], c["col"]): (
            None if c["down_row"] is None else (c["down_row"], c["down_col"])
        )
        for c in detail["old_edges"]
    }
    assert snap_old[(4, 0)] == (3, 0)
    assert snap_old[(4, 1)] == (4, 0)  # 快照里也是旧边
    snap_new = {
        (c["row"], c["col"]): (
            None if c["down_row"] is None else (c["down_row"], c["down_col"])
        )
        for c in detail["new_edges"]
    }
    assert snap_new == new
    # 分组明细与刚核对时一致
    assert detail["result"]["group_count"] == 2
    assert detail["result"]["flagged_cell_count"] == 3

    # 快照可分别下载，且内容即核对当时的两图
    old_csv = client.get(f"/api/reports/{report_id}/snapshot/old").data.decode()
    new_csv = client.get(f"/api/reports/{report_id}/snapshot/new").data.decode()
    assert parse_flow_csv(old_csv) == old
    assert parse_flow_csv(new_csv) == new


def test_check_rejects_differing_cell_sets_with_reason(client):
    old = old_stripe_edges()
    _import(client, old)

    missing = {cell: d for cell, d in new_snake_edges(old).items() if cell != (4, 2)}
    resp = _upload(client, missing)
    assert resp.status_code == 400
    msg = resp.get_json()["error"]
    assert "格点集合不一致" in msg and "(4,2)" in msg

    extra = dict(new_snake_edges(old))
    extra[(9, 9)] = None
    resp = _upload(client, extra)
    assert resp.status_code == 400
    assert "(9,9)" in resp.get_json()["error"]


def test_check_rejects_illegal_upload_and_changes_nothing(client, tmp_path):
    old = old_stripe_edges()
    _import(client, old)

    # 成环上传
    cyclic = new_snake_edges(old)
    cyclic[(4, 0)] = (4, 1)  # (4,1)->(4,0) 已存在，构成 2 环
    resp = _upload(client, cyclic)
    assert resp.status_code == 400 and "环" in resp.get_json()["error"]

    # 下游不存在
    broken = dict(old)
    broken[(0, 1)] = (7, 7)
    resp = _upload(client, broken)
    assert resp.status_code == 400 and "不在" in resp.get_json()["error"]

    # 非四邻域
    far = dict(old)
    far[(0, 1)] = (2, 1)
    resp = _upload(client, far)
    assert resp.status_code == 400 and "相邻" in resp.get_json()["error"]

    # 坏 CSV（表头错误）
    resp = client.post(
        "/api/check",
        data={"file": (io.BytesIO("a,b,c,d\n1,2,3,4".encode()), "bad.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400 and "表头" in resp.get_json()["error"]

    # 没有产生任何报告，现行数据完好
    assert client.get("/api/reports").get_json()["reports"] == []
    conn = get_db(_db_path(client))
    assert load_edges(conn) == old
    assert load_accum(conn) == compute_accumulation(old)
    conn.close()


def test_check_without_current_data_returns_400(client):
    resp = _upload(client, old_stripe_edges())
    assert resp.status_code == 400
    assert "尚无现行流向数据" in resp.get_json()["error"]


def test_report_listing_detail_and_404(client):
    old = old_stripe_edges()
    _import(client, old)
    rid1 = _upload(client, new_snake_edges(old)).get_json()["report_id"]
    rid2 = _upload(client, dict(old)).get_json()["report_id"]  # 完全相同 -> 无命中

    listing = client.get("/api/reports").get_json()["reports"]
    assert [r["id"] for r in listing] == [rid2, rid1]
    assert listing[0]["group_count"] == 0
    assert listing[1]["flagged_cell_count"] == 3

    assert client.get("/api/reports/9999").status_code == 404
    assert client.get(f"/api/reports/{rid1}/snapshot/other").status_code == 404
    assert client.get(f"/api/reports/{rid1}/snapshot/old").status_code == 200


def test_check_missing_file_field(client):
    _import(client, old_stripe_edges())
    resp = client.post("/api/check", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400 and "file" in resp.get_json()["error"]


def _db_path(c):
    return c.application.config["DB_PATH"]
