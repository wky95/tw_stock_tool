# ADR 0007：Leakage-safe ML baselines

## Status

Accepted for Phase 1 Slice 4. All artifacts remain `Candidate`.

## Decision

Supervised rows are joined only on the exact `(instrument_id, decision_time)` key and only for the pinned point-in-time eligible universe. Feature names and dtypes are an ordered schema. Labels, identifiers, market fields, validity fields, and other metadata cannot be selected as model features. Missing features are either retained as invalid or the entire sample is dropped according to an explicit versioned policy; zero fill is not an implicit option.

Every dataset pins its feature set, feature artifact, label artifact, label definition, universe, price dataset, and availability-policy versions. `latest` and `current` are rejected. A canonical JSON representation of the ordered records and manifest produces the deterministic dataset checksum.

## Walk-forward boundary

Only expanding and fixed-length rolling walk-forward splits are accepted. For each fold:

1. Training sessions are strictly earlier than validation sessions.
2. `embargo_sessions` pinned trading sessions are omitted between training and validation. Calendar-day subtraction is never used.
3. Validation is strictly earlier than test.
4. A training row is purged when its closed label interval intersects any validation or test label interval: `train_start <= evaluation_end && train_end >= evaluation_start`.
5. `step_sessions >= validation_sessions + test_sessions` prevents overlapping OOS ownership.
6. The final holdout is removed before fold construction. It is not read by preprocessing, selection, or routine test reporting; access must pass through the counted accessor.

The split manifest records method/version, embargo purpose and length, holdout dates, fold IDs, date ranges, purged samples, and embargoed samples. Short fixture holdouts are explicitly not statistically meaningful.

## Preprocessing and weighting

Median imputation, 1%/99% winsor thresholds, means, and sample standard deviations are learned from the training rows of one fold. Constant columns receive scale `1.0`. The fitted object validates exact feature order before every transform and records its fit dates and parameters.

The default loss weight is `1 / n_t` for each observation on decision date `t`, so every date has total weight one. Equal observation weighting is also supported. An estimator declaring no sample-weight support fails explicitly.

## Models and selection

The predeclared inventory is dummy training mean, dummy zero, ordinary linear regression, Ridge with alpha `{0.1, 1, 10}`, and Elastic Net with alpha `{0.1, 1}` and l1 ratio `{0.25, 0.5, 0.75}`. These deterministic implementations avoid a heavy runtime dependency and save plain numeric JSON state, not executable pickle data.

Selection maximizes mean daily validation Spearman IC. Ties prefer, in order, the simpler family, stronger regularization, fewer features, then lexical model/hyperparameter ordering. Test predictions are generated only after selection and never enter the selection key.

## OOS ledger, evaluation, and bootstrap

The ledger accepts only validation, test, or explicitly accessed final-holdout predictions. Duplicate `(instrument_id, decision_time)` ownership and in-sample rows fail closed. Each row identifies its experiment, fold, model, fit cutoff, feature artifact, model artifact, role, and creation time.

Fold and aggregate reports provide daily Pearson/Spearman IC, mean IC, IC standard deviation, ICIR with a minimum-history gate, positive-IC ratio, MAE/RMSE, coverage, prediction distribution, quantile returns, gross top-minus-bottom spread, and simplified prediction-membership turnover. They do not treat pooled stock rows as independent time observations.

Confidence intervals use moving blocks of decision-date daily Spearman observations. Seed, block length, and resample count are saved. Fewer than `max(3, 2 * block_length)` usable dates produces `unavailable`, not a numeric interval.

## Artifact and safety boundary

Model artifacts contain the fitted preprocessing parameters, estimator state and contract, ordered feature schema, target and input versions, train/validation periods, split manifest, validation/OOS metrics, seed, runtime versions, Git commit, dirty flag, source-tree hash, completeness, and model card. Artifact files are immutable content-addressed JSON with checksum verification. No external pickle/joblib loader exists.

Slice 4 permits `Candidate` only. Dirty provenance, incomplete point-in-time reference data, missing final-holdout approval, and the Slice 4 lifecycle boundary all reject promotion. Every card displays:

`EXPLORATORY — INCOMPLETE POINT-IN-TIME REFERENCE DATA`

Returns and spreads are gross-before-costs. These outputs are research diagnostics, not a live-trading or investability claim.

## Consequences and known limits

The implementation is transparent and deterministic but intentionally lacks nonlinear tree models, large hyperparameter searches, PCA, SHAP, transaction costs, portfolio construction, and live execution. Pure-Python solvers are appropriate for the baseline/fixture scale, not a claim of large-scale numerical performance. Point-in-time reference completeness remains the binding promotion limitation.
