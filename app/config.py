import os
from pathlib import Path

ORCH_DIR = os.environ.get("ORCH_DIR", "/opt/sdwan-advpn-reference")
DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
