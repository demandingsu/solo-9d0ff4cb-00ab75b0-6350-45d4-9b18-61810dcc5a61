"""SQLite 持久化层：流向表与汇水表同库同事务保存。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .flow import (
    Accum,
    Cell,
    Edges,
    FlowError,
    apply_correction,
    compute_accumulation,
    validate_edges,
)

# 进程内写锁：把“读最新状态 -> 内存增量计算 -> 事务落库”整段串行化，
# 杜绝后拿锁的批次基于过期状态计算而覆盖先前已生效批次。
# 跨进程场景再由 SQLite 的 IMMEDIATE 写锁 + busy_timeout 兜底。
_WRITE_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    row_id    INTEGER NOT NULL,
    col_id    INTEGER NOT NULL,
    down_row  INTEGER,
    down_col  INTEGER,
    accum     INTEGER NOT NULL,
    PRIMARY KEY (row_id, col_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,           -- import | correction
    changed    TEXT NOT NULL,           -- JSON 快照
    affected   TEXT NOT NULL,           -- JSON 快照
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 跨界复核报告：只追加、从不更新。报告内嵌核对当时两张图的完整快照，
-- 因此现行流向/汇水表事后被更正也不影响已打开的报告。
CREATE TABLE IF NOT EXISTS check_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    old_edges   TEXT NOT NULL,          -- 核对时现行图快照 JSON
    new_edges   TEXT NOT NULL,          -- 上传图快照 JSON
    result      TEXT NOT NULL,          -- compare_catchments 结果 JSON
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 仅保留最近这么多份复核报告，更早的随新报告生成而清理
MAX_CHECK_REPORTS = 50


def get_db(db_path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path), timeout=15, check_same_thread=check_same_thread
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.executescript(SCHEMA)
    return conn


def load_edges(conn: sqlite3.Connection) -> Edges:
    edges: Edges = {}
    for row in conn.execute(
        "SELECT row_id, col_id, down_row, down_col FROM cells"
    ):
        downstream: Optional[Cell]
        if row["down_row"] is None or row["down_col"] is None:
            downstream = None
        else:
            downstream = (row["down_row"], row["down_col"])
        edges[(row["row_id"], row["col_id"])] = downstream
    return edges


def load_accum(conn: sqlite3.Connection) -> Accum:
    return {
        (row["row_id"], row["col_id"]): row["accum"]
        for row in conn.execute("SELECT row_id, col_id, accum FROM cells")
    }


def has_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM cells LIMIT 1").fetchone() is not None


def import_flow(
    conn: sqlite3.Connection,
    edges: Edges,
    check_neighbor: bool = True,
) -> Accum:
    """整批导入新流向：先在内存校验 + 全量重算，通过后一个事务替换全表。

    任一步失败都抛 :class:`FlowError`，事务回滚，旧数据保持可用。
    """
    validate_edges(edges, check_neighbor=check_neighbor)
    accum = compute_accumulation(edges)

    try:
        with _WRITE_LOCK, conn:  # 事务：异常自动回滚
            conn.execute("DELETE FROM cells")
            conn.executemany(
                "INSERT INTO cells (row_id, col_id, down_row, down_col, accum)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        cell[0],
                        cell[1],
                        None if downstream is None else downstream[0],
                        None if downstream is None else downstream[1],
                        accum[cell],
                    )
                    for cell, downstream in edges.items()
                ],
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('cell_count', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(len(edges)),),
            )
            conn.execute(
                "INSERT INTO corrections(kind, changed, affected)"
                " VALUES('import', ?, ?)",
                ("[]", "[]"),
            )
    except sqlite3.Error as exc:
        raise FlowError(f"数据库保存失败，已回滚，旧流向与汇水表保持不变：{exc}")
    return accum


def apply_batch(
    conn: sqlite3.Connection,
    changes: dict[Cell, Optional[Cell]],
    check_neighbor: bool = True,
) -> tuple[Edges, Accum, set[Cell]]:
    """应用一批更正。

    - 进程内写锁把“读最新状态 -> 内存增量计算 -> 事务落库”整段串行化，
      后到批次读取到的一定是先前已提交批次的最新流向；
    - 内存中做增量校验与重算，环/下游无效时不触碰数据库；
    - 通过后在一个事务内只写受影响格，汇水表与流向同事务更新；
    - 保存失败一律回滚，旧流向和旧汇水表照常可用。
    """
    with _WRITE_LOCK:
        old_edges = load_edges(conn)
        if not old_edges:
            raise FlowError("尚未导入流向数据，请先导入 CSV")
        old_accum = load_accum(conn)

        # 纯内存计算，不碰数据库，失败时库里数据毫发无损
        new_edges, new_accum, affected = apply_correction(
            old_edges, old_accum, changes, check_neighbor=check_neighbor
        )

        try:
            # IMMEDIATE：第一时间拿 SQLite 写锁（跨进程兜底）
            conn.execute("BEGIN IMMEDIATE")
            try:
                for cell in affected:
                    downstream = new_edges[cell]
                    conn.execute(
                        "UPDATE cells SET down_row=?, down_col=?, accum=?"
                        " WHERE row_id=? AND col_id=?",
                        (
                            None if downstream is None else downstream[0],
                            None if downstream is None else downstream[1],
                            new_accum[cell],
                            cell[0],
                            cell[1],
                        ),
                    )
                conn.execute(
                    "INSERT INTO corrections(kind, changed, affected)"
                    " VALUES(?, ?, ?)",
                    (
                        "correction",
                        json.dumps([list(k) for k in sorted(changes)]),
                        json.dumps([list(c) for c in sorted(affected)]),
                    ),
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                raise
        except sqlite3.Error as exc:
            raise FlowError(
                f"数据库保存失败，已回滚，旧流向与汇水表保持不变：{exc}"
            )

    return new_edges, new_accum, affected


def list_corrections(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id, kind, changed, affected, created_at"
            " FROM corrections ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    )


# --------------------------------------------------------------------------- #
# 跨界复核报告（只追加的快照，不触碰 cells 表）
# --------------------------------------------------------------------------- #
def _edges_to_json(edges: Edges) -> str:
    return json.dumps(
        [
            (
                cell[0],
                cell[1],
                None if downstream is None else downstream[0],
                None if downstream is None else downstream[1],
            )
            for cell, downstream in edges.items()
        ]
    )


def _edges_from_json(text: str) -> Edges:
    edges: Edges = {}
    for r, c, dr, dc in json.loads(text):
        edges[(r, c)] = None if dr is None else (dr, dc)
    return edges


def save_check_report(
    conn: sqlite3.Connection,
    old_edges: Edges,
    new_edges: Edges,
    result: dict,
) -> int:
    """把一次跨界复核的两图快照与结果作为不可变报告保存，返回报告 ID。

    只写 ``check_reports`` 表，现行流向表 ``cells`` 不做任何改动；
    保存失败时整体回滚，不留半截报告。
    """
    try:
        with _WRITE_LOCK, conn:
            cur = conn.execute(
                "INSERT INTO check_reports(old_edges, new_edges, result)"
                " VALUES(?, ?, ?)",
                (_edges_to_json(old_edges), _edges_to_json(new_edges),
                 json.dumps(result, ensure_ascii=False)),
            )
            report_id = int(cur.lastrowid)
            conn.execute(
                "DELETE FROM check_reports WHERE id NOT IN"
                " (SELECT id FROM check_reports ORDER BY id DESC LIMIT ?)",
                (MAX_CHECK_REPORTS,),
            )
            return report_id
    except sqlite3.Error as exc:
        raise FlowError(f"复核报告保存失败，已回滚：{exc}")


def list_check_reports(conn: sqlite3.Connection) -> list[dict]:
    """报告摘要列表（不含可能很大的两图快照与分组明细）。"""
    rows = conn.execute(
        "SELECT id, result, created_at FROM check_reports ORDER BY id DESC"
    ).fetchall()
    reports = []
    for row in rows:
        result = json.loads(row["result"])
        reports.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "cell_count": result["cell_count"],
                "group_count": result["group_count"],
                "flagged_cell_count": result["flagged_cell_count"],
                "outlet_changed_count": result["outlet_changed_count"],
            }
        )
    return reports


def load_check_report(conn: sqlite3.Connection, report_id: int) -> Optional[dict]:
    """取一份报告的完整内容（含核对当时的两图快照）；不存在返回 None。"""
    row = conn.execute(
        "SELECT id, old_edges, new_edges, result, created_at"
        " FROM check_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "old_edges": _edges_from_json(row["old_edges"]),
        "new_edges": _edges_from_json(row["new_edges"]),
        "result": json.loads(row["result"]),
    }
