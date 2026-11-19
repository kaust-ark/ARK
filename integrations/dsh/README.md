# Idea2Paper for DeepSeek Harness

[中文](README_zh.md)

Give Idea2Paper a question and a new project directory. A persistent DSH session plans the experiment, runs it, reads the measurements and writes the paper. An independent reviewer examines the manuscript and can request original results and rendered pages. Follow up with the same project to change the experiment or repair a claim.

## Install the prerelease

Use Linux, Node.js 22.12 or newer, pnpm and DSH 0.2.1-rc.2. Python 3.10 or newer with venv and pip, TeX Live with pdflatex and BibTeX, and a working Docker daemon are needed for the local experiment. The first run installs the bundled Python wheel and its dependencies into a private cache and builds the experiment image. Budget disk space and package-download time for that setup. [DSH installation](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.1-rc.2)

The release asset for version 0.2.0-alpha.2 installs into the dedicated profile with one command. The DSH Web Discover view requires the full upstream CLI with its Web profile.

```sh
dsh plugin --profile idea2paper add https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-0.2.0-alpha.2/idea2paper-dsh-0.2.0-alpha.2.tgz
```

From a source checkout, build the same package locally and install the resulting archive.

```sh
python3 integrations/dsh/pack.py --out dist/dsh
dsh plugin --profile idea2paper add "$PWD/dist/dsh/idea2paper-dsh-0.2.0-alpha.2.tgz"
dsh --profile idea2paper --help
```

## Run a small study

Configure your chosen model in DSH. For its DeepSeek provider, set `DEEPSEEK_API_KEY` in the environment that launches DSH. Use a model and provider with image input for checks requiring inspection of rendered figures. A standalone project with a configured Gemini key can also request Google Deep Research and method illustrations. Those calls use the associated provider account.

```sh
dsh --profile idea2paper run search-study "Compare the number of key inspections in linear and binary search for missing targets on sorted arrays. Include small arrays and report the exact counts."
dsh --profile idea2paper status search-study
dsh --profile idea2paper resume search-study "Report the crossover and any ties in the tested range."
dsh --profile idea2paper stop search-study
dsh --profile idea2paper resume search-study
```

The first command opens the project. Run status, send a follow-up or request a stop from another terminal. Resume after an interruption with the original profile, model and project directory. Standalone runs default to local experiment execution. A configured Slurm environment can dispatch experiments to the cluster. The paper must pass the current revision's review and delivery checks.

| Inspect | Project file |
| --- | --- |
| Scientific milestones and measured artifact paths | `auto_research/state/research_events.jsonl` |
| Measurements and plotted figures | `results/` and `paper/figures/` |
| Rendered paper | `paper/main.pdf` |
| Independent verdict and issue evidence | `auto_research/state/native_review.json` |
| Archived reviewed PDFs | `auto_research/state/reviews/<revision>/main.pdf` |
| Delivery checks | `auto_research/state/delivery_report.yaml` |

## Discover

In DSH Web, open **Plugins → More → Experimental plugins**, enable **Custom plugin markets**, and select **Discover → Add index source**. After publication, enter the index URL below. The card identifies the exact release package commit and its supported DSH version. Market sources and installations belong to the selected profile. Use the CLI install command for the `idea2paper` profile when browsing from a separate Web profile. [DSH Discover guide](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/docs/user/guide/plugin-market.md)

```text
https://raw.githubusercontent.com/kaust-ark/ARK/dsh-bundle-0.2.0-alpha.2/index.json
```

## Verify and publish

From the source checkout, put the pinned DSH binary and pnpm on PATH. Build once and test the same tarball intended for release. Choose a new output directory for the distribution repository.

```sh
python3 integrations/dsh/pack.py --out dist/dsh
python3 integrations/dsh/verify.py dist/dsh/idea2paper-dsh-0.2.0-alpha.2.tgz --prepare dist/dsh/release
```

The verifier installs the archive over loopback into a clean profile, reads a stopped project using the packaged companion, checks the Git target in the running DSH Web Discover service, and installs that exact commit through a local Git mirror. Its separate Git repository contains the exact unpacked npm contents, including the wheel, and an index pointing to that package commit. The legacy v0.1.0-alpha.1 package fixture is available as `verify_legacy.py`.

After the release gates pass, a release owner with access to the repository can publish the tag and attach the verified tarball. The tag exposes the index directly as a raw HTTPS file. The archive remains available from the release asset URL above.

```sh
git -C dist/dsh/release push git@github.com:kaust-ark/ARK.git refs/tags/dsh-bundle-0.2.0-alpha.2
gh release create dsh-bundle-0.2.0-alpha.2 dist/dsh/idea2paper-dsh-0.2.0-alpha.2.tgz --repo kaust-ark/ARK --verify-tag --prerelease --title "Idea2Paper DSH 0.2.0-alpha.2" --notes "Persistent research sessions, measured experiments and revision-bound independent review. Requires DSH 0.2.1-rc.2."
```

Verify both public downloads against the tested files, then repeat the one-command installation with a fresh DSH home.

```sh
curl -fsSL https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-0.2.0-alpha.2/idea2paper-dsh-0.2.0-alpha.2.tgz -o dist/dsh/downloaded.tgz
cmp dist/dsh/downloaded.tgz dist/dsh/idea2paper-dsh-0.2.0-alpha.2.tgz
curl -fsS https://raw.githubusercontent.com/kaust-ark/ARK/dsh-bundle-0.2.0-alpha.2/index.json -o dist/dsh/downloaded-index.json
cmp dist/dsh/downloaded-index.json dist/dsh/release/index.json
```

An npm maintainer can additionally publish the same archive with `npm publish dist/dsh/idea2paper-dsh-0.2.0-alpha.2.tgz --access public --tag alpha`. Check its registry `dist.integrity` before documenting the npm install path. To uninstall the profile plugin, run `dsh plugin --profile idea2paper remove idea2paper-dsh`. Keep the project directory and original profile for resuming work.
