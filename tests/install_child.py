"""Child process of the init-config interruption tests (not a test).

    install_child.py SHA TARGET REPO   [env: FAULT_STEP, FAULT_MODE=exit|pause, PAUSE_FILE, GO_FILE, START_FILE]
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import install  # noqa: E402


def wait_for(path, seconds=30):
    deadline = time.time() + seconds
    while not Path(path).exists() and time.time() < deadline:
        time.sleep(0.01)


def fault(step):
    if step != os.environ.get("FAULT_STEP"):
        return
    if os.environ.get("FAULT_MODE") == "exit":
        os._exit(9)
    Path(os.environ["PAUSE_FILE"]).write_text(step)
    wait_for(os.environ["GO_FILE"])


if os.environ.get("START_FILE"):
    wait_for(os.environ["START_FILE"])
report = install.init_config(sys.argv[1], sys.argv[2], sys.argv[3], fault=fault)
report.emit()
sys.exit(report.exit_code())
