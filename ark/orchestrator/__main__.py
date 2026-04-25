"""Entry point for ``python -m ark.orchestrator``.

Delegates to :func:`ark.orchestrator.core.main`. The slurm template
invokes ``python -m ark.orchestrator …`` so this file is required for
production job submission.
"""
from .core import main


if __name__ == "__main__":
    main()
