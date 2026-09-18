"""Versioned expanding/rolling walk-forward splits with interval purging."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

import polars as pl


class SplitMethod(StrEnum):
    EXPANDING = "expanding_walk_forward"
    ROLLING = "rolling_walk_forward"


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    version: str
    method: SplitMethod
    train_sessions: int
    validation_sessions: int
    test_sessions: int
    step_sessions: int
    embargo_sessions: int = 0
    final_holdout_sessions: int = 0

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("split version is required")
        if min(
            self.train_sessions,
            self.validation_sessions,
            self.test_sessions,
            self.step_sessions,
        ) <= 0:
            raise ValueError("walk-forward windows must be positive")
        if self.embargo_sessions < 0 or self.final_holdout_sessions < 0:
            raise ValueError("embargo and holdout cannot be negative")
        if self.step_sessions < self.validation_sessions + self.test_sessions:
            raise ValueError("step_sessions must prevent overlapping OOS periods")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_id: str
    train: pl.DataFrame
    validation: pl.DataFrame
    test: pl.DataFrame
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    fit_cutoff: datetime
    purged_sample_count: int
    embargoed_sample_count: int


@dataclass(frozen=True, slots=True)
class SplitManifest:
    version: str
    method: str
    embargo_sessions: int
    embargo_purpose: str
    final_holdout_dates: tuple[date, ...]
    fixture_holdout_statistically_meaningful: bool
    fold_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    folds: tuple[WalkForwardFold, ...]
    final_holdout: pl.DataFrame
    manifest: SplitManifest


class WalkForwardSplitter:
    def __init__(self, config: WalkForwardConfig, trading_sessions: list[date]) -> None:
        self.config = config
        self.sessions = sorted(set(trading_sessions))
        self.final_holdout_access_count = 0

    def split(self, samples: pl.DataFrame) -> WalkForwardResult:
        if "split_role" in samples.columns:
            raise ValueError("preassigned or shuffled split roles are not accepted")
        valid = samples.filter(pl.col("valid")).sort(["decision_time", "instrument_id"])
        sample_dates = set(valid["decision_date"].unique().to_list())
        if not sample_dates.issubset(self.sessions):
            raise ValueError("sample dates must belong to the pinned trading calendar")
        holdout_count = min(self.config.final_holdout_sessions, len(self.sessions))
        holdout_dates = tuple(self.sessions[-holdout_count:]) if holdout_count else ()
        development_sessions = self.sessions[:-holdout_count] if holdout_count else self.sessions
        final_holdout = valid.filter(pl.col("decision_date").is_in(holdout_dates))
        folds: list[WalkForwardFold] = []
        train_end_index = self.config.train_sessions
        fold_number = 0
        while True:
            validation_start_index = train_end_index + self.config.embargo_sessions
            validation_end_index = validation_start_index + self.config.validation_sessions
            test_end_index = validation_end_index + self.config.test_sessions
            if test_end_index > len(development_sessions):
                break
            train_start_index = (
                0
                if self.config.method is SplitMethod.EXPANDING
                else train_end_index - self.config.train_sessions
            )
            train_dates = development_sessions[train_start_index:train_end_index]
            embargo_dates = development_sessions[train_end_index:validation_start_index]
            validation_dates = development_sessions[validation_start_index:validation_end_index]
            test_dates = development_sessions[validation_end_index:test_end_index]
            raw_train = valid.filter(pl.col("decision_date").is_in(train_dates))
            validation = valid.filter(pl.col("decision_date").is_in(validation_dates))
            test = valid.filter(pl.col("decision_date").is_in(test_dates))
            evaluation = validation.vstack(test)
            purged_train = _purge_overlapping(raw_train, evaluation)
            purged_count = raw_train.height - purged_train.height
            embargoed_count = valid.filter(
                pl.col("decision_date").is_in(embargo_dates)
            ).height
            if not train_dates or not validation_dates or not test_dates:
                break
            if (
                max(train_dates) >= min(validation_dates)
                or max(validation_dates) >= min(test_dates)
            ):
                raise RuntimeError("walk-forward chronology invariant failed")
            fit_cutoff = purged_train["decision_time"].max()
            if not isinstance(fit_cutoff, datetime):
                raise ValueError("purging left no valid training fit cutoff")
            fold_number += 1
            folds.append(
                WalkForwardFold(
                    fold_id=f"fold-{fold_number:03d}",
                    train=purged_train,
                    validation=validation,
                    test=test,
                    train_start=train_dates[0],
                    train_end=train_dates[-1],
                    validation_start=validation_dates[0],
                    validation_end=validation_dates[-1],
                    test_start=test_dates[0],
                    test_end=test_dates[-1],
                    fit_cutoff=fit_cutoff,
                    purged_sample_count=purged_count,
                    embargoed_sample_count=embargoed_count,
                )
            )
            train_end_index += self.config.step_sessions
        manifest = SplitManifest(
            version=self.config.version,
            method=self.config.method.value,
            embargo_sessions=self.config.embargo_sessions,
            embargo_purpose=(
                "exclude trading sessions between train and validation to reduce temporal leakage"
            ),
            final_holdout_dates=holdout_dates,
            fixture_holdout_statistically_meaningful=len(holdout_dates) >= 20,
            fold_ids=tuple(fold.fold_id for fold in folds),
        )
        return WalkForwardResult(tuple(folds), final_holdout, manifest)

    def access_final_holdout(self, result: WalkForwardResult) -> pl.DataFrame:
        self.final_holdout_access_count += 1
        return result.final_holdout


def _purge_overlapping(train: pl.DataFrame, evaluation: pl.DataFrame) -> pl.DataFrame:
    if not train.height or not evaluation.height:
        return train
    intervals = [
        (row["label_interval_start"], row["label_interval_end"])
        for row in evaluation.select("label_interval_start", "label_interval_end")
        .unique()
        .to_dicts()
    ]
    keep = []
    for row in train.to_dicts():
        start, end = row["label_interval_start"], row["label_interval_end"]
        overlap = any(start <= eval_end and end >= eval_start for eval_start, eval_end in intervals)
        keep.append(not overlap)
    return train.filter(pl.Series("keep", keep))
