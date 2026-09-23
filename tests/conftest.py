import importlib.util
from pathlib import Path
import sys

# config.py is local and gitignored; fall back to the example so tests can import updater.
if importlib.util.find_spec("config") is None:
    example = Path(__file__).resolve().parents[1] / "config.example.py"
    spec = importlib.util.spec_from_file_location("config", example)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["config"] = module
