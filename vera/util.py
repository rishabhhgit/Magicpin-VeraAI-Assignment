"""Formatting + parsing helpers shared by the composer and reply engine.

Everything here is pure and deterministic: no randomness, no system clock.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

# The dataset's "today". Used when a caller does not supply `now`.
DEFAULT_NOW = "2026-04-26T10:30:00Z"

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_NO = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def parse_dt(value, default=None):
    """Parse an ISO-8601-ish string. Tolerates trailing Z (py3.9 safe)."""
    if isinstance(value, datetime):
        return value
    if not value or not isinstance(value, str):
        return default
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d %b %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return default


def now_dt(now=None):
    """Resolve the reference time. Never reads the system clock."""
    dt = parse_dt(now) if now else None
    if dt is None:
        dt = parse_dt(DEFAULT_NOW)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def stable_hash(text) -> int:
    """Deterministic int hash (stable across runs / platforms)."""
    return int(hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:8], 16)


def pick(options, key):
    """Deterministic pick from a non-empty sequence."""
    seq = list(options)
    if not seq:
        return None
    return seq[stable_hash(key) % len(seq)]


def nfmt(value):
    """Indian digit grouping: 2410 -> '2,410', 1234567 -> '12,34,567'."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if n < 0 else ""
    n = abs(n)
    s = str(n)
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts) + "," + tail


def inr(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v == int(v):
        return "₹" + nfmt(int(v))
    return "₹" + nfmt(v)


def pct(value, digits=0):
    """0.021 -> '2.1%' / '2%' depending on digits (auto-tightens)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    p = v * 100 if abs(v) <= 1.5 else v
    if digits == 0 and abs(p - round(p)) > 1e-9:
        digits = 1
    return f"{round(p, digits):.{digits}f}%"


def fmt_delta(value):
    """-0.5 -> 'down 50%'; 0.18 -> 'up 18%'."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    p = v * 100 if abs(v) <= 1.5 else v
    if abs(p) < 0.5:
        return "flat"
    return f"up {abs(p):.0f}%" if p > 0 else f"down {abs(p):.0f}%"


def _naive_utc(dt):
    """Compare dates safely: drop tzinfo after normalising to UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def months_between(start, end):
    """Whole months between two dates (start <= end). None if unparsable."""
    a, b = _naive_utc(parse_dt(start)), _naive_utc(parse_dt(end))
    if not a or not b:
        return None
    if b < a:
        a, b = b, a
    months = (b.year - a.year) * 12 + (b.month - a.month)
    if b.day < a.day:
        months -= 1
    return max(months, 0)


def days_between(start, end):
    a, b = _naive_utc(parse_dt(start)), _naive_utc(parse_dt(end))
    if not a or not b:
        return None
    return (b - a).days


def fmt_date(value, weekday=False):
    dt = parse_dt(value)
    if not dt:
        return str(value) if value else ""
    label = f"{dt.day} {_MONTHS[dt.month - 1]}"
    if dt.year != now_dt().year:
        label += f" {dt.year}"
    if weekday:
        return f"{_WEEKDAYS[dt.weekday()]} {label}"
    return label


def fmt_time(value):
    dt = parse_dt(value)
    if not dt:
        return ""
    hour = dt.hour % 12 or 12
    mer = "am" if dt.hour < 12 else "pm"
    if dt.minute:
        return f"{hour}:{dt.minute:02d}{mer}"
    return f"{hour}{mer}"


def month_of(value):
    dt = parse_dt(value) if not isinstance(value, int) else None
    if isinstance(value, int) and 1 <= value <= 12:
        return value
    return dt.month if dt else None


def beat_matches(month_range, month):
    """Does a seasonal_beats month_range ('Nov-Feb', 'Jan', 'Feb 14') hit `month`?"""
    if not month_range or not month:
        return False
    text = str(month_range).lower()
    found = []
    for name, num in _MONTH_NO.items():
        if re.search(r"\b" + name, text):
            found.append(num)
    if not found:
        return False
    if len(found) == 1:
        return found[0] == month
    # range: check wrap-around (Nov-Feb)
    a, b = found[0], found[-1]
    if a <= b:
        return a <= month <= b
    return month >= a or month <= b


def humanize(key):
    if not key:
        return ""
    text = str(key).replace("_", " ").replace("-", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def snake_to_sentence(key):
    text = humanize(key)
    return text[:1].upper() + text[1:] if text else text


def strip_urls(text):
    return _URL_RE.sub("", text or "")


def squeeze(text):
    return _WS_RE.sub(" ", (text or "").strip())


def squeeze_lines(text):
    """Collapse runs of whitespace but keep explicit line breaks (draft bullets)."""
    return "\n".join(squeeze(part) for part in str(text or "").split("\n"))


def sentence(text):
    """Ensure terminal punctuation."""
    t = squeeze(text)
    if not t:
        return t
    if t[-1] not in ".!?":
        t += "."
    return t


def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def get_path(obj, *keys, default=None):
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
