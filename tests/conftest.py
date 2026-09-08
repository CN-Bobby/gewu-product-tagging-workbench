import os
import tempfile
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['TAG_STORAGE']=tempfile.mkdtemp(prefix='product-workbench-tests-')
import pytest
from backend.db import Base,engine,init_db

@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine);init_db()
    yield

@pytest.fixture
def dataset():
    from backend.importer import import_file
    source=Path(__file__).resolve().parents[1]/'测试10行.xlsx'
    return import_file(source.read_bytes(),source.name)
