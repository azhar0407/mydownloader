"""Root conftest: tambah path repo ke sys.path supaya 'import app' berhasil
baik pytest dijalankan dari root maupun dari tests/."""

import os
import sys

# Tambah direktori root repo ke sys.path (di mana app.py berada)
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
