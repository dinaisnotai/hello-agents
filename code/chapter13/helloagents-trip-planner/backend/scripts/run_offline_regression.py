"""Run regression tests without using locally configured hotel/map credentials."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings


if __name__ == "__main__":
    settings.hotel_api_provider = "none"
    settings.amap_api_key = ""
    settings.enable_travel_knowledge = False
    settings.travel_knowledge_retrieval_mode = "hybrid"
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(not result.wasSuccessful())
