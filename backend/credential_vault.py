"""Windows current-user DPAPI vault for environment credentials.

Public interface:
* ``profile_scope(profile=None)`` resolves DEV/TEST to ``dev-test`` and PROD to ``prod``.
* ``get_secret(namespace, key, profile=None)`` returns a secret to an explicitly authorized
  caller, or ``None`` when it is absent.
* ``set_secret(namespace, key, value, profile=None)`` and ``delete_secret(...)`` mutate it.
* ``has_secret(...)`` and ``credential_status(profile=None)`` reveal presence only.

The encrypted store lives outside both the repository and business data. On non-Windows hosts,
cryptographic operations fail closed; tests may inject ``_protect``/``_unprotect`` and
``_vault_path`` without weakening production behavior.
"""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

VAULT_PATH = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local')) / 'GeWuWorkbench' / 'RunCenter' / 'credentials.dpapi'
_ENTROPY = b'GeWuWorkbench credential vault v1'
_SCOPES = {'dev': 'dev-test', 'development': 'dev-test', 'test': 'dev-test',
           'acceptance': 'dev-test', 'prod': 'prod', 'production': 'prod'}
_PROCESS_LOCK = threading.RLock()


def _vault_path() -> Path:
    # Path injection is intentionally private and is used by isolated tests only.
    return VAULT_PATH


@contextmanager
def _vault_lock():
    """Serialize read-modify-write updates across DEV and TEST processes."""
    path = _vault_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + '.lock')
    with _PROCESS_LOCK, lock_path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            # Production is Windows-only; the process lock keeps tests deterministic elsewhere.
            yield


def profile_scope(profile: str | None = None) -> str:
    value = str(profile or os.environ.get('GEWU_PROFILE') or os.environ.get('GEWU_ENV') or 'dev').strip().lower()
    if value not in _SCOPES:
        raise ValueError('环境必须是 dev、test 或 prod')
    return _SCOPES[value]


class VaultUnavailable(RuntimeError):
    """The current platform cannot safely access the DPAPI vault."""


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_Blob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != 'nt':
        raise VaultUnavailable('Windows 当前用户 DPAPI 凭证库仅可在 Windows 上使用')
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _Blob()
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(output))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(source), 'GeWuWorkbench credentials', ctypes.byref(entropy), None, None, 0, ctypes.byref(output))
    if not ok:
        raise VaultUnavailable('无法使用当前 Windows 用户解密凭证库')
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _protect(data: bytes) -> bytes:
    return _crypt(data, decrypt=False)


def _unprotect(data: bytes) -> bytes:
    return _crypt(data, decrypt=True)


def _load() -> dict:
    path = _vault_path()
    if not path.is_file():
        return {'version': 1, 'scopes': {}}
    try:
        clear = _unprotect(path.read_bytes())
        value = json.loads(clear.decode('utf-8'))
    except VaultUnavailable:
        raise
    except Exception as exc:
        raise VaultUnavailable('凭证库无法读取或格式无效') from exc
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('scopes'), dict):
        raise VaultUnavailable('凭证库版本或结构无效')
    return value


def _save(value: dict) -> None:
    path = _vault_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = _protect(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    fd, name = tempfile.mkstemp(prefix='.credentials-', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _valid(namespace: str, key: str) -> tuple[str, str]:
    if not isinstance(namespace, str) or not isinstance(key, str) or not namespace or not key:
        raise ValueError('凭证类别和标识不能为空')
    if any(ord(ch) < 32 for ch in namespace + key) or len(namespace) > 100 or len(key) > 200:
        raise ValueError('凭证类别或标识格式无效')
    return namespace, key


def get_secret(namespace: str, key: str, profile: str | None = None) -> str | None:
    namespace, key = _valid(namespace, key)
    value = _load()
    secret = value['scopes'].get(profile_scope(profile), {}).get(namespace, {}).get(key)
    return secret if isinstance(secret, str) and secret else None


def set_secret(namespace: str, key: str, secret: str, profile: str | None = None) -> None:
    namespace, key = _valid(namespace, key)
    if not isinstance(secret, str) or not secret or any(ch in secret for ch in '\r\n\0'):
        raise ValueError('凭证内容无效')
    with _vault_lock():
        value = _load()
        scope = value['scopes'].setdefault(profile_scope(profile), {})
        scope.setdefault(namespace, {})[key] = secret
        _save(value)


def delete_secret(namespace: str, key: str, profile: str | None = None) -> None:
    namespace, key = _valid(namespace, key)
    with _vault_lock():
        value = _load()
        scope = value['scopes'].get(profile_scope(profile), {})
        entries = scope.get(namespace, {})
        entries.pop(key, None)
        if not entries:
            scope.pop(namespace, None)
        if not scope:
            value['scopes'].pop(profile_scope(profile), None)
        _save(value)


def has_secret(namespace: str, key: str, profile: str | None = None) -> bool:
    return bool(get_secret(namespace, key, profile))


def credential_status(profile: str | None = None) -> dict:
    """Presence-only status; never returns identifiers or credential values."""
    scope = profile_scope(profile)
    value = _load()['scopes'].get(scope, {})
    providers = value.get('model', {})
    return {'profile': scope,
            'model': any(isinstance(v, str) and v for v in providers.values()),
            'sellersprite': bool(value.get('sellersprite', {}).get('mcp_secret_key')),
            'sorftime': bool(value.get('sorftime', {}).get('api_key'))}
