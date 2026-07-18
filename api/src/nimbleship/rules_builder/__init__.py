"""The AI rules builder (ADR 0017): an in-process tool-use loop that co-authors a
draft rulebook through granular edits to an in-memory working copy, dry-running the
copy against historical orders. It never publishes - the operator commits the copy
as a draft and publishes it through the existing rulebook rails."""

from nimbleship.rules_builder.builder import BuildResult, build
from nimbleship.rules_builder.tools import WorkingCopy

__all__ = ["BuildResult", "WorkingCopy", "build"]
