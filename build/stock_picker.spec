# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['F:/source/stock_rsi/packaging/entry.py'],
    pathex=['F:/source/stock_rsi'],
    binaries=[],
    datas=[('F:/source/stock_rsi/stock_picker/static', 'stock_picker/static')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on', 'uvicorn.lifespan.off', 'pyarrow', 'pyarrow.parquet', 'pyarrow._parquet', 'pyarrow.lib', 'pyarrow._compute', 'pyarrow.vendored', 'pytdx', 'pytdx.reader', 'pytdx.reader.gbbq_reader', 'pytdx.reader.day_reader', 'pytdx.reader.tdx_reader_base', 'pytdx.parser', 'pytdx.parser.gbbq_parser', 'anyio._backends._asyncio', 'email.mime.text'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'tkinter', 'PIL', 'IPython', 'jupyter', 'notebook', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx', 'pytest', 'sphinx', 'docutils', 'pandas.tests', 'numpy.tests', 'sqlalchemy', 'boto3', 'botocore', 's3fs', 'pyarrow.flight', 'pyarrow.gandiva', 'pyarrow.cuda', 'pyarrow._substrait', 'pyarrow._azurefs', 'pyarrow._s3fs', 'pyarrow._gcsfs', 'pyarrow._hdfs', 'pyarrow.dataset', 'pyarrow.orc', 'pyarrow.parquet.encryption'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='stock_picker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['F:/source/stock_rsi/packaging/stock_picker.ico'],
)
