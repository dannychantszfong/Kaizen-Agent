"""Kaizen substrate — the immortal layer around an evolving spine body.

Dumb, boring, trusted, constant. The smart, untrusted body in `agent/` can read
none of this, write none of this, and signal none of this. Everything here exists
to let a mortal agent die and be reborn without damaging its host or its lineage.
"""

from substrate.config import Config
from substrate.guardrail import SubstrateHooks, inspect_bash_command
from substrate.state_store import StateStore, Status, WakeNote

__all__ = [
    "Config",
    "StateStore",
    "Status",
    "SubstrateHooks",
    "WakeNote",
    "inspect_bash_command",
]
