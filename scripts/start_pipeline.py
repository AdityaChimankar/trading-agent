#!/usr/bin/env python
"""
Unified pipeline startup - runs validation, then starts all services:
  1. Pre-flight validation (all APIs, auth, data freshness)
  2. Live ticker (ingest.live_ticker) - WebSocket candle stream
  3. Scheduler (scripts.scheduler) - news/sentiment/decisions/ML/LLM/shadow/monitor/repair
  4. Dashboard API (uvicorn api.main:app) - REST endpoints
  5. Frontend (npm run dev) - React dashboard

Usage:
  python -m scripts.start_pipeline

Environment:
  FRONTEND_DIR - path to frontend (default: ./frontend)
  SKIP_VALIDATION - set to "1" to skip pre-flight check (not recommended)
  SKIP_FRONTEND - set to "1" to skip frontend dev server
"""
import os
import sys
import subprocess
import signal
import time
import threading
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

CHECK_MARK = "[OK]"
FAIL_MARK = "[FAIL]"
WARN_MARK = "[WARN]"
INFO_MARK = "[INFO]"


def run_validation() -> bool:
    """Run pre-flight validation. Returns True if all checks pass."""
    print(f"\n{INFO_MARK} Running pre-flight validation...")
    print("=" * 60)
    
    # Import and run validation
    from scripts.validate_setup import main as validate_main
    
    # Capture exit code by running in subprocess
    result = subprocess.run(
        [sys.executable, "-m", "scripts.validate_setup"],
        capture_output=True,
        text=True
    )
    
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    
    if result.returncode == 0:
        print(f"{CHECK_MARK} Pre-flight validation PASSED")
        return True
    else:
        print(f"{FAIL_MARK} Pre-flight validation FAILED")
        return False


class ProcessManager:
    """Manages multiple subprocesses with proper cleanup."""
    
    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}
        self.shutdown_event = threading.Event()
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        print(f"\n{INFO_MARK} Shutdown signal received ({signum})...")
        self.shutdown_event.set()
        self.stop_all()
        sys.exit(0)
    
    def start(self, name: str, cmd: list, cwd: str = None, env: dict = None):
        """Start a process."""
        print(f"{INFO_MARK} Starting {name}: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            cwd=cwd or Path(__file__).parent.parent,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.processes[name] = proc
        
        # Start output reader thread
        threading.Thread(target=self._read_output, args=(name, proc), daemon=True).start()
        return proc
    
    def _read_output(self, name: str, proc: subprocess.Popen):
        """Read and print process output with prefix."""
        for line in proc.stdout:
            if self.shutdown_event.is_set():
                break
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] [{name}] {line.rstrip()}")
    
    def stop_all(self):
        """Stop all processes gracefully."""
        print(f"{INFO_MARK} Stopping all processes...")
        for name, proc in self.processes.items():
            if proc.poll() is None:
                print(f"{INFO_MARK} Stopping {name}...")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print(f"{WARN_MARK} Force killing {name}...")
                    proc.kill()
                    proc.wait()
        print(f"{CHECK_MARK} All processes stopped")
    
    def wait_for_shutdown(self):
        """Wait until shutdown event or any process dies unexpectedly."""
        while not self.shutdown_event.is_set():
            time.sleep(1)
            # Check if any process died unexpectedly
            for name, proc in self.processes.items():
                if proc.poll() is not None:
                    print(f"{FAIL_MARK} Process {name} exited unexpectedly with code {proc.returncode}")
                    self.shutdown_event.set()
                    break


def main():
    print("=" * 60)
    print("UNIFIED PIPELINE STARTUP")
    print("=" * 60)
    print(f"Project root: {Path(__file__).parent.parent}")
    print(f"Time: {datetime.now().isoformat()}")
    
    # Check if validation should be skipped
    if os.getenv("SKIP_VALIDATION") == "1":
        print(f"{WARN_MARK} Skipping pre-flight validation (SKIP_VALIDATION=1)")
    else:
        if not run_validation():
            print(f"{FAIL_MARK} Validation failed. Fix issues before starting pipeline.")
            print(f"       Set SKIP_VALIDATION=1 to bypass (not recommended)")
            sys.exit(1)
    
    # Initialize process manager
    pm = ProcessManager()
    project_root = Path(__file__).parent.parent
    
    try:
        # 1. Live Ticker (terminal 1 equivalent)
        pm.start(
            "live_ticker",
            [sys.executable, "-m", "ingest.live_ticker"],
        )
        
        # Small delay to let ticker connect
        time.sleep(2)
        
        # 2. Scheduler (terminal 2 equivalent)
        pm.start(
            "scheduler",
            [sys.executable, "-m", "scripts.scheduler"],
        )
        
        # Small delay
        time.sleep(1)
        
        # 3. Dashboard API (terminal 3 equivalent)
        pm.start(
            "api",
            [sys.executable, "-m", "uvicorn", "api.main:app", "--reload", "--port", "8000"],
        )
        
        # Small delay
        time.sleep(1)
        
        # 4. Health Monitor (separate port 8001)
        if os.getenv("SKIP_HEALTH_MONITOR") != "1":
            pm.start(
                "health_monitor",
                [sys.executable, "-m", "scripts.health_monitor"],
            )
            time.sleep(1)
        else:
            print(f"{INFO_MARK} Skipping health monitor (SKIP_HEALTH_MONITOR=1)")
        
        # Small delay
        time.sleep(1)
        
        # 5. Frontend (terminal 5 equivalent) - optional
        if os.getenv("SKIP_FRONTEND") != "1":
            frontend_dir = Path(os.getenv("FRONTEND_DIR", project_root / "frontend"))
            if frontend_dir.exists():
                pm.start(
                    "frontend",
                    ["npm", "run", "dev"],
                    cwd=frontend_dir,
                )
            else:
                print(f"{WARN_MARK} Frontend directory not found at {frontend_dir}, skipping")
        else:
            print(f"{INFO_MARK} Skipping frontend (SKIP_FRONTEND=1)")
        
        print("\n" + "=" * 60)
        print(f"{CHECK_MARK} ALL SERVICES STARTED")
        print("=" * 60)
        print("Services running:")
        for name in pm.processes.keys():
            print(f"  - {name}")
        print("\nDashboard:       http://localhost:8000")
        print("Health Monitor:  http://localhost:8001/health")
        print("  - Liveness:    http://localhost:8001/health/live")
        print("  - Readiness:   http://localhost:8001/health/ready")
        print("  - Metrics:     http://localhost:8001/metrics (Prometheus)")
        print("Frontend:        http://localhost:5173 (or check npm output)")
        print("\nPress Ctrl+C to stop all services")
        print("=" * 60 + "\n")
        
        # Wait for shutdown
        pm.wait_for_shutdown()
        
    except KeyboardInterrupt:
        print(f"\n{INFO_MARK} Keyboard interrupt received")
    except Exception as e:
        print(f"{FAIL_MARK} Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        pm.stop_all()


if __name__ == "__main__":
    main()