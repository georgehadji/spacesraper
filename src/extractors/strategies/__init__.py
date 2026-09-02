# src/extractors/strategies/__init__.py
# Strategy registry — maps schema_id, domain patterns, and trigger conditions
# to concrete extraction strategies. New strategies register here.

from .generic import GenericStrategy
from .google_maps import GoogleMapsStrategy
from .google_maps_place import GoogleMapsPlaceStrategy
from .override import OverrideStrategy

__all__ = [
    "GenericStrategy",
    "GoogleMapsStrategy",
    "GoogleMapsPlaceStrategy",
    "OverrideStrategy",
]
