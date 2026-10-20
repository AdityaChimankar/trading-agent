"""Timestamp-based partitions with label-horizon purging across all symbols."""
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit

METADATA_COLUMNS = ["symbol", "__timestamp", "__label_end"]


def split(timestamps, label_end, test_size=.2):
    if not 0 < test_size < 1:
        raise ValueError("test_size must be between zero and one")
    stamps = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True))
    ends = pd.DatetimeIndex(pd.to_datetime(label_end, utc=True))
    if len(stamps) != len(ends) or stamps.isna().any() or (ends < stamps).any():
        raise ValueError("Invalid timestamp or label horizon metadata")
    unique = stamps.unique().sort_values()
    cut = int(len(unique)*(1-test_size))
    if not 0 < cut < len(unique):
        raise ValueError("Insufficient distinct timestamps for a temporal split")
    boundary = unique[cut]
    train = np.flatnonzero((stamps < boundary) & (ends < boundary))
    test = np.flatnonzero((stamps >= boundary) & ends.notna())
    if not len(train) or not len(test):
        raise ValueError("No observations remain after purging label horizons")
    return train, test


def three_way(timestamps, label_end, test_size=.2):
    """Holdout never participates in early stopping or threshold selection."""
    stamps, ends = pd.Series(timestamps).reset_index(drop=True), pd.Series(label_end).reset_index(drop=True)
    development, test = split(stamps, ends, test_size)
    train, tune = split(stamps.iloc[development], ends.iloc[development], .25)
    return development[train], development[tune], test


def folds(timestamps, label_end, n_splits=5):
    stamps = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True))
    ends = pd.DatetimeIndex(pd.to_datetime(label_end, utc=True))
    unique = stamps.unique().sort_values()
    for before, after in TimeSeriesSplit(n_splits=n_splits).split(unique):
        boundary = unique[after[0]]
        development = np.flatnonzero((stamps <= unique[before[-1]]) & (ends < boundary))
        test = np.flatnonzero((stamps >= boundary) & (stamps <= unique[after[-1]]) &
                             (ends <= unique[after[-1]]))
        train, tune = split(stamps[development], ends[development], .25)
        if not len(test):
            raise ValueError("No holdout observations remain after purging")
        yield development[train], development[tune], test


def require_binary(y):
    if set(pd.Series(y).dropna().unique()) != {0, 1}:
        raise ValueError("Binary training requires both observed classes 0 and 1; continuous returns are unsupported")
