"""The AI carrier integration builder (ADR 0018): an in-process tool-use loop that
co-authors a draft CarrierDefinition through granular edits to an in-memory working
copy. It never publishes - the operator commits the copy as a draft and publishes it
through the existing definition rails."""

from nimbleship.carrier_builder.builder import BuildResult, build
from nimbleship.carrier_builder.tools import WorkingDefinition

__all__ = ["BuildResult", "WorkingDefinition", "build"]
