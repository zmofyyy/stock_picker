"""直接启动脚本：``python stock_picker/run.py``。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock_picker.app import main  # noqa: E402

if __name__ == "__main__":
    main()
