# RemoteDesktopServer.spec
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = [
    ('C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2/config', 'config'),
    ('C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2/core', 'core'),
    ('C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2/agents', 'agents'),
    ('C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2/transport', 'transport'),
    ('C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2/utils', 'utils'),
    ('C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2/ui', 'ui'),
    ('C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2/service', 'service')
]
binaries = [('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-console-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-console-l1-2-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-datetime-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-debug-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-errorhandling-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-fibers-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-file-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-file-l1-2-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-file-l2-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-handle-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-heap-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-interlocked-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-libraryloader-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-localization-l1-2-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-memory-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-namedpipe-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-processenvironment-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-processthreads-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-processthreads-l1-1-1.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-profile-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-rtlsupport-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-string-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-synch-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-synch-l1-2-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-sysinfo-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-timezone-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-core-util-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-conio-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-convert-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-environment-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-filesystem-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-heap-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-locale-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-math-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-multibyte-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-private-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-process-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-runtime-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-stdio-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-string-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-time-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/api-ms-win-crt-utility-l1-1-0.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/concrt140.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/msvcp140.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/msvcp140_1.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/msvcp140_2.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/msvcp140_atomic_wait.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/msvcp140_codecvt_ids.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/python3.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/python311.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/ucrtbase.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/vcamp140.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/vccorlib140.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/vcruntime140.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/vcruntime140_1.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/vcruntime140_threads.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/zlib.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/bzip2.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/expat.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/ffi-7.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/ffi-8.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/ffi.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/libbz2.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/libcrypto-3-x64.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/libexpat.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/liblzma.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/libssl-3-x64.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/sqlite3.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/tcl86t.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/bin/tk86t.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/lib/dde1.4/tcldde14.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/lib/itcl4.3.0/itcl430t.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/lib/reg1.3/tclreg13.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Library/lib/sqlite3.45.3/sqlite3453t.dll', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/pyexpat.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/select.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/unicodedata.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/winsound.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/xxlimited.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/xxlimited_35.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_asyncio.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_bz2.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_ctypes.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_ctypes_test.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_decimal.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_elementtree.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_hashlib.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_lzma.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_msi.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_multiprocessing.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_overlapped.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_queue.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_socket.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_sqlite3.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_ssl.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_testbuffer.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_testcapi.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_testconsole.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_testimportmultiple.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_testinternalcapi.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_testmultiphase.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_tkinter.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_uuid.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/DLLs/_zoneinfo.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/_cffi_backend.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/bcrypt/_bcrypt.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/cryptography/hazmat/bindings/_rust.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/greenlet/_greenlet.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/greenlet/tests/_test_extension.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/greenlet/tests/_test_extension_cpp.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/httptools/parser/parser.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/httptools/parser/url_parser.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/PIL/_avif.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/PIL/_imaging.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/PIL/_imagingcms.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/PIL/_imagingft.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/PIL/_imagingmath.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/PIL/_imagingmorph.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/PIL/_imagingtk.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/PIL/_webp.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/psutil/_psutil_windows.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/pydantic_core/_pydantic_core.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/sqlalchemy/cyextension/collections.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/sqlalchemy/cyextension/immutabledict.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/sqlalchemy/cyextension/processors.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/sqlalchemy/cyextension/resultproxy.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/sqlalchemy/cyextension/util.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/watchfiles/_rust_notify.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/websockets/speedups.cp311-win_amd64.pyd', '.'), ('C:/Users/ELAD DAUDET/.conda/envs/nexus-ras-v2/Lib/site-packages/yaml/_yaml.cp311-win_amd64.pyd', '.')]
hiddenimports = [
    "_ctypes",
        "ctypes",
        "ctypes.wintypes",
        "_ssl",
        "ssl",
        "_hashlib",
        "hashlib",
        "_asyncio",
        "asyncio",
        "pyautogui",
        "pygetwindow",
        "pymsgbox",
        "pyscreeze",
        "pyrect",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "fastapi.routing",
        "fastapi.security",
        "starlette.routing",
        "starlette.middleware",
        "starlette.middleware.cors",
        "pydantic.deprecated.class_validators",
        "pydantic_settings",
        "cryptography",
        "cryptography.hazmat.primitives.ciphers.aead",
        "cryptography.hazmat.primitives.asymmetric.ec",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        "cryptography.hazmat.backends.openssl",
        "cryptography.x509",
        "websockets.asyncio.client",
        "websockets.asyncio.server",
        "websockets.legacy.client",
        "websockets.legacy.server",
        "sqlalchemy.dialects.sqlite",
        "aiosqlite",
        "bcrypt",
        "jwt",
        "pyotp",
        "mss",
        "mss.windows",
        "pynput",
        "pynput.keyboard",
        "pynput.keyboard._win32",
        "pynput.mouse",
        "pynput.mouse._win32",
        "psutil",
        "structlog",
        "click",
        "rich",
        "rich.console",
        "rich.table",
        "rich.panel",
        "rich.progress",
        "httpx",
        "aiofiles",
        "PIL",
        "PIL.Image",
        "PIL.JpegImagePlugin",
        "PIL.PngImagePlugin"
]

for pkg in ['pyautogui', 'pygetwindow', 'pymsgbox', 'pyscreeze', 'pyrect', 'uvicorn', 'fastapi', 'starlette', 'pydantic', 'pydantic_settings', 'cryptography', 'websockets', 'rich', 'click', 'pynput', 'mss', 'PIL', 'psutil', 'structlog', 'aiosqlite', 'sqlalchemy', 'bcrypt', 'pyotp']:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2/server.py'],
    pathex=['C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['C:/Users/ELAD DAUDET/Documents/projects/nexus-ras-v2/rth_dll_fix.py'],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='RemoteDesktopServer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
