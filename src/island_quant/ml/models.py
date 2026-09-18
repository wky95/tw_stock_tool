"""Small deterministic regression baselines without unsafe serialized executables."""

from __future__ import annotations

import platform
from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class EstimatorContract:
    model_name: str
    model_version: str
    hyperparameters: dict[str, Any]
    feature_names: tuple[str, ...]
    feature_dtypes: tuple[str, ...]
    target_definition: str
    supports_sample_weight: bool
    random_seed: int
    library_versions: dict[str, str]
    fit_method: str
    prediction_method: str


class Regressor(Protocol):
    contract: EstimatorContract

    def fit(
        self, features: list[list[float]], target: list[float], weights: list[float]
    ) -> None: ...

    def predict(self, features: list[list[float]]) -> list[float]: ...

    def state(self) -> dict[str, Any]: ...


def contract(
    name: str,
    version: str,
    hyperparameters: dict[str, Any],
    feature_names: tuple[str, ...],
    target_definition: str,
    *,
    supports_sample_weight: bool = True,
    random_seed: int = 42,
) -> EstimatorContract:
    return EstimatorContract(
        model_name=name,
        model_version=version,
        hyperparameters=hyperparameters,
        feature_names=feature_names,
        feature_dtypes=tuple("float64" for _ in feature_names),
        target_definition=target_definition,
        supports_sample_weight=supports_sample_weight,
        random_seed=random_seed,
        library_versions={"python": platform.python_version(), "implementation": "island_quant"},
        fit_method="deterministic_weighted_fit",
        prediction_method="matrix_dot_product",
    )


class DummyRegressor:
    def __init__(self, specification: EstimatorContract, strategy: str) -> None:
        if strategy not in {"mean", "zero", "rank_constant"}:
            raise ValueError("unsupported dummy strategy")
        self.contract = specification
        self.strategy = strategy
        self.constant: float | None = None

    def fit(
        self, features: list[list[float]], target: list[float], weights: list[float]
    ) -> None:
        _validate_fit_inputs(features, target, weights, self.contract)
        if self.strategy == "zero":
            self.constant = 0.0
        elif self.strategy == "rank_constant":
            self.constant = 0.5
        else:
            self.constant = sum(
                y * weight for y, weight in zip(target, weights, strict=True)
            ) / sum(weights)

    def predict(self, features: list[list[float]]) -> list[float]:
        if self.constant is None:
            raise RuntimeError("dummy model is not fitted")
        return [self.constant] * len(features)

    def state(self) -> dict[str, Any]:
        return {
            "contract": asdict(self.contract),
            "strategy": self.strategy,
            "constant": self.constant,
        }


class LinearRegressor:
    def __init__(self, specification: EstimatorContract, ridge_alpha: float = 0.0) -> None:
        if ridge_alpha < 0:
            raise ValueError("ridge alpha cannot be negative")
        self.contract = specification
        self.ridge_alpha = ridge_alpha
        self.coefficients: list[float] | None = None

    def fit(
        self, features: list[list[float]], target: list[float], weights: list[float]
    ) -> None:
        _validate_fit_inputs(features, target, weights, self.contract)
        design = [[1.0, *row] for row in features]
        width = len(design[0])
        gram = [[0.0] * width for _ in range(width)]
        rhs = [0.0] * width
        for row, outcome, weight in zip(design, target, weights, strict=True):
            for left in range(width):
                rhs[left] += weight * row[left] * outcome
                for right in range(width):
                    gram[left][right] += weight * row[left] * row[right]
        for index in range(1, width):
            gram[index][index] += self.ridge_alpha
        self.coefficients = _solve(gram, rhs)

    def predict(self, features: list[list[float]]) -> list[float]:
        if self.coefficients is None:
            raise RuntimeError("linear model is not fitted")
        return [
            self.coefficients[0]
            + sum(
                coefficient * value
                for coefficient, value in zip(self.coefficients[1:], row, strict=True)
            )
            for row in features
        ]

    def state(self) -> dict[str, Any]:
        return {
            "contract": asdict(self.contract),
            "ridge_alpha": self.ridge_alpha,
            "coefficients": self.coefficients,
        }


class ElasticNetRegressor:
    def __init__(
        self,
        specification: EstimatorContract,
        alpha: float,
        l1_ratio: float,
        max_iterations: int = 1000,
        tolerance: float = 1e-10,
    ) -> None:
        if alpha <= 0 or not 0 <= l1_ratio <= 1:
            raise ValueError("elastic-net alpha and l1_ratio are invalid")
        self.contract = specification
        self.alpha = alpha
        self.l1_ratio = l1_ratio
        self.max_iterations = max_iterations
        self.tolerance = tolerance
        self.intercept: float | None = None
        self.coefficients: list[float] | None = None

    def fit(
        self, features: list[list[float]], target: list[float], weights: list[float]
    ) -> None:
        _validate_fit_inputs(features, target, weights, self.contract)
        weight_sum = sum(weights)
        self.intercept = sum(
            y * weight for y, weight in zip(target, weights, strict=True)
        ) / weight_sum
        centered = [value - self.intercept for value in target]
        width = len(features[0])
        coefficients = [0.0] * width
        for _ in range(self.max_iterations):
            largest_change = 0.0
            for column in range(width):
                residual_product = 0.0
                scale = 0.0
                for row_index, row in enumerate(features):
                    other_prediction = sum(
                        coefficients[index] * row[index]
                        for index in range(width)
                        if index != column
                    )
                    residual_product += (
                        weights[row_index]
                        * row[column]
                        * (centered[row_index] - other_prediction)
                    )
                    scale += weights[row_index] * row[column] ** 2
                threshold = self.alpha * self.l1_ratio * weight_sum
                numerator = _soft_threshold(residual_product, threshold)
                denominator = scale + self.alpha * (1 - self.l1_ratio) * weight_sum
                updated = numerator / denominator if denominator else 0.0
                largest_change = max(largest_change, abs(updated - coefficients[column]))
                coefficients[column] = updated
            if largest_change <= self.tolerance:
                break
        self.coefficients = coefficients

    def predict(self, features: list[list[float]]) -> list[float]:
        if self.intercept is None or self.coefficients is None:
            raise RuntimeError("elastic-net model is not fitted")
        return [
            self.intercept
            + sum(
                coefficient * value
                for coefficient, value in zip(self.coefficients, row, strict=True)
            )
            for row in features
        ]

    def state(self) -> dict[str, Any]:
        return {
            "contract": asdict(self.contract),
            "alpha": self.alpha,
            "l1_ratio": self.l1_ratio,
            "intercept": self.intercept,
            "coefficients": self.coefficients,
            "max_iterations": self.max_iterations,
            "tolerance": self.tolerance,
        }


def fit_regressor(
    estimator: Regressor,
    features: list[list[float]],
    target: list[float],
    weights: list[float] | None,
) -> None:
    if weights is not None and not estimator.contract.supports_sample_weight:
        raise ValueError(
            f"estimator {estimator.contract.model_name} does not support sample weights"
        )
    actual_weights = weights or [1.0] * len(target)
    estimator.fit(features, target, actual_weights)


def _validate_fit_inputs(
    features: list[list[float]],
    target: list[float],
    weights: list[float],
    specification: EstimatorContract,
) -> None:
    if not features or len(features) != len(target) or len(target) != len(weights):
        raise ValueError("model fit arrays are empty or misaligned")
    if any(len(row) != len(specification.feature_names) for row in features):
        raise ValueError("model feature schema width mismatch")
    if any(weight <= 0 for weight in weights):
        raise ValueError("sample weights must be positive")


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    width = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(width)]
    for column in range(width):
        pivot = max(range(column, width), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("linear system is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(width):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(
                    augmented[row], augmented[column], strict=True
                )
            ]
    return [augmented[row][-1] for row in range(width)]


def _soft_threshold(value: float, threshold: float) -> float:
    if value > threshold:
        return value - threshold
    if value < -threshold:
        return value + threshold
    return 0.0
