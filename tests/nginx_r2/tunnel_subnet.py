#!/usr/bin/env python3
"""The preflight tool is scripts/tunnel_subnet.py: ONE implementation, used by the rollout script and by this harness.

This path only keeps the harness imports (`import tunnel_subnet`) and old links working: importing it returns the real module,
running it runs the real command line.
"""
import importlib.util
import pathlib
import sys

_path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "tunnel_subnet.py"
_spec = importlib.util.spec_from_file_location("tunnel_subnet_impl", _path)
_impl = importlib.util.module_from_spec(_spec)
sys.modules["tunnel_subnet_impl"] = _impl
_spec.loader.exec_module(_impl)
if __name__ == "__main__":
    sys.exit(_impl.main())
sys.modules[__name__] = _impl
