#!/usr/bin/env python3
"""
Process supervisor for long-running trading agent processes.

Monitors and restarts processes (live_ticker, scheduler) if they crash.
Provides health checks and automatic recovery.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.logging_config import get_logger, setup_logging
from core.shutdown import GracefulShutdown, register_shutdown_handler


logger = get_logger(__name__)


@dataclass
class ProcessConfig:
    """Configuration for a supervised process."""
    name: str
    command: list[str]
    cwd: Path
    env: dict[str, str] = None
    restart_delay: float = 5.0
    max_restarts: int = -1  # -1 = unlimited
    health_check_interval: float = 30.0
    startup_timeout: float = 60.0


class SupervisedProcess:
    """A process managed by the supervisor."""

    def __init__(self, config: ProcessConfig):
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self.restart_count = 0
        self.last_start_time: float = 0
        self._should_run = True

    def start(self) -> bool:
        """Start the process."""
        env = os.environ.copy()
        if self.config.env:
            env.update(self.config.env)

        logger.info(f"Starting {self.config.name}", extra={"command": " ".join(self.config.command)})

        try:
            self.process = subprocess.Popen(
                self.config.command,
                cwd=self.config.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.last_start_time = time.time()
            self.restart_count += 1
            logger.info(f"{self.config.name} started with PID {self.process.pid}")
            return True
        except Exception as e:
            logger.error(f"Failed to start {self.config.name}", extra={"error": str(e)})
            return False

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the process gracefully."""
        if self.process is None:
            return True

        logger.info(f"Stopping {self.config.name} (PID {self.process.pid})")

        try:
            # Send SIGTERM for graceful shutdown
            self.process.terminate()

            # Wait for graceful shutdown
            try:
                self.process.wait(timeout=timeout)
                logger.info(f"{self.config.name} stopped gracefully")
                return True
            except subprocess.TimeoutExpired:
                # Force kill if graceful shutdown failed
                logger.warning(f"{self.config.name} did not stop gracefully, forcing kill")
                self.process.kill()
                self.process.wait()
                logger.info(f"{self.config.name} force killed")
                return True
        except Exception as e:
            logger.error(f"Error stopping {self.config.name}", extra={"error": str(e)})
            return False
        finally:
            self.process = None

    def is_running(self) -> bool:
        """Check if process is still running."""
        if self.process is None:
            return False
        return self.process.poll() is None

    def get_exit_code(self) -> Optional[int]:
        """Get process exit code if terminated."""
        if self.process is None:
            return None
        return self.process.poll()

    def should_restart(self) -> bool:
        """Check if process should be restarted."""
        if not self._should_run:
            return False
        if self.config.max_restarts >= 0 and self.restart_count >= self.config.max_restarts:
            logger.error(f"{self.config.name} reached max restarts ({self.config.max_restarts})")
            return False
        return True

    def read_output(self) -> None:
        """Read and log process output."""
        if self.process and self.process.stdout:
            try:
                line = self.process.stdout.readline()
                if line:
                    logger.info(f"[{self.config.name}] {line.rstrip()}")
            except Exception:
                pass


class ProcessSupervisor:
    """Supervises multiple long-running processes."""

    def __init__(self, processes: list[ProcessConfig]):
        self.processes = {p.name: SupervisedProcess(p) for p in processes}
        self._shutdown = False

    def register_shutdown(self) -> None:
        """Register shutdown handlers."""

        def shutdown_handler():
            self.shutdown()

        register_shutdown_handler(shutdown_handler)

    def start_all(self) -> None:
        """Start all supervised processes."""
        for name, proc in self.processes.items():
            proc.start()
            time.sleep(1)  # Stagger starts

    def stop_all(self) -> None:
        """Stop all supervised processes."""
        for name, proc in self.processes.items():
            proc.stop()

    def shutdown(self) -> None:
        """Shutdown supervisor and all processes."""
        logger.info("Supervisor shutting down")
        self._shutdown = True
        self.stop_all()

    def monitor(self) -> None:
        """Main monitoring loop."""
        logger.info("Starting process monitor")

        # Register signal handlers
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, shutting down")
            self.shutdown()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # Initial start
        self.start_all()

        while not self._shutdown:
            time.sleep(1)

            for name, proc in self.processes.items():
                # Read output
                proc.read_output()

                # Check if process died
                if not proc.is_running():
                    exit_code = proc.get_exit_code()
                    logger.warning(
                        f"{name} exited with code {exit_code}",
                        extra={"process": name, "exit_code": exit_code, "restart_count": proc.restart_count}
                    )

                    if proc.should_restart():
                        logger.info(f"Restarting {name} in {proc.config.restart_delay}s")
                        time.sleep(proc.config.restart_delay)
                        proc.start()
                    else:
                        logger.error(f"{name} will not be restarted")
                        self.shutdown()
                        break

                # Health check
                if proc.is_running() and proc.config.health_check_interval > 0:
                    elapsed = time.time() - proc.last_start_time
                    if elapsed > proc.config.startup_timeout:
                        # Process has been running long enough, could add custom health checks here
                        pass

        logger.info("Monitor loop ended")


def create_default_supervisor(project_root: Path) -> ProcessSupervisor:
    """Create supervisor with default trading agent processes."""
    processes = [
        ProcessConfig(
            name="live_ticker",
            command=[sys.executable, "-m", "ingest.live_ticker"],
            cwd=project_root,
            restart_delay=5.0,
            max_restarts=-1,
            health_check_interval=30.0,
            startup_timeout=60.0,
        ),
        ProcessConfig(
            name="scheduler",
            command=[sys.executable, "-m", "scripts.scheduler"],
            cwd=project_root,
            restart_delay=5.0,
            max_restarts=-1,
            health_check_interval=30.0,
            startup_timeout=30.0,
        ),
    ]
    return ProcessSupervisor(processes)


def main():
    parser = argparse.ArgumentParser(description="Trading agent process supervisor")
    parser.add_argument("--process", choices=["live_ticker", "scheduler", "all"], default="all",
                        help="Which process(es) to supervise")
    parser.add_argument("--no-restart", action="store_true",
                        help="Don't restart processes on failure")
    args = parser.parse_args()

    setup_logging()
    logger.info("Starting trading agent supervisor")

    project_root = Path(__file__).parent.parent

    if args.process == "all":
        supervisor = create_default_supervisor(project_root)
    elif args.process == "live_ticker":
        supervisor = ProcessSupervisor([
            ProcessConfig(
                name="live_ticker",
                command=[sys.executable, "-m", "ingest.live_ticker"],
                cwd=project_root,
                restart_delay=5.0,
                max_restarts=-1 if not args.no_restart else 0,
            )
        ])
    elif args.process == "scheduler":
        supervisor = ProcessSupervisor([
            ProcessConfig(
                name="scheduler",
                command=[sys.executable, "-m", "scripts.scheduler"],
                cwd=project_root,
                restart_delay=5.0,
                max_restarts=-1 if not args.no_restart else 0,
            )
        ])

    supervisor.register_shutdown()
    supervisor.monitor()
    logger.info("Supervisor stopped")


if __name__ == "__main__":
    main()