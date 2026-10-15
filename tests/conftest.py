import os
import tempfile
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / '.test-temp'
TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
for variable in ('TEMP', 'TMP', 'TMPDIR'):
    os.environ[variable] = str(TEST_TEMP_ROOT)
tempfile.tempdir = str(TEST_TEMP_ROOT)

os.environ['TAG_STORAGE']=tempfile.mkdtemp(
    prefix='product-workbench-tests-',
    dir=TEST_TEMP_ROOT,
)
import pytest
from backend.db import Base,engine,init_db


def pytest_configure(config):
    # Keep pytest's tmp_path/tmpdir fixtures on D: as well as TAG_STORAGE.
    config.option.basetemp = str(TEST_TEMP_ROOT / 'pytest')

@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine);init_db()
    yield

@pytest.fixture
def dataset():
    from backend.importer import import_file
    source=Path(__file__).resolve().parent/'fixtures'/'sample_10_rows.xlsx'
    return import_file(source.read_bytes(),source.name)
