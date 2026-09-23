from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ReportingWindow:
    start: datetime
    end: datetime

    def __post_init__(self):
        start = as_utc(self.start)
        end = as_utc(self.end)
        if end <= start:
            raise ValueError("reporting window end must be after its start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @classmethod
    def from_anchor(cls, anchor: datetime, window_days: int):
        if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
            raise ValueError("window_days must be a positive integer")
        end = as_utc(anchor)
        return cls(end - timedelta(days=window_days), end)

    def contains(self, value: datetime) -> bool:
        value = as_utc(value)
        return self.start <= value < self.end

    def contains_epoch(self, value: int) -> bool:
        return self.contains(datetime.fromtimestamp(value, timezone.utc))
