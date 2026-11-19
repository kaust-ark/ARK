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

The reviewer then tried six verdicts. In the first verdict, the reviewer quoted two headings on PDF page 2. It had received images of all seven pages and had never requested the text of page 2. A later blocker lacked a manuscript location. The last verdict still failed validation. Nine requests used $0.2483 in reported text usage. The remaining per-arm allowance could not cover another request. The incorrect manuscript therefore has **no archived verdict**. The correct twin was conditional on a successful negative control and did not run.

The candidate now reports several independent evidence errors together. Within the same DSH reviewer session, the reviewer can amend just the affected checks or findings. The host rebuilds the full verdict and validates its sources, findings and revision before archiving. In an offline replay, all six original proposals still failed. The five successive edits reconstructed their full original verdicts exactly from 7,670 bytes of amendment JSON, compared with 20,440 bytes of compact full-verdict JSON. This measures serialization on recorded responses. Live use, cost and success on the corrected control still require a new blind test.

The dashboard now displays the reported temperature of 16 beside the independently recomputed 64.0 as soon as the recorded result arrives. Replaying the genuine seed-2 event in Chromium showed one arrival animation on desktop. A narrow phone with reduced motion displayed both values immediately. Reloading either view restored the result without animating it again.

Replaying the six rejected verdicts also exposed an actionable distinction between seeing a PDF page and quoting its text. The new diagnostic identified page 2 as observed only in image form. Reading its text once verified both headings in all three places the first proposed verdict cited them. One primary-source quotation still failed. The frozen text mixed the abstract with numeric tick labels from the neighboring plot. Keep this fixture for older projects and check the original page before adjudicating that citation. This replay used the original manuscript and unmodified verdicts and made no new model requests.

### What a signed review still missed

In the next blind run the reviewer committed its own program before seeing the worker's values, inspected all seven PDF pages and archived a request for revision. The source of each value matters. The worker reported a temperature of 16 and the reviewer computed 64. Its written diagnosis attributed 16 to its own calculation. Its selection of 64 was correct, yet its own reporting calculation clipped very small probabilities. For seed 2 the reviewer computed a calibrated negative log-likelihood of about 1.568 nats. Evaluating the requested log-domain definition gives about 17,806.360 nats. Four of the six reviewer outcomes matched the independent reference. Its frozen program also missed the held-out seed. The negative control failed and the conditional clean arm did not run. The nine successful provider requests cost $0.14425 at the recorded rates.

The bundle now supplies the worker and reviewer values under explicit source names and asks for a known-answer boundary case for metrics used in selection and reporting. A reviewer attributing a reproduced discrepancy to the worker must attach the observed row, the executed operation and the available protocol definition to its finding. Replaying the archived proposal with its original evidence receipts stopped it at this check. The next live test must locate the scientific defect against the original definition, preserve the direction of each comparison and clear a correct manuscript under the same budget. A discrepant number alone can also be a defect in the reviewer's program.

A second qualification uses five paired splits from the Friedman regression study. The clean passage reports larger prediction errors and offers neighborhood changes as a hypothesis. Its twin uses the same numbers and asserts that this mechanism dominates the error gap. Neither neighbor identities nor a bias decomposition were recorded. The jury should credit a reviewer that preserves the empirical finding and identifies precisely the unsupported extension. Removing the five rows tests whether it will acknowledge missing evidence. This case uses three-decimal values from the recorded worker output, SHA-256 `237980f1b0a5dfaba3038c4692b864afa4fa9a8a966b7246123728460bc59de3`. Its live qualification is pending. [Paired review inputs](../test_fixtures/peer_review/pairs.json)
