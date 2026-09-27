"""Turn-up scheduling arithmetic.

Pure functions only: whether a measured spool has reached its turn-up diameter
and how full it is, so the trigger rule can be tested without any machinery.
"""


def should_turn_up(diameter_value: float, target: float) -> bool:
    """A reading at or past the target diameter asks for a change."""
    return float(diameter_value) >= float(target)


def change_verdict(diameter_value: float, target: float) -> str:
    """Label used by the read side and the console."""
    return "turn_up" if should_turn_up(diameter_value, target) else "winding"


def fill_fraction(diameter_value: float, core: float, target: float) -> float:
    """How full the spool is, 0.0 on the core and 1.0 at the target diameter."""
    span = float(target) - float(core)
    if span <= 0.0:
        return 0.0
    fraction = (float(diameter_value) - float(core)) / span
    return min(1.0, max(0.0, fraction))
