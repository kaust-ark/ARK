# Idea2Paper for DeepSeek Harness

This local preview compares linear and binary search on deterministic inputs. The idea argument frames the experiment. The bundle drives separate DSH planning, writing, and review sessions, measures search comparisons locally, and compiles and audits the resulting PDF. It needs neither a hosted Idea2Paper account nor access to that service.

## Install and run

Use Linux with Node.js 22.12 or later, pnpm, `flock`, and a TeX Live installation that provides `pdflatex` and the T1 fonts. Install DSH `0.2.1-rc.2`. The Python environment provides the delivery audit. From the Idea2Paper source checkout, install the project into a virtual environment and point `IDEA2PAPER_PYTHON` at it.

From the Idea2Paper source checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
export IDEA2PAPER_PYTHON="$PWD/.venv/bin/python"
npm pack ./integrations/dsh
dsh plugin --profile idea2paper add "$PWD/idea2paper-dsh-0.1.0-alpha.0.tgz"
dsh --profile idea2paper --help
```

Use this bundle in its own profile. Configure the DSH model and credentials for that profile. With the stock DeepSeek provider, supply `DEEPSEEK_API_KEY` in the environment. Keep the project directory within your workspace and give `run` a new directory name.

```sh
dsh --profile idea2paper run search-study "Compare the cost of search on ordered arrays"
dsh --profile idea2paper resume search-study
```

The project contains `workflow.json`, `results/experiment.json`, `paper/main.tex`, `paper/main.pdf`, and the delivery report at `auto_research/state/delivery_report.yaml`. A completed role's DSH session ID is recorded in the checkpoint. Resume requires the same model selection. It restarts an interrupted role and reuses completed stages and the saved measurements. A rejected review leaves the draft and feedback in the checkpoint and stops before PDF delivery. This preview covers a single local experiment, one independent review, and one provider configuration. Slurm and general research topics require further validation.

Run the packaged integration test with `python3 integrations/dsh/verify.py ./idea2paper-dsh-0.1.0-alpha.0.tgz`. The test starts DSH in a fresh profile, uses a loopback endpoint with scripted model responses, and checks actual artifacts and resume behavior.

See [the implementation plan](../../ADRs/0015-dsh-native-bundle.md) for the release gates.
