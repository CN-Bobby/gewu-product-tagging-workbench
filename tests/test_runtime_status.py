from backend.runtime_status import runtime_status


def test_dev_online_status_matches_run_center_contract(monkeypatch):
    monkeypatch.setenv("GEWU_PROFILE", "dev")
    monkeypatch.setenv("GEWU_INSTANCE_ID", "DEV")
    monkeypatch.delenv("GEWU_DEV_EXTERNAL_EFFECTS", raising=False)
    monkeypatch.delenv("GEWU_TEST_EXTERNAL_EFFECTS", raising=False)

    status = runtime_status()

    assert status["profile"] == "dev"
    assert status["instance_id"] == "DEV"
    assert status["external_effects"]["policy"] == "live_validation"
    assert status["external_effects"]["services"]["model_api"] is True


def test_dev_offline_status_reads_only_dev_policy(monkeypatch):
    monkeypatch.setenv("GEWU_PROFILE", "dev")
    monkeypatch.setenv("GEWU_INSTANCE_ID", "DEV")
    monkeypatch.setenv("GEWU_DEV_EXTERNAL_EFFECTS", "offline")
    monkeypatch.setenv("GEWU_TEST_EXTERNAL_EFFECTS", "live_validation")

    status = runtime_status()

    assert status["external_effects"]["policy"] == "offline"
    assert status["external_effects"]["services"]["model_api"] is False


def test_invalid_policy_is_not_silently_promoted_to_online(monkeypatch):
    monkeypatch.setenv("GEWU_PROFILE", "dev")
    monkeypatch.setenv("GEWU_INSTANCE_ID", "DEV")
    monkeypatch.setenv("GEWU_DEV_EXTERNAL_EFFECTS", "unexpected")

    status = runtime_status()

    assert status["external_effects"]["policy"] == "invalid"
    assert status["external_effects"]["services"]["model_api"] is False


def test_test_status_uses_test_instance_and_policy(monkeypatch):
    monkeypatch.setenv("GEWU_PROFILE", "test")
    monkeypatch.setenv("GEWU_INSTANCE_ID", "TEST")
    monkeypatch.delenv("GEWU_DEV_EXTERNAL_EFFECTS", raising=False)
    monkeypatch.setenv("GEWU_TEST_EXTERNAL_EFFECTS", "offline")

    status = runtime_status()

    assert status["profile"] == "test"
    assert status["instance_id"] == "TEST"
    assert status["external_effects"]["policy"] == "offline"


def test_runtime_status_http_route_is_registered(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import app

    monkeypatch.setenv("GEWU_PROFILE", "dev")
    monkeypatch.setenv("GEWU_INSTANCE_ID", "DEV")
    monkeypatch.delenv("GEWU_DEV_EXTERNAL_EFFECTS", raising=False)

    response = TestClient(app).get("/api/runtime/status")

    assert response.status_code == 200
    assert response.json()["profile"] == "dev"
    assert response.json()["instance_id"] == "DEV"
    assert response.json()["external_effects"]["policy"] == "live_validation"
