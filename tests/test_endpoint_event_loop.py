"""An async endpoint that never awaits runs on the event loop and blocks every request.

On 2026-10-26 two did disk work there (the v1 digest over ~2,300 symbols and
/liveness/storage/stats over every Parquet file) and froze the whole API for
minutes. Sync work belongs in a plain ``def`` endpoint (FastAPI runs those in its
threadpool) or behind ``run_in_threadpool``.
"""
import ast
from pathlib import Path

ROUTERS = Path(__file__).resolve().parents[1] / "src" / "trading_agent" / "api" / "routers"
# In-memory only: no file, database or network access.
TRIVIAL = {"health_check", "wallet_cash_movement", "digest", "get_pipeline_status", "run_pipeline",
           "schedule_pipeline", "reset_wallet"}


def test_async_endpoints_await_or_are_trivial():
    offenders = []
    for path in sorted(ROUTERS.glob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            routed = isinstance(node, ast.AsyncFunctionDef) and any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", "") in {"get", "post", "put", "patch", "delete"}
                for d in node.decorator_list)
            if routed and node.name not in TRIVIAL and not any(
                    isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor)) for n in ast.walk(node)):
                offenders.append(f"{path.name}:{node.lineno} {node.name}")
    assert offenders == []
