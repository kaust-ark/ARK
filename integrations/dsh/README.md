# Idea2Paper for DeepSeek Harness

[中文](README_zh.md)

Give Idea2Paper a question and a new project directory. A persistent DSH session plans the experiment, runs it, reads the measurements and writes the paper. An independent reviewer examines the manuscript and can request original results and rendered pages. Follow up with the same project to change the experiment or repair a claim.

## Install the prerelease

This source build supports two DSH runtimes on Linux with pnpm. Use Node.js 22.12 or newer for DSH 0.2.1-rc.2, and Node.js 24.9 or newer for DSH 0.2.2-rc.5. Resume each project with its original DSH version and profile. [DSH 0.2.1-rc.2](https://www.npmjs.com/package/@deepseek-ai/dsh/v/0.2.1-rc.2) · [DSH 0.2.2-rc.5](https://www.npmjs.com/package/@deepseek-ai/dsh/v/0.2.2-rc.5)

Python 3.10 or newer with venv and pip, TeX Live with pdflatex and BibTeX, and a working Docker daemon are needed for the local experiment. The first run installs the bundled Python wheel and its dependencies into a private cache and builds the experiment image. Budget disk space and package-download time for that setup.

Install version 0.2.0-alpha.28 into a dedicated profile with one command. The DSH Web Discover view requires the full upstream CLI with its Web profile.

```sh
dsh plugin --profile idea2paper add https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-0.2.0-alpha.28/idea2paper-dsh-0.2.0-alpha.28.tgz
```

To install a local build, run these commands from the source checkout.

```sh
python3 integrations/dsh/pack.py --out dist/dsh
version=$(node -p "require('./integrations/dsh/package.json').version")
dsh plugin --profile idea2paper add "$PWD/dist/dsh/idea2paper-dsh-$version.tgz"
dsh --profile idea2paper --help
```

To see how a review changes across manuscript revisions, open the offline replay. It contains three PDFs, their reviewer findings and the measured rows. The archive reproduces the example first published with alpha.3 and requires no model key. [Download the review replay](https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-0.2.0-alpha.24/review-replay.zip)

## Run a small study

Configure your chosen model in DSH. For its DeepSeek provider, set `DEEPSEEK_API_KEY` in the environment that launches DSH. Use a model and provider with image input for checks requiring inspection of rendered figures. A standalone project with a configured Gemini key can also request Google Deep Research and method illustrations. Those calls use the associated provider account.

```sh
dsh --profile idea2paper run search-study "Compare the number of key inspections in linear and binary search for missing targets on sorted arrays. Include small arrays and report the exact counts."
dsh --profile idea2paper status search-study
dsh --profile idea2paper resume search-study "Report the crossover and any ties in the tested range."
dsh --profile idea2paper stop search-study
dsh --profile idea2paper resume search-study
```

In a terminal, status shows the Slurm job, illustration check, last compiled body length and reviewed edition when available. Use `status <project> --json` or redirect its output for the full status record.

The first command opens the project. Run status, send a follow-up or request a stop from another terminal. Resume after an interruption with the original profile, model and project directory. Standalone runs default to local experiment execution. A configured Slurm environment can dispatch experiments to the cluster. The paper must pass the current revision's review and delivery checks.

| Inspect | Project file |
| --- | --- |
| Scientific milestones and measured artifact paths | `auto_research/state/research_events.jsonl` |
| Measurements and plotted figures | `results/` and `paper/figures/` |
| Rendered paper | `paper/main.pdf` |
| Independent verdict and issue evidence | `auto_research/state/native_review.json` |
| Archived reviewed PDFs | `auto_research/state/reviews/<revision>/main.pdf` |
| Delivery checks | `auto_research/state/delivery_report.yaml` |

The researcher can plot measured JSON columns directly as lines, points or grouped bars. A custom plotting script reads `/study/results.json` and saves its named output in `/output/`. For comparisons, Nano Banana supplies the study artwork and the tool typesets the experimental conditions from the approved plan. Each passing panel includes its caption. Mechanism diagrams are also available. During revisions, the owner can edit the numbered lines it has read. The editor checks changes to a previously compiled manuscript with TeX before saving them.

Open the manuscript progress recording to follow four real drafts from 0.91 to 3.34 body pages against a four-page target. Select a bar to return to its compilation event. The host shows page coverage independently of the review verdict. [Watch the dashboard replay](https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-0.2.0-alpha.24/draft-length.webm)

### Independent review

A reviewer working with a verified results table can commit its proposed comparison, competing explanation and discriminating observation before it reads the paper. It can search a phrase in the rendered PDF and compare the language across sections. In the hosted dashboard, open a judgment to see its evidence or send the researcher a question about the quoted page.

In a four-page study, a reviewer accepted a mechanism that the submitted measurements did not establish. Read its cited evidence before relying on a scientific judgment.

When a revised experiment returns the same measured rows, the reviewer first sees the original question and the measurement schema. It writes a short Python reproduction for one recorded condition. The bundle freezes that program before opening the manuscript, worker source and results in the same reviewer session. The review records each recomputed value alongside the worker’s value. If the data or compute is unavailable, the review identifies the missing evidence.

In the dashboard, expand **What the reviewer checked → Experimental controls** to inspect the comparison. The local Docker or configured Apptainer science image runs the calculation offline. A Slurm project can perform this check without submitting another job.

## Discover

In DSH Web, open **Plugins → More → Experimental plugins**, enable **Custom plugin markets**, and select **Discover → Add index source**. Add the community preview index below once. Discover lists subsequent releases at this address and selects the version compatible with your DSH installation. The card identifies the exact release package commit and its supported DSH version. Market sources and installations belong to the selected profile. Use the CLI install command for the `idea2paper` profile when browsing from a separate Web profile. [DSH Discover guide](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/docs/user/guide/plugin-market.md)

```text
https://raw.githubusercontent.com/kaust-ark/ARK/dsh-bundle-preview/index.json
```

## Verify and publish

From the source checkout, put the pinned DSH binary and pnpm on PATH. Build once and test the same tarball intended for release. Choose a new output directory for the distribution repository.

```sh
python3 integrations/dsh/pack.py --out dist/dsh
version=$(node -p "require('./integrations/dsh/package.json').version")
git clone --single-branch --branch dsh-bundle-preview https://github.com/kaust-ark/ARK.git dist/dsh/channel
python3 integrations/dsh/verify.py "dist/dsh/idea2paper-dsh-$version.tgz" --prepare dist/dsh/release --previous-index dist/dsh/channel/index.json
```

The release check uses `pdflatex` and `bibtex`. With local model responses it drives the installed owner through run and resume, breaks a rendered citation and the page layout, and requires a new independent review of the repaired PDF. The test spends no model credits.

To qualify a completed four-page ICML research project against the editorial benchmark, keep the authenticated Google Deep Research response outside the project and run the following from the checkout.

```sh
python scripts/eval_manuscript.py editorial PROJECT --google-response INTERACTION.json --output acceptance.json
```

The report verifies ten distinct source-backed publications in the body, the printed figures, references and appendix, final-column balance, a clean PDF rebuild and the review of that exact revision. The digits and Friedman benchmarks also replay their experimental measurements.

The verifier installs the archive over loopback into a clean profile, reads a stopped project using the packaged companion, checks the Git target in the running DSH Web Discover service, and installs that exact commit through a local Git mirror. Its separate Git repository contains the exact unpacked npm contents, including the wheel, and an index pointing to that package commit. The legacy v0.1.0-alpha.1 package fixture is available as `verify_legacy.py`.

Include the offline replay from alpha.3 and checksums for both downloads. The release tag exposes the Discover index as a raw HTTPS file.

```sh
curl -fsSL https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-0.2.0-alpha.3/review-replay.zip -o dist/dsh/review-replay.zip
(cd dist/dsh && sha256sum idea2paper-dsh-${version}.tgz review-replay.zip > SHA256SUMS)
git -C dist/dsh/release push git@github.com:kaust-ark/ARK.git refs/tags/dsh-bundle-${version}
gh release create dsh-bundle-${version} dist/dsh/idea2paper-dsh-${version}.tgz dist/dsh/review-replay.zip dist/dsh/SHA256SUMS --repo kaust-ark/ARK --verify-tag --prerelease --title "Idea2Paper DSH ${version}" --notes "Persistent research sessions, measured experiments and revision-bound independent review. Supports DSH 0.2.1-rc.2 and 0.2.2-rc.5."
```

Verify the public tarball and Discover index against the tested files, then repeat the one-command installation with a fresh DSH home.

```sh
curl -fsSL https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-${version}/idea2paper-dsh-${version}.tgz -o dist/dsh/downloaded.tgz
cmp dist/dsh/downloaded.tgz dist/dsh/idea2paper-dsh-${version}.tgz
curl -fsS https://raw.githubusercontent.com/kaust-ark/ARK/dsh-bundle-${version}/index.json -o dist/dsh/downloaded-index.json
cmp dist/dsh/downloaded-index.json dist/dsh/release/index.json
cp dist/dsh/downloaded-index.json dist/dsh/channel/index.json
git -C dist/dsh/channel add index.json
git -C dist/dsh/channel commit -m "List Idea2Paper $version in Discover"
git -C dist/dsh/channel push origin HEAD:dsh-bundle-preview
```

An npm maintainer can additionally publish the same archive with `npm publish dist/dsh/idea2paper-dsh-${version}.tgz --access public --tag alpha`. Check its registry `dist.integrity` before documenting the npm install path. To uninstall the profile plugin, run `dsh plugin --profile idea2paper remove idea2paper-dsh`. Keep the project directory and original profile for resuming work.
