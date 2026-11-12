"""Running one pull request through a review, start to finish.

`record` owns the database and GitHub side; the two run modules produce a
ReviewOutcome and touch neither, which is what lets the evaluation harness
drive them directly.
"""

from src.pipeline.record import process_pull_request

__all__ = ["process_pull_request"]
