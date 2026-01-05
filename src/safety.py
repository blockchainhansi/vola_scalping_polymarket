"""
Safety and risk management utilities for the Polymarket box spread market maker.

For 15-minute markets, we use limit orders only (no market orders).
Emergency handling cancels traps and places aggressive limit hedges.
"""

from datetime import datetime, timedelta
from typing import Optional

from config import Config
from models import InventoryState


def exposure_exceeded(inventory: InventoryState, config: Config) -> bool:
    """Check if |ΔQ| exceeds configured maximum exposure."""
    return abs(inventory.delta_q) > config.max_exposure


def is_in_expiry_buffer(config: Config) -> bool:
    """
    Check if we're within the expiry buffer window.
    During this time, stop placing new traps - only hedge existing exposure.
    """
    return config.is_in_expiry_buffer()


def is_in_final_exit(config: Config) -> bool:
    """
    Check if we're in final exit window.
    Cancel all orders and stop trading - let positions settle at expiry.
    """
    return config.is_in_final_exit()


def should_cooldown(now: datetime, cooldown_until: Optional[datetime]) -> bool:
    """Return True if we are in cooldown window after emergency."""
    if cooldown_until is None:
        return False
    return now < cooldown_until


def seconds_until_expiry(config: Config) -> float:
    """Get seconds remaining until market expiry."""
    return config.time_until_expiry().total_seconds()
