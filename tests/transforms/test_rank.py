"""Tests for untangle/ranking on the new model (transforms.rank).

Defines:    same-second grouping, accuracy ranking, and resolution behaviour tests.
Used by:    pytest.
Depends on: transforms.rank, pandas.
"""
from __future__ import annotations

import pandas as pd

from model_atlas.transforms.rank import untangle


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # two records of the same second (1.0s and 1.4s in ns), different accuracy
            {"time_lower_unix_ns": 1_000_000_000, "time_upper_unix_ns": 1_000_000_000, "entity": "device",
             "time_lower_source_field": "T", "time_upper_source_field": "T", "horizontal_accuracy_m": 50.0},
            {"time_lower_unix_ns": 1_400_000_000, "time_upper_unix_ns": 1_400_000_000, "entity": "device",
             "time_lower_source_field": "T", "time_upper_source_field": "T", "horizontal_accuracy_m": 10.0},
            # a lone record in a different second (5.0s)
            {"time_lower_unix_ns": 5_000_000_000, "time_upper_unix_ns": 5_000_000_000, "entity": "device",
             "time_lower_source_field": "T", "time_upper_source_field": "T", "horizontal_accuracy_m": 5.0},
        ]
    )


def test_same_second_records_are_ranked_by_accuracy() -> None:
    out = untangle(_frame())
    # The more accurate (10m) record is main rank 1; the 50m one is additional rank 2.
    assert out.loc[1, "record_type"] == "main" and out.loc[1, "record_rank"] == 1
    assert out.loc[0, "record_type"] == "additional" and out.loc[0, "record_rank"] == 2
    # The lone record stays unranked.
    assert pd.isna(out.loc[2, "record_rank"])


def test_finer_resolution_separates_the_subsecond_pair() -> None:
    # At 0.1s resolution, 1.0s and 1.4s fall in different buckets -> no group.
    out = untangle(_frame(), resolution_ns=100_000_000)
    assert out["record_rank"].notna().sum() == 0
