"""跨界复核算法测试。

- 手工构造一条在两个原集水区之间反复跨界的新流路，断言分组口径；
- 随机图上与“逐格走完整条新流路”的朴素实现逐格对照。
"""

import random

import pytest

from app.flow import (
    boundary_edges_along_new_flow,
    compare_catchments,
    compute_accumulation,
    outlet_of_each_cell,
    validate_edges,
)

H, W = 5, 3


def old_stripe_edges():
    """5×3 条纹图：第 0/1 列属集水区 A，出口 (0,0)；第 2 列属集水区 B，
    出口 (4,2)。A 区先向左汇入第 0 列再向上，B 区沿列向下。"""
    edges = {}
    for r in range(H):
        for c in range(W):
            if c <= 1:  # 集水区 A
                if c == 1:
                    edges[(r, c)] = (r, 0)
                elif r > 0:
                    edges[(r, c)] = (r - 1, 0)
                else:
                    edges[(r, c)] = None  # (0,0) 出口
            else:  # 集水区 B：第 2 列向下
                edges[(r, c)] = None if r == H - 1 else (r + 1, 2)
    return edges


def new_snake_edges(old):
    """在旧图上改出一条蛇形新流路（其余格保留旧边）：

    (0,1)A→(0,2)B→(1,2)B→(1,1)A→(2,1)A→(2,2)B→(3,2)B→(3,1)A
          →(4,1)A→(4,0)A→…沿第 0 列向上→(0,0)

    (0,1) 的新流路两次出界、两次回界，最终仍回原出口 (0,0)。
    """
    new = dict(old)
    path_edges = [
        ((0, 1), (0, 2)),
        ((0, 2), (1, 2)),
        ((1, 2), (1, 1)),
        ((1, 1), (2, 1)),
        ((2, 1), (2, 2)),
        ((2, 2), (3, 2)),
        ((3, 2), (3, 1)),
        ((3, 1), (4, 1)),
        ((4, 1), (4, 0)),
    ]
    for cell, downstream in path_edges:
        new[cell] = downstream
    return new


def test_handbuilt_repeated_crossing_groups():
    old = old_stripe_edges()
    validate_edges(old)
    new = new_snake_edges(old)
    validate_edges(new)
    assert set(old) == set(new)

    result = compare_catchments(old, new)
    assert result["cell_count"] == H * W

    # (0,1) 反复跨界只计一次，单独成组：首条出界边与末条回界边各一条
    g1 = next(
        g for g in result["groups"]
        if g["first_exit"]["from_row"] == 0 and g["first_exit"]["from_col"] == 1
    )
    assert g1["outlet"] == {"row": 0, "col": 0}
    assert (g1["first_exit"]["from_row"], g1["first_exit"]["from_col"]) == (0, 1)
    assert (g1["first_exit"]["to_row"], g1["first_exit"]["to_col"]) == (0, 2)
    assert (g1["last_return"]["from_row"], g1["last_return"]["from_col"]) == (3, 2)
    assert (g1["last_return"]["to_row"], g1["last_return"]["to_col"]) == (3, 1)
    assert g1["cell_count"] == 1
    assert g1["cells"] == [{"row": 0, "col": 1}]

    # (1,1)/(2,1) 的新流路各跨界一次，首出/末回相同 -> 同组两格
    g2 = next(
        g for g in result["groups"]
        if (g["first_exit"]["from_row"], g["first_exit"]["from_col"]) == (2, 1)
    )
    assert g2["cell_count"] == 2
    assert g2["cells"] == [{"row": 1, "col": 1}, {"row": 2, "col": 1}]
    assert (g2["last_return"]["from_row"], g2["last_return"]["from_col"]) == (3, 2)

    assert result["group_count"] == 2
    assert result["flagged_cell_count"] == 3
    # 4 个 B 区格点新出口变成 (0,0)，出口已变 -> 不计入复核
    assert result["outlet_changed_count"] == 4
    # 其余 8 格出口未变且从未跨界
    assert result["no_crossing_count"] == 8

    # 分组按原出口、首出边、末回边排序
    keys = [
        (
            (g["outlet"]["row"], g["outlet"]["col"]),
            (g["first_exit"]["from_row"], g["first_exit"]["from_col"]),
        )
        for g in result["groups"]
    ]
    assert keys == sorted(keys)


def test_identical_graphs_flag_nothing():
    old = old_stripe_edges()
    result = compare_catchments(old, dict(old))
    assert result["group_count"] == 0
    assert result["flagged_cell_count"] == 0
    assert result["outlet_changed_count"] == 0
    assert result["no_crossing_count"] == H * W


def test_outlet_changed_cells_are_excluded_even_if_path_crosses():
    """换了出口的格即使新流路穿过别的集水区，也不属于本次复核范围。"""
    old = old_stripe_edges()
    new = new_snake_edges(old)
    # B 区格 (2,2) 旧出口是 (4,2)，新出口是 (0,0)，且路径跨界 -> 必须排除
    old_out = outlet_of_each_cell(old)
    new_out = outlet_of_each_cell(new)
    assert old_out[(2, 2)] == (4, 2) and new_out[(2, 2)] == (0, 0)
    flagged = {
        (c["row"], c["col"])
        for g in compare_catchments(old, new)["groups"]
        for c in g["cells"]
    }
    assert (2, 2) not in flagged
    assert all(cell[1] != 2 for cell in flagged)  # 命中格全部来自 A 区


def test_boundary_memoization_keeps_only_first_and_last():
    old = old_stripe_edges()
    new = new_snake_edges(old)
    old_out = outlet_of_each_cell(old)
    boundary = boundary_edges_along_new_flow(new, old_out)
    first_edge, last_edge = boundary[(0, 1)]
    # 流路上共 4 条跨界边，记忆化结果只留首/末各一条
    assert first_edge == ((0, 1), (0, 2))
    assert last_edge == ((3, 2), (3, 1))
    # 无跨界流路返回 (None, None)
    assert boundary[(0, 0)] == (None, None)
    assert boundary[(4, 0)] == (None, None)
    # 每个格只记忆化一次：结果表大小等于格点数
    assert len(boundary) == H * W


# --------------------------------------------------------------------------- #
# 随机性质测试：与逐格走完整流路的朴素实现对照
# --------------------------------------------------------------------------- #
def random_dag(rng: random.Random, height: int, width: int):
    cells = [(r, c) for r in range(height) for c in range(width)]
    index = {cell: i for i, cell in enumerate(cells)}
    edges = {}
    for cell in cells:
        r, c = cell
        neighbors = [
            (r + dr, c + dc)
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1))
            if 0 <= r + dr < height and 0 <= c + dc < width
        ]
        forward = [n for n in neighbors if index[n] > index[cell]]
        if not forward:
            edges[cell] = None
        elif rng.random() < 0.15:
            edges[cell] = None  # 随机多出口，制造多个原集水区
        else:
            edges[cell] = rng.choice(forward)
    return edges


def naive_compare(old, new):
    """朴素参照：每个格都沿新图完整走一遍，收集全部跨界边。"""
    old_out = outlet_of_each_cell(old)

    def walk_outlet(edges, start):
        seen, node = set(), start
        while edges[node] is not None:
            assert node not in seen, "测试图必须无环"
            seen.add(node)
            node = edges[node]
        return node

    groups = {}
    counters = {"flagged": 0, "outlet_changed": 0, "no_crossing": 0}
    for cell in old:
        if walk_outlet(new, cell) != old_out[cell]:
            counters["outlet_changed"] += 1
            continue
        crossed, node = [], cell
        seen = set()
        while new[node] is not None:
            assert node not in seen
            seen.add(node)
            nxt = new[node]
            if old_out[node] != old_out[nxt]:
                crossed.append((node, nxt))
            node = nxt
        if not crossed:
            counters["no_crossing"] += 1
            continue
        key = (old_out[cell], crossed[0], crossed[-1])
        groups.setdefault(key, set()).add(cell)
        counters["flagged"] += 1
    return groups, counters


@pytest.mark.parametrize("seed", range(40))
def test_matches_naive_full_walk_on_random_graphs(seed):
    rng = random.Random(1000 + seed)
    old = random_dag(rng, H, W)
    new = random_dag(rng, H, W)
    validate_edges(old)
    validate_edges(new)
    # 朴素实现自身需要的不变量
    compute_accumulation(old)
    compute_accumulation(new)

    result = compare_catchments(old, new)
    naive_groups, counters = naive_compare(old, new)

    assert result["flagged_cell_count"] == counters["flagged"]
    assert result["outlet_changed_count"] == counters["outlet_changed"]
    assert result["no_crossing_count"] == counters["no_crossing"]
    assert result["group_count"] == len(naive_groups)

    got = {}
    for g in result["groups"]:
        outlet = (g["outlet"]["row"], g["outlet"]["col"])
        first = (
            (g["first_exit"]["from_row"], g["first_exit"]["from_col"]),
            (g["first_exit"]["to_row"], g["first_exit"]["to_col"]),
        )
        last = (
            (g["last_return"]["from_row"], g["last_return"]["from_col"]),
            (g["last_return"]["to_row"], g["last_return"]["to_col"]),
        )
        got[(outlet, first, last)] = {
            (c["row"], c["col"]) for c in g["cells"]
        }
    assert got == naive_groups
    # 每个命中格在全部分组里只出现一次
    all_cells = [c for cells in got.values() for c in cells]
    assert len(all_cells) == len(set(all_cells))
