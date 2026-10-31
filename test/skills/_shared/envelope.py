"""Standard response envelope + date/code normalization helpers.

Stdlib-only (no pandas / requests) so the read-only query path runs on any
CPython. The envelope is the contract that makes every result *auditable*:
it always carries the data nature, the as-of timestamp, the source table,
coverage, and any limitations. A historical snapshot is never relabelled as a
live quote.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, List, Optional, Tuple

# Fixed compliance notice attached to every envelope.
DISCLAIMER = (
    "仅为数据查询与事实呈现，不构成任何投资建议、荐股或代客理财；"
    "历史数据不代表未来表现，据此操作风险自担。"
)

# Data nature vocabulary.
NATURE_HISTORICAL = "historical_snapshot"   # rows read from the local DB
NATURE_LIVE = "live_quote"                  # only produced by a running live feed
NATURE_NOT_AVAILABLE = "not_available"      # table empty / source unreachable
NATURE_MIXED = "mixed"                      # rows span several as-of dates
NATURE_REFERENCE = "reference_data"         # static / low-frequency reference

# Status vocabulary.
STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_LIMITED = "limited"
STATUS_ERROR = "error"


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})$")


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def today_iso() -> str:
    return _dt.date.today().isoformat()


def normalize_date(value: Any) -> Optional[Tuple[str, str]]:
    """Normalize many date shapes to ``(iso 'YYYY-MM-DD', compact 'YYYYMMDD')``.

    Accepts 'YYYYMMDD', 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS' and full ISO
    datetimes. Returns ``None`` when it cannot be parsed (never fabricates).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # datetime-like: take the leading date portion.
    head = text.split("T", 1)[0].split(" ", 1)[0]
    m = _DATE_RE.match(head)
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        iso = _dt.date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None
    return iso, iso.replace("-", "")


def iso_of(value: Any) -> Optional[str]:
    nd = normalize_date(value)
    return nd[0] if nd else None


def compact_of(value: Any) -> Optional[str]:
    nd = normalize_date(value)
    return nd[1] if nd else None


def calendar_days_between(a: Any, b: Any) -> Optional[int]:
    """Whole calendar days from a to b (b - a). ``None`` if either is invalid."""
    da, db = iso_of(a), iso_of(b)
    if not da or not db:
        return None
    pa = _dt.date.fromisoformat(da)
    pb = _dt.date.fromisoformat(db)
    return (pb - pa).days


def add_days(value: Any, days: int) -> Optional[str]:
    iso = iso_of(value)
    if not iso:
        return None
    return (_dt.date.fromisoformat(iso) + _dt.timedelta(days=days)).isoformat()


# --------------------------------------------------------------------------- #
# Code helpers
# --------------------------------------------------------------------------- #
def normalize_code(value: Any) -> Optional[str]:
    """Extract the bare 6-digit A-share code from any shape.

    '600519' / '600519.SH' / 'sh600519' / 'SH600519' all -> '600519'.
    """
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    digits = digits.zfill(6)[-6:]
    return digits if len(digits) == 6 else None


def code_match_clause(code_col: str, style: str) -> Tuple[str, tuple]:
    """Build a WHERE fragment matching a code regardless of table code style.

    style:
      - 'bare'   : code_col stores bare 6-digit codes (ohlcv, hsgt)
      - 'suffix' : code_col stores 'XXXXXX.SH/.SZ/.BJ' (daily_basic, moneyflow)
      - 'mixed'  : either form may appear (top10_floatholders)
    """
    raise NotImplementedError  # implemented in the gateway (needs the bare code)


# --------------------------------------------------------------------------- #
# Envelope builder
# --------------------------------------------------------------------------- #
def build_envelope(
    skill: str,
    function: str,
    request: Dict[str, Any],
    *,
    status: str = STATUS_OK,
    data: Any = None,
    data_nature: str = NATURE_HISTORICAL,
    source: Optional[str] = None,
    as_of: Optional[Dict[str, Any]] = None,
    coverage: Optional[Dict[str, Any]] = None,
    limitations: Optional[List[str]] = None,
    notes: Optional[List[str]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the standard, auditable response envelope."""
    envelope: Dict[str, Any] = {
        "skill": skill,
        "function": function,
        "request": request,
        "status": status,
        "data_nature": data_nature,
        "as_of": as_of or {},
        "source": source,
        "coverage": coverage or {},
        "data": data if data is not None else ([] if isinstance(data, (list, tuple)) else None),
        "limitations": limitations or [],
        "notes": notes or [],
        "disclaimer": DISCLAIMER,
        "generated_at": now_iso(),
    }
    if error:
        envelope["error"] = error
    return envelope


def rows_coverage(rows: List[Any], *, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cov = {"rows": len(rows or [])}
    if extra:
        cov.update(extra)
    return cov
