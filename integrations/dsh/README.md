# Idea2Paper for DeepSeek Harness

The bundle runs a small research workflow in DSH. Separate model sessions plan the study, write an experiment, draft the paper, and review the claims. The experiment produces measured data in a local container. The bundle renders a LaTeX source and a PDF, then checks the delivery with the existing Python audit.

The current scope is a local CPU experiment that fits in Python's standard library. Use ideas that can be tested on generated inputs. The workflow makes at most one revision after independent review. Research requiring an external dataset, libraries, GPU, or Slurm needs a separate execution backend.

## Install

Use Linux with Node.js 22.12 or later, pnpm, `flock`, a local Docker daemon, and TeX Live with `pdflatex` and T1 fonts. Install DSH `0.2.1-rc.2`. Pull the same experiment image the package uses:

```sh
docker pull python@sha256:ae52c5bef62a6bdd42cd1e8dffef86b9cd284bde9427da79839de7a4b983e7ca
```

Install the Idea2Paper Python project for the audit, then install the packed bundle into a dedicated profile. Run these commands from the source checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
export IDEA2PAPER_PYTHON="$PWD/.venv/bin/python"
npm pack ./integrations/dsh
dsh plugin --profile idea2paper add "$PWD/idea2paper-dsh-0.1.0-alpha.1.tgz"
dsh --profile idea2paper --help
```

Configure the DSH model and credentials for the dedicated profile. For the default DeepSeek route, set `DEEPSEEK_API_KEY` in the environment. The supported DSH version may print pnpm peer warnings during bundle installation. It supplies those peers from its own installation. Use the profile name `idea2paper` to activate the workflow commands. The bundle can be listed in DSH Web Discover while its workflow runs in the dedicated CLI profile.

## Run and resume

```sh
dsh --profile idea2paper run search-study "Compare linear and binary search on missing targets"
dsh --profile idea2paper resume search-study
```

Give `run` a new project directory. The workspace contains `workflow.json`, `results/experiment.json`, `paper/main.tex`, `paper/main.pdf`, and `auto_research/state/delivery_report.yaml`. The checkpoint records the model selection and completed DSH role session IDs. Resume with the same model and project directory. It retries the unfinished model stage, reuses a matching experiment container after interruption, and reuses recorded results after the experiment completes. A second rejected review leaves the feedback in the checkpoint and stops before PDF delivery. A project from alpha.0 resumes with the alpha.0 bundle.

The experiment container runs as an unprivileged user with a read-only filesystem, no network, no host mounts, and bounded memory, process count, CPU, and time. The runner keeps an interrupted container long enough to recover its result. For a failed experiment, `docker logs idea2paper-<project id>` displays the bounded output. The project ID is in `workflow.json`.

## Verify a package

```sh
python3 integrations/dsh/verify.py ./idea2paper-dsh-0.1.0-alpha.1.tgz
```

The test installs the tarball into a fresh DSH profile, uses scripted model responses over loopback, and checks real container results, PDFs, audit reports, and resume behavior.

## Publish a prerelease

Run `npm publish <tested-tarball> --access public --tag alpha` from an authorized npm account. Verify the published `dist.integrity` matches the tested tarball. Install the exact version in a clean DSH home with `dsh plugin --profile idea2paper add idea2paper-dsh@0.1.0-alpha.1` and check `dsh --profile idea2paper --help`.

To list it in Discover, generate a format-1 index from the published npm package name using the [DSH market guide](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/docs/user/guide/plugin-market.md) and serve the result over HTTPS. Include the pinned DSH version and tell users to install the bundle into the `idea2paper` profile. To withdraw a prerelease, remove it from the index and remove the npm `alpha` dist-tag. An installed package can be removed with `dsh plugin --profile idea2paper remove idea2paper-dsh`. Keep project directories and checkpoints for resuming with their original bundle version.

The [implementation plan](../../ADRs/0015-dsh-native-bundle.md) records the public release criteria.
