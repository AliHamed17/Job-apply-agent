"""Live Web Dashboard Server Launcher for Ali Hamed."""

from __future__ import annotations

import os
import sys
import uvicorn

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

if __name__ == "__main__":
    print("\n" + "=" * 90)
    print("STARTING LIVE WEB DASHBOARD SERVER FOR ALI HAMED")
    print("Dashboard URL: http://localhost:8000")
    print("=" * 90 + "\n")
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=False)
