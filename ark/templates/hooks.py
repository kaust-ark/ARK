# Project-specific hooks
# Override these functions to customize behavior for your research project.


def run_research_iteration(orch) -> bool:
    """Custom research iteration logic.

    Args:
        orch: Orchestrator instance

    Returns:
        True to continue, False to stop
    """
    raise NotImplementedError(
        "Define your research iteration logic here. "
        "See projects/GAC/hooks.py for an example."
    )


def generate_figures_from_results(orch) -> bool:
    """Generate figures from experiment results.

    Args:
        orch: Orchestrator instance

    Returns:
        True if figures were generated successfully
    """
    return False
