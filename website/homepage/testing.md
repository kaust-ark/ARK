# Testing

## Quick Run

```bash
pip install -e .
pip install pytest

# All tests
python -m pytest tests/ -v

# Integration only (full pipeline, all external calls mocked)
python -m pytest tests/test_integration.py -v -s
```

## Test Suite (84 tests)

| File | Tests | Coverage |
|------|-------|----------|
| `test_config.py` | 10 | LaTeX geometry presets, matplotlib rcParams, figure config generation |
| `test_memory.py` | 14 | Score tracking, stagnation detection, issue-repeat tracking, save/load |
| `test_orchestrator.py` | 22 | Score parsing, issue extraction, decision logic, action plan validation |
| `test_dev.py` | 30 | Development mode utilities, dev agent logic |
| `test_integration.py` | 8 | Full Compile → Review → Plan & Execute → Visualize cycle (end-to-end) |

## What Integration Tests Cover

The integration tests mock all external calls:
- **Subprocess mocks**: Claude CLI, Gemini CLI, pdflatex, bibtex, git
- **File system**: Creates temp project directories with config + state files
- **Full cycle**: Runs a complete paper iteration through all 4 steps
- **Assertions**: State files, score parsing, agent call sequences, cost tracking

No API keys, GPU, or LaTeX installation needed to run the full suite.
