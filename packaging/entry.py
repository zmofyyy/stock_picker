"""PyInstaller 入口脚本。

打包后 exe 的 ``__main__`` 就是这里 —— 只做一件事：把启动交给
``stock_picker.app.main()``，保证 exe 与 ``python -m stock_picker.app`` 行为一致。
"""

from __future__ import annotations

import multiprocessing

from stock_picker.app import main

if __name__ == "__main__":
    # 冻结后若子进程重新执行 exe（pandas / pyarrow 某些路径会碰多进程），
    # 没有这行会无限开新窗口。
    multiprocessing.freeze_support()
    main()
