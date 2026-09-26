from __future__ import annotations

import io
import json
import threading
import time

from PIL import Image
import pytest

from backend import amazon_scraper
from backend.importer import parse

ASIN1 = f"B{1:09d}"
ASIN2 = f"B{2:09d}"
ASIN3 = f"B{3:09d}"


def png_bytes(color: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(out, format="PNG")
    return out.getvalue()


def wait_for(service, job_id, expected, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get(job_id)
        if job["status"] in expected:
            return job
        time.sleep(0.01)
    raise AssertionError(f"任务未进入预期状态：{service.get(job_id)}")


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("条件未在时限内满足")


def test_parse_asins_and_marketplace_urls_deduplicates_by_marketplace():
    parsed = amazon_scraper.parse_inputs(
        f"{ASIN1}\nhttps://www.amazon.co.uk/dp/{ASIN1}?ref=tracking\n"
        f"https://www.amazon.com/dp/{ASIN1}?tag=private-value\nhttps://example.test/dp/{ASIN2}",
        "US",
    )
    assert [(item["marketplace"], item["asin"]) for item in parsed["items"]] == [
        ("US", ASIN1), ("UK", ASIN1)
    ]
    assert parsed["duplicates_removed"] == 1
    assert len(parsed["invalid"]) == 1
    assert all("ref=" not in item["url"] and "tag=" not in item["url"] for item in parsed["items"])


def test_parse_rejects_oversize_payload_and_more_than_100_unique_items():
    with pytest.raises(ValueError, match="128 KiB"):
        amazon_scraper.parse_inputs("x" * (128 * 1024 + 1))
    too_many = " ".join(f"B{i:09d}" for i in range(1, 102))
    with pytest.raises(ValueError, match="100"):
        amazon_scraper.parse_inputs(too_many)


def test_checkpoint_revision_and_generation_reject_late_write(tmp_path):
    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=lambda *_: {"status": "ready"})
    job = {"id": "b" * 32, "status": "running", "attempt_generation": 0, "revision": 0,
           "items": [], "created_at": 1, "updated_at": 1}
    assert service._save(job)
    stale = service._read(job["id"])
    current = service._read(job["id"])
    current.update(status="cancelled", attempt_generation=1)
    assert service._save(current, expected_generation=0)
    stale["status"] = "completed"
    assert not service._save(stale, expected_generation=0)
    persisted = service._read(job["id"])
    assert persisted["revision"] == 2
    assert persisted["attempt_generation"] == 1
    assert persisted["status"] == "cancelled"


def test_public_job_and_preview_report_revision_and_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(amazon_scraper, "runtime_status", lambda: {"available": True, "proxy_count": 0})
    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=lambda *_: {"status": "ready"})
    job_id = "d" * 32
    service._save({"id": job_id, "status": "paused", "items": [], "profile": "stable",
                   "created_at": 1, "updated_at": 1})
    public = service.get(job_id)
    assert public["revision"] == 1
    assert public["max_items_per_job"] == 100
    assert public["max_input_bytes"] == 128 * 1024
    assert public["max_active_page_fetches"] == 4
    assert public["max_concurrent_jobs"] == 2
    assert public["profile_page_caps"] == {"stable": 1, "balanced": 2, "fast": 4}
    preview = service.preview(ASIN1)
    assert preview["max_items_per_job"] == 100
    assert preview["max_input_bytes"] == 128 * 1024
    assert preview["max_active_page_fetches"] == 4
    assert preview["max_concurrent_jobs"] == 2
    assert preview["profile_page_caps"] == {"stable": 1, "balanced": 2, "fast": 4}


def test_request_interval_is_measured_from_actual_admitted_starts(tmp_path):
    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=lambda *_: {"status": "ready"})
    cancel = threading.Event()
    starts = []
    assert service._dispatch_with_interval("ab" * 16, 0.03, 0.03, cancel,
        lambda: starts.append(time.monotonic()) or True)[0]
    assert service._dispatch_with_interval("ab" * 16, 0.03, 0.03, cancel,
        lambda: starts.append(time.monotonic()) or True)[0]
    assert starts[1] - starts[0] >= 0.02


def test_legacy_over_100_item_job_remains_readable_and_retryable(tmp_path):
    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=lambda *_: {"status": "ready"})
    job_id = "c" * 32
    items = [{"marketplace": "US", "asin": f"B{i:09d}", "url": "https://www.amazon.com/dp/B000000001",
              "status": "pending", "title": "", "image_count": 0, "error": ""} for i in range(1, 102)]
    folder = tmp_path / "jobs" / job_id
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({"id": job_id, "status": "interrupted", "profile": "stable",
        "created_at": 1, "updated_at": 1, "items": items, "output_file": "result.xlsx"}), encoding="utf-8")
    (folder / "result.xlsx").write_bytes(b"old result")
    service.shutting_down = True  # Keep retry queued so this remains a storage compatibility check.
    assert service.get(job_id)["total"] == 101
    assert service.output_path(job_id).is_file()
    retried = service.retry(job_id)
    assert retried["total"] == 101
    assert retried["status"] == "queued"
    assert service._read(job_id)["attempt_generation"] == 1


def test_three_group_queue_obeys_two_group_slots_and_four_page_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(amazon_scraper, "runtime_status", lambda: {"available": True, "proxy_count": 0})
    monkeypatch.setattr(amazon_scraper.random, "uniform", lambda low, high: 0)
    release = threading.Event()
    four_active = threading.Event()
    guard = threading.Lock()
    active = 0
    maximum = 0

    def engine(item, proxy, cancel):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
            if maximum == 4:
                four_active.set()
        release.wait(4)
        with guard:
            active -= 1
        return {"status": "ready", "title": "ok", "images": []}

    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=engine)
    service.shutting_down = True  # Create all ids before releasing the injected engine.
    jobs = []
    for offset in range(4):
        items = " ".join(f"B{offset * 10 + i:09d}" for i in range(1, 5))
        job = service.start(items, profile="fast")
        jobs.append(job["id"])
    launched_from_queue = []
    launch = service._launch
    def record_queue_launch(job_id):
        if job_id in jobs[2:]:
            launched_from_queue.append(job_id)
        return launch(job_id)
    service._launch = record_queue_launch
    service.shutting_down = False
    with service.lock:
        service._dispatch_queued_locked()
    wait_until(lambda: service.get(jobs[2])["queue_position"] == 1 and
               service.get(jobs[3])["queue_position"] == 2)
    assert four_active.wait(2)
    assert maximum == 4
    assert service.get(jobs[0])["active_job_count"] == 2
    release.set()
    for job_id in jobs:
        wait_for(service, job_id, {"completed"})
    assert launched_from_queue[:2] == jobs[2:]
    assert maximum <= 4


def test_restriction_cooldown_rebuilds_pending_and_keeps_single_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(amazon_scraper, "runtime_status", lambda: {"available": True, "proxy_count": 0})
    monkeypatch.setattr(amazon_scraper.random, "uniform", lambda low, high: 0)
    calls = {}
    cooldowns = []

    def engine(item, proxy, cancel):
        count = calls.get(item["asin"], 0) + 1
        calls[item["asin"]] = count
        if item["asin"] == ASIN1:
            return {"status": "restricted", "error": "HTTP 503"}
        if count == 1 and item["asin"] in (ASIN2, ASIN3, f"B{4:09d}"):
            wait_until(lambda: bool(service._restricted_jobs), timeout=2)
        if count == 1 and item["asin"] in (ASIN3, f"B{4:09d}"):
            return {"status": "cancelled", "error": "restriction stopped peer"}
        if item["asin"] == ASIN2 or count > 1:
            return {"status": "ready", "title": "ok", "images": []}
        return {"status": "failed", "error": "unexpected"}

    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=engine)
    service._cooldown = lambda cancel: cooldowns.append(True)
    fourth = f"B{4:09d}"
    job = service.start(f"{ASIN1} {ASIN2} {ASIN3} {fourth}", profile="fast")
    complete = wait_for(service, job["id"], {"completed_with_errors"})
    states = {item["asin"]: item["status"] for item in complete["items"]}
    assert states[ASIN1] == "needs_attention"
    assert states[ASIN2] == "ready"
    assert states[ASIN3] == "ready"
    assert states[fourth] == "ready"
    assert cooldowns == [True]
    assert complete["restricted_results"] == 0
    assert calls[ASIN3] == 2 and calls[fourth] == 2


def test_cancel_during_cooldown_does_not_reopen_restriction_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(amazon_scraper, "runtime_status", lambda: {"available": True, "proxy_count": 0})
    monkeypatch.setattr(amazon_scraper.random, "uniform", lambda low, high: 0)
    cooldown_started = threading.Event()
    calls = []

    def engine(item, proxy, cancel):
        calls.append(item["asin"])
        return {"status": "restricted", "error": "HTTP 429"}

    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=engine)
    def held_cooldown(cancel):
        cooldown_started.set()
        cancel.wait(3)
    service._cooldown = held_cooldown
    job = service.start(f"{ASIN1} {ASIN2}", profile="stable")
    assert cooldown_started.wait(2)
    cancelled = service.cancel(job["id"])
    assert cancelled["status"] == "cancelled"
    assert calls == [ASIN1]
    assert service._fetch_active == 0


def test_worker_start_signal_is_blocked_when_restriction_gate_closed(tmp_path, monkeypatch):
    from backend.agent import runtime

    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=None)
    job_id = "e" * 32
    service._save({"id": job_id, "status": "running", "profile": "stable", "attempt_generation": 0,
                   "revision": 0, "items": [], "created_at": 1, "updated_at": 1})
    service._restricted_jobs.add(job_id)

    class FakeJob:
        def __init__(self): self.released = False
        def create(self): return True
        def assign(self, _pid): return True
        def release(self): self.released = True

    class FakeChild:
        pid = 123
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO()
            self.returncode = None
        def poll(self): return self.returncode
        def kill(self): self.returncode = -9
        def wait(self, timeout=None): return self.returncode

    child = FakeChild()
    fake_job = FakeJob()
    monkeypatch.setattr(runtime, "ProcessJob", lambda: fake_job)
    monkeypatch.setattr(amazon_scraper.subprocess, "Popen", lambda *args, **kwargs: child)
    result = service._fetch_in_worker(job_id, {"marketplace": "US", "asin": ASIN1,
        "url": f"https://www.amazon.com/dp/{ASIN1}"}, None, threading.Event())
    assert result["status"] == "cancelled"
    assert child.stdin.getvalue() == ""
    assert fake_job.released


def test_cancel_persists_cancelling_fence_until_child_is_reaped(tmp_path):
    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=lambda *_: {"status": "ready"})
    job_id = "f" * 32
    service._save({"id": job_id, "status": "running", "profile": "stable", "attempt_generation": 0,
                   "revision": 0, "created_at": 1, "updated_at": 1,
                   "items": [{"marketplace": "US", "asin": ASIN1, "url": f"https://www.amazon.com/dp/{ASIN1}",
                              "status": "scraping", "title": "", "image_count": 0, "error": ""}]})
    observed = []

    class FakeJob:
        def release(self): pass

    class FakeChild:
        def __init__(self): self.dead = False
        def poll(self): return 0 if self.dead else None
        def terminate(self):
            snapshot = service._read(job_id)
            observed.append((snapshot["status"], snapshot["attempt_generation"]))
            self.dead = True
        def kill(self): self.dead = True
        def wait(self, timeout=None): return 0

    child = FakeChild()
    service._child_processes[job_id] = {(child, FakeJob())}
    service.active_job_ids.add(job_id)
    service.cancel_events[job_id] = threading.Event()
    cancelled = service.cancel(job_id)
    assert observed == [("cancelling", 1)]
    assert cancelled["status"] == "cancelled"
    assert cancelled["pending"] == 1
    assert service._read(job_id)["attempt_generation"] == 1


def test_cancel_joins_worker_created_between_spawn_and_registry_insert(tmp_path, monkeypatch):
    from backend.agent import runtime

    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=None)
    job_id = "9" * 32
    service._save({"id": job_id, "status": "running", "profile": "stable", "attempt_generation": 0,
                   "revision": 0, "created_at": 1, "updated_at": 1, "items": []})
    service.active_job_ids.add(job_id)
    cancel_event = threading.Event()
    service.cancel_events[job_id] = cancel_event
    spawned = threading.Event()
    allow_popen_return = threading.Event()

    class FakeJob:
        def create(self): return True
        def assign(self, _pid): return True
        def release(self): pass

    class FakeChild:
        pid = 456
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO()
            self.returncode = None
        def poll(self): return self.returncode
        def kill(self): self.returncode = -9
        def terminate(self): self.returncode = -15
        def wait(self, timeout=None): return self.returncode

    child = FakeChild()
    def delayed_popen(*args, **kwargs):
        spawned.set()
        allow_popen_return.wait(2)
        return child

    monkeypatch.setattr(runtime, "ProcessJob", FakeJob)
    monkeypatch.setattr(amazon_scraper.subprocess, "Popen", delayed_popen)
    worker = threading.Thread(target=lambda: service._fetch_in_worker(
        job_id, {"marketplace": "US", "asin": ASIN1, "url": f"https://www.amazon.com/dp/{ASIN1}"},
        None, cancel_event), daemon=True)
    service.threads[job_id] = worker
    worker.start()
    assert spawned.wait(1)
    timer = threading.Timer(0.05, allow_popen_return.set)
    timer.start()
    cancelled = service.cancel(job_id)
    timer.join()
    assert cancelled["status"] == "cancelled"
    assert not worker.is_alive()
    assert child.poll() is not None
    assert child.stdin.getvalue() == ""


def test_shutdown_interrupts_queued_and_active_with_one_deadline(tmp_path):
    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=lambda *_: {"status": "ready"})
    queued_id, active_id = "1" * 32, "2" * 32
    base = {"profile": "stable", "created_at": 1, "updated_at": 1,
            "items": [{"marketplace": "US", "asin": ASIN1, "url": f"https://www.amazon.com/dp/{ASIN1}",
                       "status": "pending", "title": "", "image_count": 0, "error": ""}]}
    service._save({**base, "id": queued_id, "status": "queued", "queued_at": 1})
    active = {**base, "id": active_id, "status": "running"}
    active["items"] = [dict(active["items"][0], status="scraping")]
    service._save(active)
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, daemon=True)
    thread.start()
    service.active_job_ids.add(active_id)
    service.threads[active_id] = thread
    service.cancel_events[active_id] = stop
    started = time.monotonic()
    service.stop_all()
    elapsed = time.monotonic() - started
    assert elapsed < 2
    for job_id in (queued_id, active_id):
        checkpoint = service._read(job_id)
        assert checkpoint["status"] == "interrupted"
        assert checkpoint["attempt_generation"] == 1
        assert checkpoint["items"][0]["status"] == "pending"
    assert not thread.is_alive()


def test_product_url_validation_rejects_http_and_non_amazon_hosts():
    parsed = amazon_scraper.parse_inputs(f"http://www.amazon.com/dp/{ASIN1}\nhttps://amazon.example/dp/{ASIN2}")
    assert parsed["items"] == []
    assert len(parsed["invalid"]) == 2


def test_safe_image_url_blocks_untrusted_hosts_and_schemes():
    assert amazon_scraper._safe_image_url("https://m.media-amazon.com/images/I/example.jpg")
    assert not amazon_scraper._safe_image_url("http://m.media-amazon.com/images/I/example.jpg")
    assert not amazon_scraper._safe_image_url("https://media-amazon.com.evil.test/image.jpg")
    assert not amazon_scraper._safe_image_url("https://user:pass@m.media-amazon.com/image.jpg")


def test_gallery_keeps_page_order_removes_size_suffixes_and_deduplicates():
    class Page:
        def evaluate(self, _script):
            return [
                "https://m.media-amazon.com/images/I/first._AC_SX300_.jpg",
                "https://m.media-amazon.com/images/I/second._AC_SX300_.jpg",
                "https://m.media-amazon.com/images/I/first._AC_SX500_.jpg",
            ]

    assert amazon_scraper._gallery_urls(Page()) == [
        "https://m.media-amazon.com/images/I/first.jpg",
        "https://m.media-amazon.com/images/I/second.jpg",
    ]


def test_successful_job_exports_excel_that_imports_as_ordered_images(tmp_path, monkeypatch):
    monkeypatch.setattr(amazon_scraper, "runtime_status", lambda: {"available": True, "proxy_count": 0})
    monkeypatch.setattr(amazon_scraper.random, "uniform", lambda low, high: 0)

    def engine(item, proxy, cancel):
        return {"status": "ready", "title": "Test swimsuit", "images": [(png_bytes("red"), "image/png"), (png_bytes("blue"), "image/png")]}

    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=engine)
    job = service.start(ASIN1, profile="stable")
    complete = wait_for(service, job["id"], {"completed"})
    assert complete["can_download"]
    parsed = parse(service.output_path(job["id"]).read_bytes())
    assert parsed["headers"] == ["ASIN", "商品标题", "图片 1", "图片 2"]
    assert parsed["rows"][0]["fields"]["asin"] == ASIN1
    assert parsed["rows"][0]["fields"]["title"] == "Test swimsuit"
    assert len(parsed["rows"][0]["images"]) == 2


def test_failed_item_can_retry_and_rebuild_output(tmp_path, monkeypatch):
    monkeypatch.setattr(amazon_scraper, "runtime_status", lambda: {"available": True, "proxy_count": 0})
    monkeypatch.setattr(amazon_scraper.random, "uniform", lambda low, high: 0)
    calls = 0

    def engine(item, proxy, cancel):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"status": "failed", "error": "temporary failure"}
        return {"status": "ready", "title": "Recovered", "images": [(png_bytes("green"), "image/png")]}

    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=engine)
    job = service.start(ASIN1, profile="stable")
    wait_for(service, job["id"], {"completed_with_errors"})
    service.retry(job["id"])
    complete = wait_for(service, job["id"], {"completed"})
    assert complete["items"][0]["image_count"] == 1
    assert parse(service.output_path(job["id"]).read_bytes())["rows"][0]["fields"]["title"] == "Recovered"


def test_three_consecutive_restrictions_pause_and_keep_pending_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(amazon_scraper, "runtime_status", lambda: {"available": True, "proxy_count": 0})
    monkeypatch.setattr(amazon_scraper.random, "uniform", lambda low, high: 0)
    cooldowns = []

    def engine(item, proxy, cancel):
        return {"status": "restricted", "error": "HTTP 503"}

    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=engine)
    service._cooldown = lambda cancel: cooldowns.append(True)
    job = service.start(f"{ASIN1} {ASIN2} {ASIN3}", profile="stable")
    paused = wait_for(service, job["id"], {"paused"})
    assert paused["restricted_results"] == 3
    assert paused["failed"] == 3
    assert not paused["can_download"]
    assert cooldowns == [True, True]


def test_recovery_marks_inflight_items_resumable(tmp_path):
    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=lambda *_: {"status": "ready"})
    job = {"id": "a" * 32, "status": "running", "profile": "balanced", "created_at": 1,
           "updated_at": 1, "items": [{"marketplace": "US", "asin": ASIN1, "url": f"https://www.amazon.com/dp/{ASIN1}",
                                          "status": "scraping", "title": "", "image_count": 0, "error": ""}],
           "invalid": [], "duplicates_removed": 0, "restricted_results": 0, "output_file": None, "proxy_count": 0}
    service._save(job)
    recovered = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=lambda *_: {"status": "ready"})
    assert recovered.get(job["id"])["status"] == "interrupted"
    assert recovered.get(job["id"])["pending"] == 1


def test_job_checkpoint_does_not_store_query_or_proxy_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(amazon_scraper, "runtime_status", lambda: {"available": True, "proxy_count": 1})
    monkeypatch.setattr(amazon_scraper, "_proxy_pool", lambda: ["http://user:secret@proxy.example:8080"])
    monkeypatch.setattr(amazon_scraper.random, "uniform", lambda low, high: 0)

    def engine(item, proxy, cancel):
        return {"status": "ready", "title": "Safe", "images": [(png_bytes("black"), "image/png")]}

    service = amazon_scraper.AmazonScraperService(tmp_path / "jobs", engine=engine)
    job = service.start(f"{ASIN1} https://example.test/item?token=private-value", profile="stable")
    complete = wait_for(service, job["id"], {"completed"})
    assert complete["invalid"] == ["example.test/item"]
    checkpoint = (tmp_path / "jobs" / job["id"] / "job.json").read_text(encoding="utf-8")
    assert "private-value" not in checkpoint
    assert "user:secret" not in checkpoint
    assert "token=" not in checkpoint
