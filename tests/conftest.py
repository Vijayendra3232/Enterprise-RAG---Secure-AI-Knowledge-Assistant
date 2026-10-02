"""
conftest.py — Global test configuration ensuring backend/ directory is loaded in sys.path.
"""

import os
import sys

# Ensure backend directory is first in sys.path
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
backend_dir = os.path.join(root_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
