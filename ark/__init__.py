"""ARK - Automatic Research Kit: AI-powered idea-to-paper automation"""
__version__ = "0.1.1"

import threading

# Set when the run is told to stop (the orchestrator's SIGTERM handler). A
# signal only reaches the main thread, so worker threads (parallel
# experimenters, SLURM waits, retry backoffs) check this and wind down instead
# of starting new work.
shutting_down = threading.Event()
