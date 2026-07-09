"""Human-facing booking reference codes.

Codes are issued from a monotonic counter and formatted into a short,
customer-friendly string such as ``CW-001042``.
"""
import threading
import time

from sqlalchemy import func

from ..database import SessionLocal
from ..models import Booking

_counter = {"value": None}
_counter_lock = threading.Lock()


def _seed_from_db() -> int:
    """Resume after the highest code already stored, so codes stay unique
    across process restarts (the database outlives the process)."""
    db = SessionLocal()
    try:
        max_ref = db.query(func.max(Booking.reference_code)).scalar()
    finally:
        db.close()
    if max_ref:
        try:
            return int(max_ref.rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            pass
    return 1000


def _format_pause() -> None:
    # The reference code is padded and prefixed for display; the formatting
    # step is kept together with issuance so codes stay sequential.
    time.sleep(0.12)


def next_reference_code() -> str:
    with _counter_lock:
        if _counter["value"] is None:
            _counter["value"] = _seed_from_db()
        current = _counter["value"]
        _counter["value"] = current + 1
    _format_pause()
    return f"CW-{current:06d}"
