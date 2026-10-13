import os
import ipaddress
import socket
import tempfile
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['TAG_STORAGE']=tempfile.mkdtemp(prefix='product-workbench-tests-')
os.environ['GEWU_PROFILE']='test'
os.environ['GEWU_TEST_EXTERNAL_EFFECTS']='offline'
import pytest
from backend.db import Base,engine,init_db,cache_engine,VisionCache,TextCache
from backend import external_effects

@pytest.fixture(autouse=True)
def isolated_credential_vault(tmp_path,monkeypatch):
    """Keep every test credential out of the current user's real DPAPI vault."""
    from backend import credential_vault
    from backend import runtime_isolation
    path=tmp_path/'isolated-run-center'/'credentials.dpapi'
    monkeypatch.setattr(credential_vault,'_vault_path',lambda:path)
    monkeypatch.setattr(credential_vault,'_protect',lambda data:b'TEST-VAULT:'+data[::-1])
    monkeypatch.setattr(credential_vault,'_unprotect',
        lambda blob:blob[len(b'TEST-VAULT:'):][::-1] if blob.startswith(b'TEST-VAULT:') else (_ for _ in ()).throw(ValueError('bad test envelope')))
    monkeypatch.setattr(runtime_isolation,'_runtime_home',
                        lambda dataset_id:(tmp_path/'isolated-reasonix'/dataset_id).resolve())
    # Legacy unit tests exercise mocked provider code. The socket guard below guarantees that
    # even a missing mock cannot reach a non-loopback service. Dedicated policy tests restore the
    # production gate and verify DEV/TEST refusals explicitly.
    monkeypatch.setattr(external_effects,'effects_allowed',lambda service=None: True)
    yield

@pytest.fixture(autouse=True)
def reset_agent_shutdown_gate():
    """A TestClient lifespan must not leave its shutdown gate closed for the next test."""
    from backend import agent_application
    agent_application.start()
    yield
    agent_application.start()

@pytest.fixture(autouse=True)
def loopback_network_only(monkeypatch):
    """Prevent accidental internet calls while allowing local fake servers and app tests."""
    def is_loopback(host):
        if isinstance(host,bytes):host=host.decode('ascii','ignore')
        if str(host).casefold()=='localhost':return True
        try:return ipaddress.ip_address(str(host).split('%',1)[0]).is_loopback
        except ValueError:return False

    original_getaddrinfo=socket.getaddrinfo
    original_connect=socket.socket.connect
    original_connect_ex=socket.socket.connect_ex
    def guarded_getaddrinfo(host,*args,**kwargs):
        if host is not None and not is_loopback(host):
            raise PermissionError('Automated tests may resolve loopback hosts only')
        return original_getaddrinfo(host,*args,**kwargs)
    def guarded_connect(sock,address):
        if isinstance(address,(tuple,list)) and address and not is_loopback(address[0]):
            raise PermissionError('Automated tests may connect to loopback hosts only')
        return original_connect(sock,address)
    def guarded_connect_ex(sock,address):
        if isinstance(address,(tuple,list)) and address and not is_loopback(address[0]):
            raise PermissionError('Automated tests may connect to loopback hosts only')
        return original_connect_ex(sock,address)
    monkeypatch.setattr(socket,'getaddrinfo',guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket,'connect',guarded_connect)
    monkeypatch.setattr(socket.socket,'connect_ex',guarded_connect_ex)
    yield

@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine);init_db()
    # The offline gate gives TAG_CACHE its own SQLite file. Clear its tables too,
    # or identical fixture fingerprints leak results between otherwise isolated tests.
    if cache_engine is not None:
        Base.metadata.drop_all(cache_engine,tables=[VisionCache.__table__,TextCache.__table__])
        Base.metadata.create_all(cache_engine,tables=[VisionCache.__table__,TextCache.__table__])
    yield

@pytest.fixture
def dataset():
    from backend.importer import import_file
    source=Path(__file__).resolve().parent/'fixtures'/'sample_10_rows.xlsx'
    return import_file(source.read_bytes(),source.name)
