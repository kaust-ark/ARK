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
