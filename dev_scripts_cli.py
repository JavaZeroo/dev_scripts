#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dev-scripts CLI 入口点包装器"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
_project_root = Path(__file__).parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.cli import main

if __name__ == "__main__":
    sys.exit(main())
