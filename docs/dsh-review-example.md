# Read a reviewer decision against the measurements

The pilot used three deliberately constructed paired timings. A Gemini reviewer in a fresh, read-only DSH subagent received each frozen manuscript and could inspect its data. The original draft claimed caching halved the median time. Check the medians yourself.

| Trial | Uncached (ms) | Cached (ms) |
| --- | ---: | ---: |
| 1 | 9 | 11 |
| 2 | 10 | 10 |
| 3 | 11 | 9 |

Sorting each column yields 9, 10, 11. Both medians are 10 ms.

| Manuscript shown to the reviewer | Actual verdict | Finding R1 |
| --- | --- | --- |
| Original speedup claim | Revise | Opened against the manuscript passage and the CSV rows |
| Edited text retaining that claim | Revise | Still present |
| Both sample medians reported as 10 ms | Accept | Resolved |

Download the **review-replay.zip** asset from the bundle prerelease and extract it. Open each revision’s `paper/main.pdf`, then its `review.json` to see the cited passage, source evidence and verdict. All three use `results/rows.csv`. The archive is an offline replay and requires no credentials. Each review is paired with the exact page artifact by SHA-256. To verify the archive contents on Linux, run `sha256sum -c SHA256SUMS` from its extracted directory.

The original and corrected manuscripts were benchmark inputs. The reviewer responses came from live model calls. On the first matched controls, both the prior and native reviewer detected the unsupported speedup and accepted the scoped null result. The new reviewer additionally provided verifiable data locations and carried R1 across revisions. In a separate test with the rows withheld, it returned an unassessed verdict after the rendered-page attachment path was fixed. An earlier attempt lost the page receipt and a provider request returned HTTP 503. These observations cover one held-out timing example.

The repository includes the fixture and the evaluator for preparing new blind packets. [Paired inputs](../test_fixtures/peer_review/pairs.json) · [Evaluation script](../scripts/eval_peer_review.py)

## Independent recomputation before the reviewer sees a revision

A second benchmark uses temperature calibration on handwritten digits. An incorrect manuscript and its worker agreed on the wrong temperature. Reviewing their agreement missed the scientific error. For a disputed revision that preserves the reported rows, the native reviewer now gets the original research question and the measurement schema first. It submits a program for one condition, or records why the calculation is infeasible. The host seals that decision before showing the manuscript, observed values and previous review.

The live Google reviewer abstained on the flawed manuscript. It had spent its three execution attempts on a batch-shaped interface. Its fourth program had the right interface but never ran during that review. The corrected tool describes the single-row contract and allows one additional execution for a row-format error. Replaying those exact recorded calls produced the following results.

| Frozen input | Independent computation | Comparison with the manuscript |
| --- | --- | --- |
| Flawed revision, seed 2 | Temperature 64 | The paper reported 16. The raw and calibrated negative log-likelihoods also differed. |
| Corrected revision, seed 3 | Six numeric outcomes | All six matched the independent reference. |

The dashboard shows the failed attempts and the final comparison. The recorded model verdict remains **unassessed**. A fresh blinded run must produce its own signed-off findings on the flaw and clear the corrected control before this benchmark can support a reviewer-quality claim.

### A fresh blind attempt on the calibration study

In a new live review, the first model response supplied an independent calculation from the protocol alone. The program reproduced all six reference outcomes for seed 2. The manuscript reported a temperature of 16 where the calculation gave 64. Both negative log-likelihood values also differed. An offline replay on held-out seed 3 matched all six independent reference values.

The reviewer then tried six verdicts. It cited a passage on the wrong PDF page, and one blocker lacked a manuscript location. The last verdict still failed validation. Nine requests used $0.2483 in reported text usage. The remaining per-arm allowance could not cover another request. The incorrect manuscript therefore has **no archived verdict**. The correct twin was conditional on a successful negative control and did not run.

The candidate now reports several independent evidence errors together. Within the same DSH reviewer session, the reviewer can amend just the affected checks or findings. The host rebuilds the full verdict and validates its sources, findings and revision before archiving. In an offline replay, all six original proposals still failed. The five successive edits reconstructed their full original verdicts exactly from 7,670 bytes of amendment JSON, compared with 20,440 bytes of compact full-verdict JSON. This measures serialization on recorded responses. Live use, cost and success on the corrected control still require a new blind test.
