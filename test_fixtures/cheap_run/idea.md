# Feature Standardization Accelerates Logistic Regression Convergence

## Research Summary
We study whether standardizing input features (zero mean, unit variance) reduces
the number of optimizer iterations a logistic-regression classifier needs to
converge, without harming accuracy. Hypothesis: ill-conditioned raw features
force the L-BFGS optimizer to take many small steps; standardization improves the
conditioning of the loss surface and converges faster.

## External Systems & Platforms
- scikit-learn (already installed in the environment): `LogisticRegression`,
  `StandardScaler`, `load_breast_cancer`. No GPU, no external services, no
  downloads — the dataset ships inside scikit-learn.

## Proposed Methodology
Use scikit-learn's built-in breast-cancer dataset (569 samples, 30 features).
Fix a train/test split (`random_state=0`). Train two
`LogisticRegression(solver="lbfgs", max_iter=1000, random_state=0)` models: one
on raw features, one on `StandardScaler`-transformed features. Report `n_iter_`
(iterations to converge) and held-out test accuracy for each. Expected result:
the standardized model converges in far fewer iterations at equal-or-better
accuracy. Present one comparison table and one bar chart of `n_iter_` (raw vs
standardized).

## Scope
This is a deliberately small, self-contained study intended to run in seconds on
CPU. One dataset, one model family, one preprocessing toggle, one headline
finding.
