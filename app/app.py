"""Flask 网页：导入流向 CSV、批量更正、下载流向表与汇水表。"""

from __future__ import annotations

import io
import json
import os
from typing import Optional

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
)

from .db import (
    apply_batch,
    get_db,
    has_data,
    import_flow,
    list_check_reports,
    list_corrections,
    load_accum,
    load_check_report,
    load_edges,
    save_check_report,
)
from .flow import (
    Cell,
    FlowError,
    compare_catchments,
    dump_flow_csv,
    parse_flow_csv,
    validate_edges,
)


def create_app(db_path: Optional[str] = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get(
        "FLOW_DB_PATH", os.path.join(app.instance_path, "flow.db")
    )
    # 网格表格最多展示这么多格，避免超大图把页面撑爆；下载不受限制
    app.config["MAX_GRID_DISPLAY"] = int(os.environ.get("MAX_GRID_DISPLAY", "400"))

    def db():
        return get_db(app.config["DB_PATH"])

    # ------------------------------------------------------------------ #
    def parse_changes(payload) -> dict[Cell, Optional[Cell]]:
        """解析更正请求，支持 JSON 数组或 {"changes": [...]} 两种形态。"""
        if isinstance(payload, dict) and "changes" in payload:
            items = payload["changes"]
        else:
            items = payload
        if not isinstance(items, list) or not items:
            raise FlowError("请求体必须是非空的更正数组 changes")

        changes: dict[Cell, Optional[Cell]] = {}
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise FlowError(f"第 {idx + 1} 条更正不是 JSON 对象")
            try:
                row, col = int(item["row"]), int(item["col"])
            except (KeyError, TypeError, ValueError):
                raise FlowError(f"第 {idx + 1} 条更正缺少整数类型的 row/col")
            cell = (row, col)
            if cell in changes:
                raise FlowError(f"格点 {cell} 在同一批更正中出现多次")

            dr = item.get("down_row")
            dc = item.get("down_col")
            # None 或空字符串都表示出口；只填一个则报错
            if dr is None and dc is None:
                downstream: Optional[Cell] = None
            elif dr in ("", None) or dc in ("", None):
                raise FlowError(
                    f"格点 {cell} 的下游行列号必须同时填写或同时留空（出口留空）"
                )
            else:
                try:
                    downstream = (int(dr), int(dc))
                except (TypeError, ValueError):
                    raise FlowError(f"格点 {cell} 的下游行列号必须是整数")
            changes[cell] = downstream
        return changes

    def snapshot(conn) -> dict:
        edges = load_edges(conn)
        accum = load_accum(conn)
        limit = app.config["MAX_GRID_DISPLAY"]
        truncated = len(edges) > limit
        cells = [
            {
                "row": cell[0],
                "col": cell[1],
                "down_row": None if edges[cell] is None else edges[cell][0],
                "down_col": None if edges[cell] is None else edges[cell][1],
                "accum": accum[cell],
            }
            for cell in sorted(edges)[:limit]
        ]
        return {
            "cell_count": len(edges),
            "truncated": truncated,
            "cells": cells,
        }

    # ------------------------------------------------------------------ #
    @app.errorhandler(FlowError)
    def handle_flow_error(exc: FlowError):
        return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def api_state():
        conn = db()
        try:
            data = snapshot(conn)
            data["ok"] = True
            data["has_data"] = has_data(conn)
            data["corrections"] = [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "changed": row["changed"],
                    "affected": row["affected"],
                    "created_at": row["created_at"],
                }
                for row in list_corrections(conn)
            ]
            return jsonify(data)
        finally:
            conn.close()

    @app.post("/api/import")
    def api_import():
        upload = request.files.get("file")
        if upload is None:
            raise FlowError("缺少上传文件字段 file（CSV）")
        raw = upload.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise FlowError("CSV 必须是 UTF-8 编码")
        edges = parse_flow_csv(text)
        if not edges:
            raise FlowError("CSV 中没有任何格点记录")
        conn = db()
        try:
            accum = import_flow(conn, edges)
            return jsonify(
                {
                    "ok": True,
                    "message": f"导入成功：{len(edges)} 个格点，汇水表已全量重算",
                    "cell_count": len(edges),
                    "outlet_count": sum(1 for d in edges.values() if d is None),
                }
            )
        finally:
            conn.close()

    @app.post("/api/correct")
    def api_correct():
        payload = request.get_json(silent=True)
        if payload is None:
            raise FlowError("请求体必须是 application/json")
        changes = parse_changes(payload)
        conn = db()
        try:
            new_edges, new_accum, affected = apply_batch(conn, changes)
            return jsonify(
                {
                    "ok": True,
                    "message": (
                        f"批次生效：更正 {len(changes)} 格，"
                        f"增量重算 {len(affected)} 格（含更正格与新旧下游链）"
                    ),
                    "changed_count": len(changes),
                    "affected_count": len(affected),
                    "affected": [list(c) for c in sorted(affected)],
                    "new_values": [
                        {
                            "row": cell[0],
                            "col": cell[1],
                            "down_row": (
                                None if new_edges[cell] is None else new_edges[cell][0]
                            ),
                            "down_col": (
                                None if new_edges[cell] is None else new_edges[cell][1]
                            ),
                            "accum": new_accum[cell],
                        }
                        for cell in sorted(affected)
                    ],
                }
            )
        finally:
            conn.close()

    @app.get("/download/flow.csv")
    def download_flow():
        conn = db()
        try:
            if not has_data(conn):
                abort(409, description="尚无流向数据，请先导入 CSV")
            text = dump_flow_csv(load_edges(conn))
            return Response(
                text,
                mimetype="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": "attachment; filename=flow_direction.csv"
                },
            )
        finally:
            conn.close()

    @app.get("/download/accumulation.csv")
    def download_accumulation():
        conn = db()
        try:
            if not has_data(conn):
                abort(409, description="尚无汇水数据，请先导入 CSV")
            accum = load_accum(conn)
            buf = io.StringIO()
            buf.write("row,col,accumulation\n")
            for cell in sorted(accum):
                buf.write(f"{cell[0]},{cell[1]},{accum[cell]}\n")
            return Response(
                buf.getvalue(),
                mimetype="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        "attachment; filename=accumulation.csv"
                    )
                },
            )
        finally:
            conn.close()

    @app.get("/api/sample")
    def api_sample():
        """内置 4x4 示例流向 CSV，方便网页一键填充演示。"""
        from .flow import dump_flow_csv

        edges = sample_edges()
        return Response(
            dump_flow_csv(edges),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=sample_flow.csv"},
        )

    @app.errorhandler(409)
    def handle_409(exc):
        return jsonify({"ok": False, "error": exc.description}), 409

    @app.errorhandler(404)
    def handle_404(exc):
        return jsonify({"ok": False, "error": exc.description}), 404

    # ---------------------------------------------------------------- #
    # 跨界复核：上传图与现行图核对，结果以不可变快照报告保存
    # ---------------------------------------------------------------- #
    @app.post("/api/check")
    def api_check():
        upload = request.files.get("file")
        if upload is None:
            raise FlowError("缺少上传文件字段 file（CSV）")
        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise FlowError("CSV 必须是 UTF-8 编码")
        new_edges = parse_flow_csv(text)
        if not new_edges:
            raise FlowError("CSV 中没有任何格点记录")
        # 上传图自身必须合法（下游存在、四邻域、无环）
        validate_edges(new_edges)

        conn = db()
        try:
            if not has_data(conn):
                raise FlowError("尚无现行流向数据，请先导入一份流向 CSV 再核对")
            old_edges = load_edges(conn)  # 现行图由导入/更正事务保证合法

            old_cells, new_cells = set(old_edges), set(new_edges)
            if old_cells != new_cells:
                missing = sorted(new_cells - old_cells)
                extra = sorted(old_cells - new_cells)
                parts = [
                    "两图格点集合不一致，无法核对："
                    f"现行图 {len(old_cells)} 格，上传图 {len(new_edges)} 格"
                ]
                if missing:
                    parts.append(
                        "上传图多出的格点："
                        + "、".join(f"({r},{c})" for r, c in missing[:10])
                        + ("…" if len(missing) > 10 else "")
                    )
                if extra:
                    parts.append(
                        "上传图缺少的格点："
                        + "、".join(f"({r},{c})" for r, c in extra[:10])
                        + ("…" if len(extra) > 10 else "")
                    )
                raise FlowError("；".join(parts))

            result = compare_catchments(old_edges, new_edges)
            # 只读了 cells 表；保存动作只写 check_reports，现行流向/汇水表不变
            report_id = save_check_report(conn, old_edges, new_edges, result)
            result["ok"] = True
            result["report_id"] = report_id
            result["message"] = (
                f"核对完成：{result['group_count']} 个复核分组，"
                f"涉及 {result['flagged_cell_count']} 个格点；"
                f"出口已变更 {result['outlet_changed_count']} 格，"
                f"未跨界 {result['no_crossing_count']} 格"
            )
            return jsonify(result)
        finally:
            conn.close()

    @app.get("/api/reports")
    def api_reports():
        conn = db()
        try:
            return jsonify({"ok": True, "reports": list_check_reports(conn)})
        finally:
            conn.close()

    @app.get("/api/reports/<int:report_id>")
    def api_report_detail(report_id: int):
        conn = db()
        try:
            report = load_check_report(conn, report_id)
            if report is None:
                abort(404, description=f"复核报告 #{report_id} 不存在或已被清理")
            result = report["result"]
            return jsonify(
                {
                    "ok": True,
                    "report_id": report["id"],
                    "created_at": report["created_at"],
                    "result": result,
                    "old_edges": [
                        {
                            "row": cell[0],
                            "col": cell[1],
                            "down_row": None if d is None else d[0],
                            "down_col": None if d is None else d[1],
                        }
                        for cell, d in sorted(report["old_edges"].items())
                    ],
                    "new_edges": [
                        {
                            "row": cell[0],
                            "col": cell[1],
                            "down_row": None if d is None else d[0],
                            "down_col": None if d is None else d[1],
                        }
                        for cell, d in sorted(report["new_edges"].items())
                    ],
                }
            )
        finally:
            conn.close()

    @app.get("/api/reports/<int:report_id>/snapshot/<which>")
    def api_report_snapshot(report_id: int, which: str):
        if which not in ("old", "new"):
            abort(404, description="快照类型只能是 old 或 new")
        conn = db()
        try:
            report = load_check_report(conn, report_id)
            if report is None:
                abort(404, description=f"复核报告 #{report_id} 不存在或已被清理")
            edges = report["old_edges"] if which == "old" else report["new_edges"]
            label = "current" if which == "old" else "uploaded"
            return Response(
                dump_flow_csv(edges),
                mimetype="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f"attachment; filename=report_{report_id}_{label}.csv"
                    )
                },
            )
        finally:
            conn.close()

    return app


def sample_edges() -> dict[Cell, Optional[Cell]]:
    """4x4 示例：水流整体向右再向下，(3,3) 为唯一出口。"""
    edges: dict[Cell, Optional[Cell]] = {}
    for r in range(4):
        for c in range(4):
            if c < 3:
                edges[(r, c)] = (r, c + 1)
            elif r < 3:
                edges[(r, c)] = (r + 1, c)
            else:
                edges[(r, c)] = None
    return edges


# gunicorn / flask run 的入口
app = create_app()
