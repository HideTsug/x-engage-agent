import sys
from pathlib import Path

# Tests run against the xmcp repo root (server.py / search_quota.py live there).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
