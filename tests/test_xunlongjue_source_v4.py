"""Source regression suite carried from Xunlongjue v4.2.0-beta.3."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from backend.xunlongjue.vendor.xlj_core.cli import main as cli_main
from backend.xunlongjue.vendor.xlj_core.db import connect, transaction, utc_now
from backend.xunlongjue.vendor.xlj_core.engine import (PROTOCOL_VERSION, _normalize_trend_payload, create_run,
                             get_owner, ingest_batch, ingest_results, lease_jobs,
                             list_batches, pulse_batch, resume_run, start_run, status, takeover_run)
from backend.xunlongjue.vendor.xlj_core.ledger import (claim_request, create_request, list_presets, list_requests,
                             save_profile, save_preset)
from backend.xunlongjue.vendor.xlj_core.outputs import generate_html, regenerate_reports, validate_report_contract
from backend.xunlongjue.vendor.xlj_core.panel import open_panel, panel_status, stop_panel
from backend.xunlongjue.vendor.xlj_core.syncpack import export_pack, import_pack


def recent_months(count=24):
    now = datetime.now(timezone.utc); end = now.year * 12 + now.month - 2
    result = []
    for index in range(end - count + 1, end + 1):
        result.append(f"{index // 12:04d}-{index % 12 + 1:02d}")
    return result


def document(run_id, jobs, payloads=None, failures=False):
    payloads = payloads or {}
    results = []
    for job in jobs:
        payload = payloads.get(job["indicator"] or job["type"], [])
        results.append({"job_id": job["job_id"], "attempt": job["attempt"],
                        "lease_token": job["lease_token"], "status": "failed" if failures else "success",
                        "payload": payload, "error": "timeout" if failures else ""})
    return {"schema_version": PROTOCOL_VERSION, "run_id": run_id,
            "batch_id": jobs[0]["batch_id"] if jobs else None, "results": results}


def trend_payload(values):
    return {"months": recent_months(len(values)), "values": values}


class V41Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.workspace = self.tmp.name
    def tearDown(self): self.tmp.cleanup()

    def expand_root(self, run_id, child_id=None, child_name="Subject"):
        jobs = lease_jobs(self.workspace, run_id)["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["type"], "category_tree")
        root_id = jobs[0]["node_id"]
        child_id = child_id or (root_id + "01")
        ingest_results(self.workspace, run_id, document(
            run_id, jobs, {"category_tree": [{"id": child_id, "name": child_name}]},
        ))
        return child_id

    def ingest_trends(self, run_id, sales, prices):
        jobs = lease_jobs(self.workspace, run_id)["jobs"]
        if jobs and jobs[0]["type"] == "category_tree":
            root_id = jobs[0]["node_id"]
            ingest_results(self.workspace, run_id, document(
                run_id, jobs, {"category_tree": [{"id": root_id + "01", "name": "Subject"}]},
            ))
            jobs = lease_jobs(self.workspace, run_id)["jobs"]
        self.assertEqual({j["indicator"] for j in jobs}, {"SalesCount", "AvgPrice"})
        return ingest_results(self.workspace, run_id, document(run_id, jobs, {
            "SalesCount": trend_payload(sales), "AvgPrice": trend_payload(prices)}))

    def test_root_expands_first_and_only_children_are_evaluated(self):
        run_id = create_run(self.workspace, "US", "1288631011", "Kitchen")
        jobs = lease_jobs(self.workspace, run_id)["jobs"]
        self.assertEqual([j["type"] for j in jobs], ["category_tree"])
        self.assertEqual(jobs[0]["node_id"], "1288631011")
        ingest_results(self.workspace, run_id, document(run_id, jobs, {
            "category_tree": [{"id":"1288631012","name":"Child"}]}))
        trends = lease_jobs(self.workspace, run_id)["jobs"]
        self.assertEqual({j["indicator"] for j in trends}, {"SalesCount", "AvgPrice"})
        self.assertEqual({j["node_id"] for j in trends}, {"1288631012"})
        ingest_results(self.workspace, run_id, document(run_id, trends, {
            "SalesCount": trend_payload([9000] * 24), "AvgPrice": trend_payload([20 + i*.1 for i in range(24)])}))
        current = status(self.workspace, run_id)
        self.assertEqual(current["status"], "ALL_DONE")
        self.assertEqual(current["node_counts"]["POLICY_PRUNED"], 1)
        conn=connect(self.workspace)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM decisions WHERE run_id=? AND node_id='1288631011'",(run_id,)).fetchone()[0],0)
        conn.close()
        self.assertEqual(lease_jobs(self.workspace, run_id)["jobs"], [])

    def test_empty_root_tree_closes_without_creating_root_opportunity(self):
        run_id=create_run(self.workspace,"US","1288631099","Empty Root")
        jobs=lease_jobs(self.workspace,run_id)["jobs"]
        ingest_results(self.workspace,run_id,document(run_id,jobs,{"category_tree":[]}))
        current=status(self.workspace,run_id)
        self.assertEqual(current["status"],"ALL_DONE")
        self.assertEqual(current["opportunities"],0)
        self.assertEqual(current["node_counts"],{"EXPANDED":1})

    def test_volume_boundaries_use_recent_six_months(self):
        for index, scale in enumerate((9999, 10000, 10001), 1):
            run_id = create_run(self.workspace, "US", str(100 + index), f"Scale {scale}")
            self.ingest_trends(run_id, [scale] * 24, [20 + i*.1 for i in range(24)])
            current = status(self.workspace, run_id)
            if scale < 10000:
                self.assertEqual(current["node_counts"].get("POLICY_PRUNED"), 1)
            else:
                self.assertEqual(lease_jobs(self.workspace, run_id)["jobs"][0]["type"], "category_tree")

    def test_policy_validation_and_custom_threshold_report_labels(self):
        for policy, message in (({"batch_size": 0}, "批量"), ({"max_attempts": 11}, "重试"),
                                ({"min_r_squared": 2}, "R²"), ({"min_avg_sales": float("nan")}, "NaN")):
            with self.assertRaisesRegex(ValueError, message):
                create_run(self.workspace, "US", "190", "Invalid", policy=policy)
        run_id = create_run(self.workspace, "US", "191", "Custom", policy={"min_avg_sales": 9000})
        self.ingest_trends(run_id, [9500 + i * 10 for i in range(24)], [20 + i * .1 for i in range(24)])
        tree = lease_jobs(self.workspace, run_id)["jobs"]
        ingest_results(self.workspace, run_id, document(run_id, tree, {"category_tree": []}))
        path = generate_html(self.workspace, run_id, "final", self.workspace)
        with open(path, encoding="utf-8") as handle: html = handle.read()
        self.assertIn("月均销量 ≥ 9,000", html)
        self.assertIn("末端机会", html)
        self.assertNotIn('badge-potential">潜力</span>', html)

    def test_low_volume_growth_candidate_never_drills(self):
        run_id = create_run(self.workspace, "US", "201", "Growth")
        sales = [4000 + i * 250 for i in range(24)]
        self.ingest_trends(run_id, sales, [15 + i*.2 for i in range(24)])
        conn = connect(self.workspace)
        row = conn.execute("SELECT opportunity_level FROM decisions WHERE run_id=?", (run_id,)).fetchone(); conn.close()
        self.assertEqual(row[0], "LOW_VOLUME_GROWTH_CANDIDATE")
        self.assertEqual(status(self.workspace, run_id)["opportunities"], 0)
        self.assertEqual(lease_jobs(self.workspace, run_id)["jobs"], [])
        path = generate_html(self.workspace, run_id, "final", self.workspace)
        with open(path, encoding="utf-8") as handle: html = handle.read()
        self.assertIn("低体量增长候选 — 非正式机会", html)
        self.assertIn("正式机会: <strong>0</strong>", html)

    def test_recent_six_not_full_average_drives_scale(self):
        run_id = create_run(self.workspace, "US", "202", "Recent")
        sales = [20000] * 18 + [9000] * 6
        self.ingest_trends(run_id, sales, [20 + i*.1 for i in range(24)])
        current = status(self.workspace, run_id)
        self.assertEqual(current["node_counts"].get("POLICY_PRUNED"), 1)

    def test_short_history_rules_and_one_expansion_only(self):
        low = create_run(self.workspace, "US", "301", "Short Low")
        self.ingest_trends(low, [9000] * 6, [20] * 6)
        self.assertEqual(status(self.workspace, low)["node_counts"].get("SHORT_HISTORY"), 1)
        high = create_run(self.workspace, "US", "302", "Short High")
        self.ingest_trends(high, [12000] * 7, [20] * 7)
        tree = lease_jobs(self.workspace, high)["jobs"]
        ingest_results(self.workspace, high, document(high, tree, {"category_tree": [{"id":"303","name":"Child"}]}))
        child_jobs = lease_jobs(self.workspace, high)["jobs"]
        ingest_results(self.workspace, high, document(high, child_jobs, {
            "SalesCount": trend_payload([13000]*7), "AvgPrice": trend_payload([21]*7)}))
        self.assertEqual(lease_jobs(self.workspace, high)["jobs"], [])
        self.assertEqual(status(self.workspace, high)["status"], "ALL_DONE")

    def test_high_volume_full_history_reaches_leaf_opportunity(self):
        run_id=create_run(self.workspace,"CA","310","High")
        self.ingest_trends(run_id,[12000+i*50 for i in range(24)],[20+i*.1 for i in range(24)])
        tree=lease_jobs(self.workspace,run_id)["jobs"]
        self.assertEqual(len(tree),1); self.assertEqual(tree[0]["type"],"category_tree")
        ingest_results(self.workspace,run_id,document(run_id,tree,{"category_tree":[]}))
        current=status(self.workspace,run_id)
        self.assertEqual(current["status"],"ALL_DONE"); self.assertEqual(current["opportunities"],1)

    def test_invalid_short_gap_stale_and_mismatch_are_retried(self):
        with self.assertRaisesRegex(ValueError, "6至24"):
            _normalize_trend_payload({"months": recent_months(5), "values":[1]*5}, "SalesCount")
        bad = recent_months(6); bad[-1] = bad[0]
        with self.assertRaisesRegex(ValueError, "连续"):
            _normalize_trend_payload({"months":bad,"values":[1]*6}, "SalesCount")
        stale = [f"2020-{i:02d}" for i in range(1,7)]
        with self.assertRaisesRegex(ValueError, "过旧"):
            _normalize_trend_payload({"months":stale,"values":[1]*6}, "SalesCount")

    def test_lease_token_stale_rejection_backoff_and_resume(self):
        run_id = create_run(self.workspace, "US", "401", "Lease")
        for attempt in range(3):
            jobs = lease_jobs(self.workspace, run_id)["jobs"]
            if not jobs:
                conn=connect(self.workspace)
                with transaction(conn): conn.execute("UPDATE jobs SET eligible_at=? WHERE run_id=? AND state='PENDING'", (utc_now(),run_id))
                conn.close(); jobs=lease_jobs(self.workspace,run_id)["jobs"]
            stale=document(run_id,jobs,failures=True); stale["results"][0]["lease_token"]="wrong"
            with self.assertRaisesRegex(ValueError,"租约"): ingest_results(self.workspace,run_id,stale)
            ingest_results(self.workspace,run_id,document(run_id,jobs,failures=True))
        self.assertEqual(status(self.workspace,run_id)["status"],"PARTIAL_DONE")
        resumed=resume_run(self.workspace,run_id,True); self.assertGreater(resumed["resumed_nodes"],0)

    def test_completed_at_is_write_once_and_depth_event_once(self):
        run_id=create_run(self.workspace,"US","501","Done")
        self.ingest_trends(run_id,[9000]*24,[20+i*.1 for i in range(24)])
        first=status(self.workspace,run_id)["completed_at"]; second=status(self.workspace,run_id)["completed_at"]
        self.assertEqual(first,second)
        conn=connect(self.workspace); levels=conn.execute("SELECT node_id,COUNT(*) FROM events WHERE run_id=? AND event_type='DEPTH_COMPLETED' GROUP BY node_id",(run_id,)).fetchall(); conn.close()
        self.assertEqual({row[0]:row[1] for row in levels},{"1":1,"2":1})

    def test_report_insights_metadata_and_favorite_key(self):
        run_id=create_run(self.workspace,"US","601","Report",operator_name="Alice")
        self.ingest_trends(run_id,[9000]*24,[20+i*.1 for i in range(24)])
        path=generate_html(self.workspace,run_id,"final",self.workspace)
        with open(path,encoding="utf-8") as handle: html=handle.read()
        self.assertIn("扫描过程洞察",html); self.assertIn("Alice",html)
        self.assertIn(f"xlj:favorites:US:{run_id}",html)
        self.assertIn("https://www.amazon.com/gp/bestsellers/kitchen/601",html)
        for marker in ("寻龙诀 · 选品挖掘报告", "全量节点明细", "回归销量斜率", "YoY销量",
                       "sortTable", "openNote", "filterStar", "exportData", "本次运行说明"):
            self.assertIn(marker, html)
        self.assertNotIn("<pre>", html)

    def test_historical_report_batch_regeneration_never_calls_mcp(self):
        finished = create_run(self.workspace, "US", "611", "Finished")
        self.ingest_trends(finished, [9000] * 24, [20 + i * .1 for i in range(24)])
        running = create_run(self.workspace, "UK", "612", "Running")
        before = status(self.workspace, finished)["mcp_attempts"]
        result = regenerate_reports(self.workspace, self.workspace)
        self.assertEqual([item["run_id"] for item in result["generated"]], [finished])
        self.assertEqual([item["run_id"] for item in result["skipped"]], [running])
        self.assertEqual(result["errors"], [])
        self.assertEqual(status(self.workspace, finished)["mcp_attempts"], before)
        self.assertTrue(os.path.isfile(result["generated"][0]["path"]))
        included = regenerate_reports(self.workspace, self.workspace, include_running=True)
        partial = next(item for item in included["generated"] if item["run_id"] == running)
        self.assertEqual(partial["mode"], "partial")

    def test_panel_request_claim_freezes_parameters(self):
        save_profile(self.workspace,"Bob","Laptop")
        req=create_request(self.workspace,"UK","701","Home",{"min_avg_sales":9000})
        claimed=claim_request(self.workspace,req["request_code"]); again=claim_request(self.workspace,req["request_code"])
        self.assertEqual(claimed["run_id"],again["run_id"]); self.assertTrue(again["already_claimed"])
        current=status(self.workspace,claimed["run_id"]); self.assertEqual(current["policy"]["min_avg_sales"],9000)
        self.assertEqual(current["operator_name"],"Bob")
        self.assertTrue(claimed["start_allowed"]); self.assertFalse(again["start_allowed"])
        self.assertIn("worker_id", claimed); self.assertNotIn("worker_id", again)

    def test_concurrent_claim_creates_exactly_one_run(self):
        request = create_request(self.workspace, "US", "705", "Concurrent Claim")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: claim_request(self.workspace, request["request_code"]), range(2)))
        self.assertEqual(len({item["run_id"] for item in claimed}), 1)
        self.assertEqual(sorted(item["start_allowed"] for item in claimed), [False, True])
        conn = connect(self.workspace)
        try: self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        finally: conn.close()

    def test_managed_ipc_batch_atomic_ingest_and_idempotency(self):
        run_id=create_run(self.workspace,"US","711","IPC")
        self.expand_root(run_id)
        owner=get_owner(self.workspace,run_id)["worker_id"]
        leased=lease_jobs(self.workspace,run_id,20,owner)
        self.assertTrue(os.path.isfile(leased["jobs_path"]))
        self.assertIn(os.path.join("ipc",run_id,leased["batch_id"]),leased["jobs_path"])
        incomplete=document(run_id,leased["jobs"][:1],{
            "SalesCount":trend_payload([9000]*24),"AvgPrice":trend_payload([20]*24)})
        incomplete["batch_id"]=leased["batch_id"]
        with open(leased["results_draft_path"],"w",encoding="utf-8") as handle: json.dump(incomplete,handle)
        with self.assertRaisesRegex(ValueError,"不完整"): ingest_batch(self.workspace,run_id,leased["batch_id"],owner)
        conn=connect(self.workspace)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND state='LEASED'",(run_id,)).fetchone()[0],2)
        conn.close()
        complete=document(run_id,leased["jobs"],{
            "SalesCount":trend_payload([9000]*24),"AvgPrice":trend_payload([20]*24)})
        with open(leased["results_path"],"w",encoding="utf-8") as handle: json.dump(complete,handle)
        first=ingest_batch(self.workspace,run_id,leased["batch_id"],owner)
        second=ingest_batch(self.workspace,run_id,leased["batch_id"],owner)
        self.assertEqual(first,second); self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(leased["jobs_path"]),"receipt.json")))
        with open(leased["results_path"], "rb") as handle: sealed = handle.read()
        with open(leased["results_draft_path"], "w", encoding="utf-8") as handle: handle.write("{}")
        third = ingest_batch(self.workspace, run_id, leased["batch_id"], owner)
        with open(leased["results_path"], "rb") as handle: self.assertEqual(handle.read(), sealed)
        self.assertEqual(third, first)

    def test_ten_runs_and_same_run_concurrent_next_are_isolated(self):
        run_ids=[create_run(self.workspace,"US",str(8000+i),f"Run {i}") for i in range(10)]
        with ThreadPoolExecutor(max_workers=10) as pool:
            batches=list(pool.map(lambda rid: lease_jobs(self.workspace,rid,20,get_owner(self.workspace,rid)["worker_id"]),run_ids))
        self.assertEqual(len({item["batch_id"] for item in batches}),10)
        self.assertEqual(len({item["jobs_path"] for item in batches}),10)
        self.assertTrue(all(os.path.isfile(item["jobs_path"]) for item in batches))
        one=create_run(self.workspace,"US","8999","Same Run")
        owner=get_owner(self.workspace,one)["worker_id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            leased=list(pool.map(lambda _: lease_jobs(self.workspace,one,1,owner),range(2)))
        self.assertEqual(sum(1 for item in leased if item.get("busy")),1)
        job_ids=[item["jobs"][0]["job_id"] for item in leased if item["jobs"]]
        self.assertEqual(len(set(job_ids)),1)
        same_us=create_run(self.workspace,"US","7777","Shared Node")
        same_de=create_run(self.workspace,"DE","7777","Shared Node")
        us_batch=lease_jobs(self.workspace,same_us,20,get_owner(self.workspace,same_us)["worker_id"])
        de_batch=lease_jobs(self.workspace,same_de,20,get_owner(self.workspace,same_de)["worker_id"])
        self.assertNotEqual(us_batch["jobs_path"],de_batch["jobs_path"])
        self.assertEqual(us_batch["jobs"][0]["node_id"],de_batch["jobs"][0]["node_id"])

    def test_expired_batch_rejected_and_force_takeover_preserves_run(self):
        run_id=create_run(self.workspace,"US","9101","Expiry")
        owner=get_owner(self.workspace,run_id)["worker_id"]
        old=lease_jobs(self.workspace,run_id,20,owner)
        with self.assertRaisesRegex(ValueError,"有效批次租约"): takeover_run(self.workspace,run_id)
        taken=takeover_run(self.workspace,run_id,True,"WorkBuddy/New")
        self.assertEqual(taken["canceled_batches"],1)
        fresh=lease_jobs(self.workspace,run_id,20,owner)
        self.assertNotEqual(old["batch_id"],fresh["batch_id"])
        with self.assertRaisesRegex(ValueError,"失效"):
            ingest_results(self.workspace,run_id,document(run_id,old["jobs"]),old["batch_id"],taken["worker_id"])
        states={row["batch_id"]:row["state"] for row in list_batches(self.workspace,run_id)}
        self.assertEqual(states[old["batch_id"]],"ABANDONED")

    def test_upgrade_v2_inflight_result_is_accepted_once(self):
        run_id=create_run(self.workspace,"US","9201","Legacy")
        conn=connect(self.workspace); token="legacy-token"
        with transaction(conn):
            job=conn.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY job_id LIMIT 1",(run_id,)).fetchone()
            conn.execute("UPDATE jobs SET state='LEASED',attempts=1,lease_token=?,lease_until='2999-01-01T00:00:00+00:00' WHERE job_id=?",(token,job["job_id"]))
        indicator=job["indicator"]; conn.close()
        legacy={"schema_version":"2","run_id":run_id,"results":[{
            "job_id":job["job_id"],"attempt":1,"lease_token":token,"status":"success",
            "payload":trend_payload([9000]*24) if indicator=="SalesCount" else trend_payload([20]*24),"error":""}]}
        result=ingest_results(self.workspace,run_id,legacy)
        self.assertTrue(result["legacy_protocol"]); self.assertEqual(result["accepted"],1)

    def test_xljpack_dry_run_commit_duplicate_and_conflict(self):
        run_id=create_run(self.workspace,"DE","801","Pack")
        self.ingest_trends(run_id,[9000]*24,[20+i*.1 for i in range(24)])
        pack=export_pack(self.workspace,os.path.join(self.workspace,"transfer.xljpack"),[run_id])
        same=import_pack(self.workspace,pack,False); self.assertEqual(same["skipped"],[run_id])
        with zipfile.ZipFile(pack) as zf:
            data=zf.read("data.jsonl").decode("utf-8")
        self.assertNotIn(self.workspace,data)
        target=tempfile.TemporaryDirectory()
        try:
            dry=import_pack(target.name,pack,False); self.assertEqual(dry["new"],[run_id]); self.assertFalse(dry["committed"])
            done=import_pack(target.name,pack,True); self.assertTrue(done["committed"])
            repeat=import_pack(target.name,pack,False); self.assertEqual(repeat["skipped"],[run_id])
        finally: target.cleanup()

    def test_xljpack_rejects_unknown_runs_tables_and_columns(self):
        import hashlib
        run_id=create_run(self.workspace,"US","10001","Pack Safe")
        self.ingest_trends(run_id,[9000]*24,[20+i*.1 for i in range(24)])
        with self.assertRaisesRegex(ValueError,"不存在"):
            export_pack(self.workspace,os.path.join(self.workspace,"missing.xljpack"),["does-not-exist"])
        good=export_pack(self.workspace,os.path.join(self.workspace,"good.xljpack"),[run_id])
        with zipfile.ZipFile(good) as zf:
            manifest=json.loads(zf.read("manifest.json")); lines=[json.loads(x) for x in zf.read("data.jsonl").decode().splitlines()]
        lines[0]["table"]="runs;DROP TABLE runs"
        payload=("\n".join(json.dumps(x,ensure_ascii=False,separators=(",",":"),sort_keys=True) for x in lines)+"\n").encode()
        manifest["data_sha256"]=hashlib.sha256(payload).hexdigest()
        bad=os.path.join(self.workspace,"bad.xljpack")
        with zipfile.ZipFile(bad,"w",zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json",json.dumps(manifest)); zf.writestr("data.jsonl",payload)
        with self.assertRaisesRegex(ValueError,"不允许"):
            import_pack(os.path.join(self.workspace,"other"),bad,False)

    def test_cli_bom_protocol_v2(self):
        run_id=create_run(self.workspace,"US","901","CLI")
        jobs=lease_jobs(self.workspace,run_id)["jobs"]
        path=os.path.join(self.workspace,"result.json")
        with open(path,"w",encoding="utf-8-sig") as f: json.dump(document(run_id,jobs,{"SalesCount":trend_payload([9000]*24),"AvgPrice":trend_payload([20+i*.1 for i in range(24)])}),f)
        self.assertEqual(cli_main(["ingest","--run",run_id,"--input",path,"--workspace",self.workspace]),0)

    def test_cli_response_file_and_volume_alias(self):
        response=os.path.join(self.workspace,"start-response.json")
        code=cli_main(["start","--site","US","--root-node","902","--category","CLI Response",
                       "--min-avg-sales","43210","--workspace",self.workspace,
                       "--response-file",response])
        self.assertEqual(code,0); self.assertTrue(os.path.isfile(response))
        with open(response,encoding="utf-8") as handle: started=json.load(handle)
        self.assertEqual(started["status"],"RUNNING")
        self.assertEqual(status(self.workspace,started["run_id"])["policy"]["min_avg_sales"],43210)
        self.assertFalse(os.path.exists(response+".tmp"))

        failed=os.path.join(self.workspace,"failed-response.json")
        code=cli_main(["start","--site","US","--root-node","903","--category","Invalid",
                       "--max-level","0","--workspace",self.workspace,
                       "--response-file",failed])
        self.assertEqual(code,2)
        with open(failed,encoding="utf-8") as handle: error=json.load(handle)
        self.assertFalse(error["ok"]); self.assertEqual(error["exit_code"],2)
        self.assertIn("最大层级",error["error"])

        parse_failed=os.path.join(self.workspace,"parse-failed-response.json")
        with self.assertRaises(SystemExit) as raised:
            cli_main(["start","--site","US","--root-node","904","--category","Bad Flag",
                      "--unknown-parameter","1","--response-file",parse_failed])
        self.assertEqual(raised.exception.code,2)
        with open(parse_failed,encoding="utf-8") as handle: parse_error=json.load(handle)
        self.assertFalse(parse_error["ok"]); self.assertIn("--unknown-parameter",parse_error["error"])

    @unittest.skip("upstream standalone panel subprocess is excluded from the Workbench adapter")
    def test_panel_lifecycle_is_background_idempotent_and_uses_next_port(self):
        preferred = 19000 + (os.getpid() % 500)
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", preferred)); blocker.listen(1)
        opened = None
        try:
            opened = open_panel(self.workspace, port=preferred, open_browser=False, timeout=20)
            self.assertEqual(opened["action"], "started")
            self.assertNotEqual(opened["port"], preferred)
            current = panel_status(self.workspace)
            self.assertTrue(current["running"]); self.assertEqual(current["version"], "4.2.0-beta.3")
            again = open_panel(self.workspace, port=preferred, open_browser=False, timeout=5)
            self.assertEqual(again["action"], "already_running"); self.assertEqual(again["pid"], opened["pid"])
        finally:
            blocker.close()
            if opened:
                stopped = stop_panel(self.workspace)
                self.assertEqual(stopped["action"], "stopped")
        self.assertFalse(panel_status(self.workspace)["running"])

    @unittest.skip("upstream standalone panel subprocess is excluded from the Workbench adapter")
    def test_panel_instruction_presets_and_refresh_are_idempotent(self):
        preferred = 19500 + (os.getpid() % 400)
        opened = open_panel(self.workspace, port=preferred, open_browser=False, timeout=20)
        try:
            with urllib.request.urlopen(opened["url"], timeout=5) as response:
                page = response.read().decode("utf-8")
            self.assertIn("生成自然语言指令", page); self.assertIn("实时运行汇总", page)
            with open(os.path.join(self.workspace,"panel.json"),encoding="utf-8") as handle: csrf=json.load(handle)["token"]
            body={"name":"US Standard","site":"US","root_node_id":"9301","category":"Refresh",
                  "operator_name":"Bob","report_dir":"","policy":{"min_avg_sales":10000,"max_level":12,
                  "sales_decline_tolerance":.05,"price_decline_tolerance":.1,"min_r_squared":.15,
                  "batch_size":20,"max_attempts":3,"max_data_lag_months":2}}
            def post(path,value):
                req=urllib.request.Request(opened["url"]+path,data=json.dumps(value).encode(),method="POST",
                                           headers={"Content-Type":"application/json","X-XLJ-Panel-Token":csrf})
                return json.loads(urllib.request.urlopen(req,timeout=5).read())
            first=post("api/preset/save",body); second=post("api/preset/save",body)
            self.assertEqual(first["preset"]["preset_id"],second["preset"]["preset_id"])
            self.assertEqual(len(list_presets(self.workspace)),1)
            instruction=post("api/instruction",body)["instruction"]
            self.assertIn("站点 US",instruction); self.assertIn("自动闭环",instruction)
            self.assertIn('"schema_version":"xlj-launch/1"',instruction)
            self.assertIn('"volume_threshold":10000.0',instruction)
            urllib.request.urlopen(opened["url"],timeout=5).read(); urllib.request.urlopen(opened["url"],timeout=5).read()
            self.assertEqual(len(list_presets(self.workspace)),1)
        finally:
            stop_panel(self.workspace)

    def test_start_reuses_active_run_and_pulse_is_visible(self):
        first=start_run(self.workspace,"US","9401","Natural",policy={"batch_size":2})
        second=start_run(self.workspace,"US","9401","Natural",policy={"batch_size":2})
        self.assertFalse(first["reused"]); self.assertTrue(second["reused"])
        self.assertEqual(first["run_id"],second["run_id"])
        batch=lease_jobs(self.workspace,first["run_id"],2)
        pulse_batch(self.workspace,first["run_id"],batch["batch_id"],completed=1,current_node_id="9401",stage="CALLING_MCP")
        current=status(self.workspace,first["run_id"])
        self.assertEqual(current["batch_progress"]["processed"],1)
        self.assertEqual(current["batch_progress"]["current_node_id"],"9401")

    def test_report_contract_rejects_downgrade(self):
        with self.assertRaisesRegex(RuntimeError,"报告功能契约"):
            validate_report_contract("<html>old report</html>")


if __name__ == "__main__": unittest.main()
