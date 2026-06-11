"""
Backend模块初始化文件
"""

import os
import sys

backend_path = os.path.dirname(__file__)
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)
