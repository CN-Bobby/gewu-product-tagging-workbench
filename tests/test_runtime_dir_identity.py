"""The runtime directory's role identity: worker versus coordinator.

This used to live in a suite about the legacy conversation runtime binding. The binding is retired;
the runtime directory is not — batch workers and every agent session are launched through it, so
what it puts in the environment and the prompt is still production surface.
"""
import pytest

from backend.harness import runtime_dir


def test_the_worker_and_coordinator_runtimes_keep_their_own_identity(dataset):
    did=dataset['id']
    coordinator=runtime_dir(did,'chat-explicit',role='coordinator')
    coordinator_config=(coordinator/'reasonix.toml').read_text(encoding='utf-8')
    coordinator_prompt=(coordinator/'PRODUCT_ANALYST_AGENT.md').read_text(encoding='utf-8')
    assert 'TAG_RUN_ID' not in coordinator_config and 'TAG_PRODUCT_ID' not in coordinator_config
    assert '持续对话协调员' in coordinator_prompt
    worker=runtime_dir(did,'run-explicit','product-one',role='worker')
    worker_config=(worker/'reasonix.toml').read_text(encoding='utf-8')
    worker_prompt=(worker/'PRODUCT_ANALYST_AGENT.md').read_text(encoding='utf-8')
    assert 'TAG_RUN_ID' in worker_config and 'TAG_PRODUCT_ID' in worker_config
    assert '执行打标' in worker_prompt


def test_a_worker_runtime_without_a_product_is_refused(dataset):
    with pytest.raises(ValueError):runtime_dir(dataset['id'],'run-broken',role='worker')


def test_generated_config_bounds_mcp_calls_and_denies_run_skill(dataset):
    did=dataset['id']
    path=runtime_dir(did,'chat-explicit',role='coordinator')
    config=(path/'reasonix.toml').read_text(encoding='utf-8')
    assert 'call_timeout_seconds = 300' in config
    assert 'run_skill' in config
    prompt=(path/'PRODUCT_ANALYST_AGENT.md').read_text(encoding='utf-8')
    assert 'mcp__product_workbench__route_tagging_skill' in prompt
    assert '先调用 route_tagging_skill' not in prompt
