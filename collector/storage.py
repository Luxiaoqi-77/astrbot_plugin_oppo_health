"""Private JSON: owner-only POSIX files, user-scoped DPAPI on Windows."""
import base64
import ctypes
import json
import os
from pathlib import Path
import stat

FORMAT = 'oppo-health-dpapi-v1'

def is_windows():
    return os.name == 'nt'

def private_root():
    if is_windows():
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local')))
        return base / 'AstrBot/oppo-health'
    return Path.home() / '.local/share/astrbot-oppo-health'

def _dpapi(payload, decrypt=False):
    """Use the logged-in Windows user, never machine-wide protection."""
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        description = None
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        description = 'OPPO Health private data'
    fn.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; intentionally no CRYPTPROTECT_LOCAL_MACHINE.
    if not fn(ctypes.byref(source), description, None, None, None, 1, ctypes.byref(output)):
        raise ValueError('Windows 私有数据加解密失败，请使用创建文件的同一 Windows 用户')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(output.data, 0, output.size)
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))

def read_private_json(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('私有数据必须是普通文件，不能使用符号链接')
    if not is_windows() and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
        raise ValueError('私有数据文件必须由当前用户拥有，且权限为 600')
    stored = json.loads(path.read_text(encoding='utf-8'))
    if is_windows():
        if not isinstance(stored, dict) or stored.get('format') != FORMAT:
            raise ValueError('Windows 不接受明文凭证，请重新运行 configure_auth.py 导入')
        try:
            encrypted = base64.b64decode(stored['payload'], validate=True)
            return json.loads(_dpapi(encrypted, decrypt=True).decode('utf-8'))
        except (KeyError, TypeError, UnicodeError, ValueError) as exc:
            raise ValueError('Windows 私有文件无效或不属于当前用户') from exc
    return stored

def write_private_json(value, path):
    path = Path(path)
    plain = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if is_windows():
        encrypted = _dpapi(plain)
        content = json.dumps({'format': FORMAT, 'payload': base64.b64encode(encrypted).decode('ascii')}).encode('utf-8')
    else:
        content = plain
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError('私有目录不能为符号链接')
    if not is_windows():
        path.parent.chmod(0o700)
    temp = path.with_name('.private-' + os.urandom(8).hex())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(content)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
