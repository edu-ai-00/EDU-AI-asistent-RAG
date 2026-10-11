#!/usr/bin/env python3
"""EDU-AI database migration runner (B03e-1): status, plan, init, apply, generators.

See specs/active/spec.migration-registry.md. Standard library only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eduai_migrate.commands import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
