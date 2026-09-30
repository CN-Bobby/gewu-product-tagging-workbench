"""Offline end-to-end regression for Xunlongjue leaf opportunity recovery.

Run from the repository root with:
    python -m pytest -q tests/test_xunlongjue_opportunity_recovery.py

All Sorftime responses are simulated and all run data lives in pytest's temporary
directory. The test exercises the normal Workbench HTTP routes and report output.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.xunlongjue import router, service, settings
from backend.xunlongjue.vendor.xlj_core import db as xlj_db


ROOT_NODE = "90000001"
LEAF_NODE = "90000002"


def _complete_months(count: int = 24) -> list[str]:
    now = datetime.now(timezone.utc)
    latest = now.year * 12 + now.month - 2
    months = []
    for index in range(latest - count + 1, latest + 1):
        year, zero_based_month = divmod(index, 12)
        months.append(f"{year:04d}-{zero_based_month + 1:02d}")
    return months


@pytest.fixture
def isolated_xunlongjue(tmp_path, monkeypatch):
    home = tmp_path / "xunlongjue-test-data"
    monkeypatch.setattr(settings, "_root", lambda: home)
    monkeypatch.setattr(service, "root", lambda: home)
    service.stop_all()
    settings.save("https://mcp.example.test/mcp", "offline-test-key")
    yield home
    service.stop_all()


def test_failed_leaf_lookup_stays_partial_then_resume_marks_verified_opportunity(
    isolated_xunlongjue, monkeypatch,
):
    """Trend-qualified is not a leaf until a successful empty tree is received."""
    all_calls: list[tuple[str, dict]] = []
    leaf_tree_attempts = 0
    months = _complete_months()

    class RecordedSorftime:
        def __init__(self, endpoint, api_key):
            self.endpoint = endpoint
            self.api_key = api_key

        async def call(self, tool: str, arguments: dict):
            nonlocal leaf_tree_attempts
            all_calls.append((tool, dict(arguments)))
            node_id = str(arguments["node_id"])

            if tool == "category_tree" and node_id == ROOT_NODE:
                return {"items": [{"id": LEAF_NODE, "name": "Qualified test leaf"}]}

            if tool == "category_tree" and node_id == LEAF_NODE:
                leaf_tree_attempts += 1
                if leaf_tree_attempts == 1:
                    raise RuntimeError("No data available.")
                return {"items": []}

            if tool == "category_trend":
                if arguments["trend_index"] == "SalesCount":
                    values = [12000 + index * 50 for index in range(24)]
                else:
                    values = [20 + index * 0.1 for index in range(24)]
                return {"months": months, "values": values}

            raise AssertionError(f"Unexpected Sorftime call: {tool} {arguments}")

    monkeypatch.setattr(service, "_CLIENT_FACTORY", RecordedSorftime)
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        created = client.post("/api/xunlongjue/runs", json={
            "site": "US",
            "node_id": ROOT_NODE,
            "category": "Opportunity recovery regression",
            "policy": {"max_attempts": 1, "batch_size": 20},
        })
        assert created.status_code == 200
        run_id = created.json()["run_id"]

        deadline = time.monotonic() + 10
        partial = client.get(f"/api/xunlongjue/runs/{run_id}").json()
        while (partial.get("status") != "partial_done" or not partial.get("report_url")) \
                and time.monotonic() < deadline:
            time.sleep(0.025)
            partial = client.get(f"/api/xunlongjue/runs/{run_id}").json()

        assert partial["status"] == "partial_done"
        assert partial["opportunities"] == 0
        assert partial["completion_ok"] is False

        conn = xlj_db.connect(str(isolated_xunlongjue / run_id / "workspace"))
        try:
            node = conn.execute(
                "SELECT state,result,current_scale FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, LEAF_NODE),
            ).fetchone()
            decision = conn.execute(
                "SELECT status FROM decisions WHERE run_id=? AND node_id=?",
                (run_id, LEAF_NODE),
            ).fetchone()
        finally:
            conn.close()

        assert node["state"] == "SUSPENDED"
        assert node["result"] == "MCP_OR_DATA_FAILED"
        assert node["current_scale"] >= 10000
        assert decision["status"] == "passed"

        partial_report = client.get(f"/api/xunlongjue/runs/{run_id}/report")
        assert partial_report.status_code == 200
        assert "阶段报告" in partial_report.text
        assert "正式机会: <strong>0</strong>" in partial_report.text

        resumed = client.post(f"/api/xunlongjue/runs/{run_id}/resume")
        assert resumed.status_code == 200

        deadline = time.monotonic() + 10
        final = client.get(f"/api/xunlongjue/runs/{run_id}").json()
        while (final.get("status") != "completed" or not final.get("report_url")) \
                and time.monotonic() < deadline:
            time.sleep(0.025)
            final = client.get(f"/api/xunlongjue/runs/{run_id}").json()

        assert final["status"] == "completed"
        assert final["engine_status"] == "ALL_DONE"
        assert final["opportunities"] == 1
        assert final["completion_ok"] is True

        conn = xlj_db.connect(str(isolated_xunlongjue / run_id / "workspace"))
        try:
            node = conn.execute(
                "SELECT state,result FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, LEAF_NODE),
            ).fetchone()
        finally:
            conn.close()

        assert node["state"] == "OPPORTUNITY"
        assert node["result"] == "LEAF_OPPORTUNITY"
        final_report = client.get(f"/api/xunlongjue/runs/{run_id}/report")
        assert final_report.status_code == 200
        assert "阶段报告" not in final_report.text
        assert "正式机会: <strong>1</strong>" in final_report.text
        assert "末端机会" in final_report.text
        assert leaf_tree_attempts == 2
        assert sum(tool == "category_tree" and args["node_id"] == LEAF_NODE
                   for tool, args in all_calls) == 2
