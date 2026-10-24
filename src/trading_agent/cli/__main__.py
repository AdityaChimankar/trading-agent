"""Allow ``python -m trading_agent.cli`` as an alternative to the console script."""

from trading_agent.cli import app


if __name__ == "__main__":
    app()
