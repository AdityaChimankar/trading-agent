"""Trading Agent - NSE Intraday Trading Signal System.

A local-first research pipeline for NSE intraday signals with three independent
decision paths: rule-based, LLM, and ML.
"""

__version__ = "2.0.0"
__author__ = "Trading Agent Team"

from trading_agent.core.config import Settings, get_settings

__all__ = [
    "Settings",
    "get_settings",
]
