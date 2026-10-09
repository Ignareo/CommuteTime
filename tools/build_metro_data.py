#!/usr/bin/env python3
"""把 shmetro-accessibility 项目输出的 CSV 转换为前端消费的静态 JSON。

输入（对方项目 output 目录下）：
  amap_station_matches.csv   站点元数据 + GCJ-02 坐标（status==resolved 且 location 非空才可用）
  travel_time_matrix.csv     N×N 长表（utf-8-sig，只保留 status==done 的行）
  average_time_ranking.csv   站点平均通勤时间排行（average_minutes 可能为 "NaN"）
  stations_all.csv           可选：每条线的站点顺序（行序即线路顺序），用于生成地铁线折线

输出（--output 目录，例如 data/shanghai/）：
  meta.json            城市元信息、节点/组/对数、未解析节点列表
  stations.json        按站名聚合的分组（含坐标均值、线路、排行）
  rows/<group_id>.json 每组到其他组的分钟数：{"t": {"g002": 12, ...}}
  lines.json           可选：每条线的组 id 有序序列 + 标志色，供前端画 Polyline
并追加/更新 <output 父目录>/index.json 城市清单。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

MATCHES_COLUMNS = [
    "line_order", "line_label", "station_id", "station_slug", "station_name",
    "status", "score", "query_text", "poi_name", "poi_type", "poi_address",
    "poi_id", "location", "note", "source_key",
]
MATRIX_COLUMNS = [
    "from_id", "from_line_order", "from_line_label", "from_name",
    "to_id", "to_line_order", "to_line_label", "to_name",
    "status", "duration_seconds", "duration_minutes", "summary", "reason",
]
RANKING_COLUMNS = [
    "rank", "station_id", "line_order", "line_label", "station_name",
    "average_minutes", "sample_size",
]
STATIONS_ALL_COLUMNS = ["line", "station_id", "station_name"]

DEPARTURE_NOTE = "工作日 07:15 出发，含进出站步行与候车"

# 线路官方标志色：上海取自上海地铁官方发布（见维基百科「Template:上海地铁颜色」），
# 厦门为主题色近似值（1 闽南红 / 2 鹭岛绿 / 3 云天青 CMYK 63,0,18,0）；未收录线路用灰。
CITY_LINE_COLORS = {
    "shanghai": {
        "1号线": "#E3002B", "2号线": "#82BF25", "3号线": "#FCD600", "4号线": "#461D84",
        "5号线": "#944D9A", "6号线": "#D40068", "7号线": "#ED6F00", "8号线": "#0094D8",
        "9号线": "#87CAED", "10号线": "#C6AFD4", "11号线": "#871C2B", "12号线": "#007B61",
        "13号线": "#E999C0", "14号线": "#626020", "15号线": "#BCA886", "16号线": "#98D1C0",
        "17号线": "#BC796F", "18号线": "#C4984F", "浦江线": "#B5B5B6", "磁浮线": "#008B9A",
    },
    "xiamen": {
        "1号线": "#E60012", "2号线": "#00A650", "3号线": "#5ED1D1",
    },
}
DEFAULT_LINE_COLOR = "#9ca3af"


def fail(message: str) -> "SystemExit":
    return SystemExit(f"error: {message}")


def read_rows(path: Path, required_columns: list[str], encoding: str = "utf-8-sig"):
    """读取 CSV 并校验必需列，缺失文件/列时给出清晰报错。utf-8-sig 同时兼容带/不带 BOM。"""
    if not path.is_file():
        raise fail(f"输入文件不存在: {path}")
    with path.open(newline="", encoding=encoding) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in required_columns if c not in fieldnames]
        if missing:
            raise fail(f"{path.name} 缺少必需列: {', '.join(missing)}")
        for row in reader:
            yield row


def node_key(line_order: str, station_id: str) -> str:
    return f"{line_order}:{station_id}"


def line_order_sort_key(line_order: str):
    try:
        return (0, int(line_order), "")
    except ValueError:
        return (1, 0, line_order)


def round_half_up(value: float) -> int:
    return int(value + 0.5)


def parse_float(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def load_nodes(matches_path: Path):
    """返回 (nodes, unresolved)。nodes: key -> dict；unresolved: 不可用节点 key 列表。"""
    nodes: dict[str, dict] = {}
    unresolved: list[str] = []
    for row in read_rows(matches_path, MATCHES_COLUMNS):
        key = node_key(row["line_order"].strip(), row["station_id"].strip())
        location = (row.get("location") or "").strip()
        if row["status"].strip() != "resolved" or not location:
            unresolved.append(key)
            continue
        try:
            lng_text, lat_text = location.split(",")
            lng, lat = float(lng_text), float(lat_text)
        except ValueError:
            unresolved.append(key)
            continue
        nodes[key] = {
            "name": row["station_name"].strip(),
            "line_label": row["line_label"].strip(),
            "line_order": row["line_order"].strip(),
            "lng": lng,
            "lat": lat,
        }
    return nodes, sorted(set(unresolved))


def load_rankings(ranking_path: Path) -> dict[str, tuple[int, float]]:
    """node key -> (rank, average_minutes)，排除 average_minutes 为 NaN 的行。"""
    rankings: dict[str, tuple[int, float]] = {}
    for row in read_rows(ranking_path, RANKING_COLUMNS):
        avg = parse_float(row["average_minutes"].strip())
        if avg is None:
            continue
        try:
            rank = int(row["rank"].strip())
        except ValueError:
            continue
        key = node_key(row["line_order"].strip(), row["station_id"].strip())
        rankings[key] = (rank, avg)
    return rankings


def load_matrix(matrix_path: Path) -> dict[tuple[str, str], int]:
    """(from_key, to_key) -> duration_seconds，只保留 status==done，重复对取最小。"""
    matrix: dict[tuple[str, str], int] = {}
    for row in read_rows(matrix_path, MATRIX_COLUMNS, encoding="utf-8-sig"):
        if row["status"].strip() != "done":
            continue
        try:
            seconds = int(row["duration_seconds"].strip())
        except ValueError:
            continue
        pair = (
            node_key(row["from_line_order"].strip(), row["from_id"].strip()),
            node_key(row["to_line_order"].strip(), row["to_id"].strip()),
        )
        if pair not in matrix or seconds < matrix[pair]:
            matrix[pair] = seconds
    return matrix


def build_groups(nodes: dict[str, dict], rankings: dict[str, tuple[int, float]]):
    """按站名聚合节点为组，按 rank 排序后编号 g001...，无 rank 的排最后按名字排序。"""
    by_name: dict[str, list[str]] = defaultdict(list)
    for key, node in nodes.items():
        by_name[node["name"]].append(key)

    groups = []
    for name, keys in by_name.items():
        keys.sort(key=lambda k: line_order_sort_key(nodes[k]["line_order"]))
        ranked = [rankings[k] for k in keys if k in rankings]
        groups.append({
            "id": "",
            "name": name,
            "lng": round(sum(nodes[k]["lng"] for k in keys) / len(keys), 6),
            "lat": round(sum(nodes[k]["lat"] for k in keys) / len(keys), 6),
            "lines": [nodes[k]["line_label"] for k in keys],
            "avg_minutes": round(sum(a for _, a in ranked) / len(ranked), 1) if ranked else None,
            "rank": min(r for r, _ in ranked) if ranked else None,
            "nodes": keys,
        })

    groups.sort(key=lambda g: (
        g["rank"] is None,
        g["rank"] if g["rank"] is not None else 0,
        g["name"],
    ))
    for index, group in enumerate(groups, start=1):
        group["id"] = f"g{index:03d}"
    return groups


def build_rows(groups, matrix: dict[tuple[str, str], int]):
    """每组 -> {目的组 id: 分钟}，取组内节点间 done 记录的最小值。返回 (rows, pair_count)。"""
    rows: dict[str, dict[str, int]] = {}
    pair_count = 0
    for origin in groups:
        dest_minutes: dict[str, int] = {}
        for dest in groups:
            if dest["id"] == origin["id"]:
                continue
            best = None
            for from_key in origin["nodes"]:
                for to_key in dest["nodes"]:
                    seconds = matrix.get((from_key, to_key))
                    if seconds is not None and (best is None or seconds < best):
                        best = seconds
            if best is not None:
                dest_minutes[dest["id"]] = round_half_up(best / 60)
        if dest_minutes:
            rows[origin["id"]] = dict(sorted(dest_minutes.items()))
            pair_count += len(dest_minutes)
    return rows, pair_count


def load_lines(stations_all_path: Path, city: str, nodes: dict[str, dict], groups) -> tuple[list[dict], int]:
    """把 stations_all.csv 的线路站点顺序映射为组 id 序列（行序即线路顺序）。

    返回 (lines, missing)。stations_all.csv 不存在时返回 ([], 0)。
    stations_all 的 station_id 与 matches 不同格式，故按 (line, 站名) 关联。
    """
    if not stations_all_path.is_file():
        return [], 0

    by_line_name: dict[tuple[str, str], str] = {}
    label_by_order: dict[str, str] = {}
    for key, node in nodes.items():
        by_line_name[(node["line_order"], node["name"])] = key
        label_by_order.setdefault(node["line_order"], node["line_label"])
    node_group: dict[str, str] = {}
    for g in groups:
        for k in g["nodes"]:
            node_group[k] = g["id"]

    colors = CITY_LINE_COLORS.get(city, {})
    lines: list[dict] = []
    missing = 0

    def flush(order: str, names: list[str]) -> None:
        nonlocal missing
        seq: list[str] = []
        for name in names:
            key = by_line_name.get((order, name))
            gid = node_group.get(key) if key else None
            if gid is None:
                missing += 1
                continue
            if not seq or seq[-1] != gid:   # 去重，保序
                seq.append(gid)
        if len(seq) < 2:
            missing += len(seq)
            return
        label = label_by_order.get(order, order)
        lines.append({"label": label, "color": colors.get(label, DEFAULT_LINE_COLOR), "groups": seq})

    # 两种 schema：上海 root 输出 {line, station_id, station_name}；
    # 厦门等分城市输出 {line_order, line_label, station_id, station_slug, station_name, source_key}
    if not stations_all_path.is_file():
        return [], 0
    with stations_all_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "line_order" in fieldnames:
            col_order, col_name = "line_order", "station_name"
        elif "line" in fieldnames:
            col_order, col_name = "line", "station_name"
        else:
            return [], 0
        csv_rows = [(row[col_order].strip(), row[col_name].strip()) for row in reader]

    current_order: str | None = None
    current_names: list[str] = []
    for order, name in csv_rows:
        if current_order is None:
            current_order = order
        elif order != current_order:
            flush(current_order, current_names)
            current_order, current_names = order, []
        current_names.append(name)
    if current_order is not None:
        flush(current_order, current_names)
    return lines, missing


def update_index(index_path: Path, city: str, city_name: str, group_count: int) -> None:
    cities = []
    if index_path.is_file():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            cities = [c for c in data.get("cities", []) if c.get("city") != city]
        except (json.JSONDecodeError, AttributeError):
            raise fail(f"已有的 {index_path} 不是合法的城市清单 JSON")
    cities.append({"city": city, "city_name": city_name, "group_count": group_count})
    cities.sort(key=lambda c: c["city"])
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"cities": cities}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 shmetro-accessibility 输出的 CSV 转换为前端静态 JSON。",
    )
    parser.add_argument("--city", required=True, help="城市标识，如 shanghai")
    parser.add_argument("--city-name", required=True, help="城市中文名，如 上海")
    parser.add_argument("--input", required=True, type=Path,
                        help="对方项目的 output 目录（含三个 CSV）")
    parser.add_argument("--output", required=True, type=Path,
                        help="输出目录，如 data/shanghai")
    args = parser.parse_args()

    input_dir: Path = args.input
    if not input_dir.is_dir():
        raise fail(f"输入目录不存在: {input_dir}")

    nodes, unresolved = load_nodes(input_dir / "amap_station_matches.csv")
    rankings = load_rankings(input_dir / "average_time_ranking.csv")
    matrix = load_matrix(input_dir / "travel_time_matrix.csv")
    if not nodes:
        raise fail("amap_station_matches.csv 中没有可用的 resolved 站点")

    groups = build_groups(nodes, rankings)
    rows, pair_count = build_rows(groups, matrix)
    lines, line_missing = load_lines(input_dir / "stations_all.csv", args.city, nodes, groups)

    output_dir: Path = args.output
    rows_dir = output_dir / "rows"
    rows_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "stations.json").write_text(
        json.dumps({"groups": groups}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if lines:
        (output_dir / "lines.json").write_text(
            json.dumps({"lines": lines}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    for group_id, dest in rows.items():
        (rows_dir / f"{group_id}.json").write_text(
            json.dumps({"t": dest}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    meta = {
        "city": args.city,
        "city_name": args.city_name,
        "generated_at": date.today().isoformat(),
        "departure": DEPARTURE_NOTE,
        "group_count": len(groups),
        "node_count": len(nodes),
        "pair_count": pair_count,
        "line_count": len(lines),
        "line_missing_stations": line_missing,
        "unresolved_nodes": unresolved,
    }
    (output_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    update_index(output_dir.parent / "index.json", args.city, args.city_name, len(groups))

    print(f"{args.city_name}: {len(groups)} 组 / {len(nodes)} 节点 / {pair_count} 组间对"
          f"（未解析节点 {len(unresolved)} 个）-> {output_dir}")
    if lines:
        print(f"线路折线 {len(lines)} 条（缺站点 {line_missing} 个）-> {output_dir / 'lines.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
