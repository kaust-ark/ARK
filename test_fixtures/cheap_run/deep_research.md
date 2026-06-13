# Deep Research Report: Feature Standardization and Logistic Regression Convergence

> NOTE: This is a CANNED report shipped with ARK's cheap "test project" preset.
> It is dropped into the project state so the pipeline skips the (expensive)
> live Gemini Deep Research call. It is intentionally short and self-contained.

## Background

Logistic regression is a standard linear classifier trained by minimizing the
regularized negative log-likelihood. Solvers such as L-BFGS are sensitive to the
*conditioning* of the input feature matrix: when features live on very different
scales, the curvature of the loss surface is highly anisotropic, and a
quasi-Newton method needs more iterations to reach the convergence tolerance.

## Prior Art (representative, not exhaustive)

- **Feature scaling for gradient-based optimization.** It is widely documented
  (e.g. LeCun et al.'s "Efficient BackProp" guidance, and standard ML texts such
  as Hastie, Tibshirani & Friedman) that standardizing inputs to zero mean and
  unit variance improves the conditioning of the optimization problem and speeds
  convergence for linear and neural models.
- **scikit-learn user guide — Preprocessing data.** Recommends `StandardScaler`
  before fitting linear models with iterative solvers, noting that unscaled
  features can dominate the objective and slow or destabilize convergence.
- **Conditioning and convergence of quasi-Newton methods.** Classical
  optimization results (Nocedal & Wright, *Numerical Optimization*) relate the
  condition number of the Hessian to the convergence rate of L-BFGS; better
  conditioning ⇒ fewer iterations.

## Gap / What This Project Adds

The effect is well-known qualitatively, but this project provides a crisp,
reproducible, single-dataset demonstration: a controlled before/after on the
breast-cancer dataset measuring `n_iter_` and test accuracy with everything else
held fixed. It is a clean teaching/benchmark artifact rather than a novel
method — the contribution is a tidy, fully-reproducible measurement.

## Suggested Experimental Setup

- Dataset: `sklearn.datasets.load_breast_cancer` (569 × 30).
- Split: `train_test_split(..., random_state=0)`.
- Models: `LogisticRegression(solver="lbfgs", max_iter=1000, random_state=0)` on
  raw vs `StandardScaler`-transformed features.
- Metrics: `n_iter_`, held-out accuracy.
- Expected: standardized model converges in markedly fewer iterations at
  equal-or-better accuracy.
