"""Shared deterministic scan-process aggregations for status and reports."""
from __future__ import annotations

from collections import Counter, defaultdict


def scan_insights(conn, run_id: str) -> dict:
    nodes = [dict(row) for row in conn.execute(
        "SELECT node_id,path,level,state,result,current_scale,history_months FROM nodes WHERE run_id=? ORDER BY level,path",
        (run_id,),
    )]
    edges = [dict(row) for row in conn.execute(
        "SELECT parent_id,child_id,child_name FROM edges WHERE run_id=?", (run_id,),
    )]
    state_counts = Counter(row["state"] for row in nodes)
    levels = defaultdict(Counter)
    for row in nodes:
        levels[int(row["level"])][row["state"]] += 1
    children = defaultdict(list)
    by_id = {row["node_id"]: row for row in nodes}
    for edge in edges:
        children[edge["parent_id"]].append(by_id.get(edge["child_id"], {"state": "UNKNOWN"}))
    branch_rows = []
    sibling_divergence = []
    for parent_id, items in children.items():
        counts = Counter(item["state"] for item in items)
        branch_rows.append({"parent_id": parent_id, "children": len(items), "states": dict(counts)})
        if len(counts) > 1:
            sibling_divergence.append({"parent_id": parent_id, "children": len(items), "states": dict(counts)})
    paths = sorted(nodes, key=lambda row: (int(row["level"]), row["path"]), reverse=True)[:20]
    attempts = conn.execute(
        "SELECT status,COUNT(*) cnt FROM mcp_attempts WHERE run_id=? GROUP BY status", (run_id,),
    ).fetchall()
    mcp = {row["status"]: int(row["cnt"]) for row in attempts}
    total_attempts = sum(mcp.values())
    return {
        "coverage": {
            "discovered": len(nodes), "edges": len(edges),
            "eliminated": state_counts["ELIMINATED"],
            "opportunities": state_counts["OPPORTUNITY"],
            "policy_pruned": state_counts["POLICY_PRUNED"],
            "short_history": state_counts["SHORT_HISTORY"],
            "l4_plus": sum(1 for row in nodes if int(row["level"]) >= 4),
            "suspended": state_counts["SUSPENDED"],
            "unknown_descendants": state_counts["POLICY_PRUNED"] + state_counts["SHORT_HISTORY"],
        },
        "level_funnel": [{"level": level, "states": dict(counts), "total": sum(counts.values())}
                         for level, counts in sorted(levels.items())],
        "major_branches": sorted(branch_rows, key=lambda item: item["children"], reverse=True)[:20],
        "continuous_paths": [{"node_id": row["node_id"], "path": row["path"],
                              "level": row["level"], "state": row["state"]} for row in paths],
        "sibling_divergence": sibling_divergence[:20],
        "opportunity_frontier": [{"node_id": row["node_id"], "path": row["path"],
                                  "level": row["level"], "current_scale": row["current_scale"]}
                                 for row in nodes if row["state"] == "OPPORTUNITY"],
        "policy_pruning": [{"node_id": row["node_id"], "path": row["path"],
                            "current_scale": row["current_scale"], "result": row["result"]}
                           for row in nodes if row["state"] == "POLICY_PRUNED"],
        "short_history": [{"node_id": row["node_id"], "path": row["path"],
                           "months": row["history_months"], "result": row["result"]}
                          for row in nodes if row["state"] == "SHORT_HISTORY"],
        "mcp_efficiency": {"attempts": total_attempts, "success": mcp.get("成功", 0),
                           "failed": mcp.get("失败", 0),
                           "success_rate": (mcp.get("成功", 0) / total_attempts if total_attempts else None)},
    }
