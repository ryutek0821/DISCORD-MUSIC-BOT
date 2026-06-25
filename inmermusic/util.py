"""Pure formatting/parsing helpers (no package or discord dependencies)."""
from typing import Optional


def fmt_duration(seconds: float) -> str:
    """Format seconds as m:ss (or h:mm:ss past an hour)."""
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(value: str) -> Optional[float]:
    """Parse '90', '1:30' or '1:02:03' into seconds. Returns None if invalid."""
    value = value.strip()
    if not value:
        return None
    try:
        if ":" in value:
            parts = [float(p) for p in value.split(":")]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            return None
        return float(value)
    except ValueError:
        return None
