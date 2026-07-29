"""Compatibility exports for the model domain.

New code should import from :mod:`synapse.models.registry`.
"""

from synapse.models.registry import *  # noqa: F403
from synapse.models.registry import _profiles_from_mapping as _profiles_from_mapping
