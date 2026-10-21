"""
JSON-safe conversion for the API layer.

The dashboard's logic returns a mix of dataclasses (PositionPlan,
PositionStatus, PortfolioAdjustedPlan, Opportunity), sqlite3.Row
objects, and numpy/pandas scalars. FastAPI's encoder chokes on numpy
types and NaN is not valid JSON, so everything crossing the API
boundary goes through jsonable() first.
"""
import dataclasses
import sqlite3
from datetime import date, datetime

import numpy as np
import pandas as pd


def jsonable(value):
    """Recursively convert `value` into plain JSON-serializable types.

    - dataclasses -> dict
    - sqlite3.Row -> dict
    - numpy scalars -> Python scalars
    - NaN / NaT / pandas NA -> None (NaN isn't valid JSON)
    - datetime/date -> ISO string
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if f != f else f  # NaN -> null
    if isinstance(value, (int, str)):
        return value
    # pandas.Timestamp is a datetime subclass, so the datetime branch
    # below catches it too - checked before the generic fallback.
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, sqlite3.Row):
        return {k: jsonable(value[k]) for k in value.keys()}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, np.ndarray):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    # pandas NA / NaT and anything else nullable
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value
