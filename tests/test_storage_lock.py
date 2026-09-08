import pytest
from backend.storage_lock import exclusive_storage


def test_storage_rejects_second_owner_and_releases(tmp_path):
    with exclusive_storage(tmp_path):
        with pytest.raises(RuntimeError,match='正在使用'):
            with exclusive_storage(tmp_path):pytest.fail('second owner acquired data')
    with exclusive_storage(tmp_path):pass
