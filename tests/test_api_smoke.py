"""Fresh-database API smoke tests."""

from fastapi.testclient import TestClient

from trading_agent.api.main import create_app
from trading_agent.core.config import get_settings
from trading_agent.storage import db as db_module


def test_fresh_database_health_wallet_and_position_lifecycle(tmp_path, monkeypatch):
    db_path = tmp_path / "smoke.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    get_settings.cache_clear()
    db_module.reset_database()

    try:
        with TestClient(create_app()) as client:
            assert client.get("/api/v1/health").status_code == 200
            assert client.get("/api/v1/ready").json()["status"] == "ready"
            assert client.get("/api/v1/wallet/metrics").status_code == 200
            assert client.get("/api/v1/watchlist/").json() == []

            created = client.post(
                "/api/v1/positions/",
                json={
                    "symbol": "RELIANCE",
                    "action": "BUY",
                    "entry_price": 100.0,
                    "quantity": 10,
                    "stop_loss": 95.0,
                    "take_profit": 110.0,
                    "strategy": "manual",
                },
            )
            assert created.status_code == 200, created.text
            position_id = created.json()["id"]

            open_positions = client.get("/api/v1/positions/").json()
            assert [position["symbol"] for position in open_positions] == ["RELIANCE"]

            closed = client.post(
                f"/api/v1/positions/{position_id}/close",
                params={"exit_price": 105.0},
            )
            assert closed.status_code == 200, closed.text
            assert closed.json()["pnl"] == 50.0

            assert client.get("/api/v1/positions/").json() == []
            closed_positions = client.get("/api/v1/positions/", params={"status": "closed"})
            assert closed_positions.status_code == 200
            assert len(closed_positions.json()) == 1
    finally:
        db_module.reset_database()
        get_settings.cache_clear()
