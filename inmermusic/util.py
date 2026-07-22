"""Pure formatting/parsing helpers (no package or discord dependencies)."""
import math
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
                result = parts[0] * 60 + parts[1]
                return result if math.isfinite(result) else None
            if len(parts) == 3:
                result = parts[0] * 3600 + parts[1] * 60 + parts[2]
                return result if math.isfinite(result) else None
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def friendly_extract_error(message: str) -> str:
    """Map a yt-dlp/extraction error string to a short Japanese explanation."""
    m = message.lower()
    if any(k in m for k in ("private", "login", "sign in", "members-only", "cookies")):
        return "ログインが必要な動画のため再生できません。"
    if any(k in m for k in (
            "age-restricted", "age restricted", "age verification", "confirm your age")):
        return "年齢制限付きの動画のため再生できません。"
    if any(k in m for k in ("geo", "not available in your country", "region")):
        return "地域制限により再生できません。"
    if any(k in m for k in ("unavailable", "removed", "deleted", "does not exist", "not found")):
        return "動画が削除・非公開のため見つかりません。"
    if any(k in m for k in ("timed out", "timeout", "connection", "unable to download", "network")):
        return "ネットワークエラーです。時間をおいて再試行してください。"
    return "取得に失敗しました。URLやキーワードを確認してください。"
