from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import holidays

WORKDAY_START = time(9, 0)
WORKDAY_END = time(18, 0)
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

_br_holidays_cache = {}


def _holidays_for_years(years):
    missing = [year for year in years if year not in _br_holidays_cache]
    if missing:
        _br_holidays_cache.update(holidays.Brazil(years=missing))
    return _br_holidays_cache


def _is_business_day(date, br_holidays):
    return date.weekday() < 5 and date not in br_holidays


def business_hours_between(start: datetime, end: datetime) -> float:
    """Horas úteis (seg-sex, 9h-18h, sem feriados nacionais BR) entre dois timestamps."""
    if start is None or end is None:
        return 0.0

    # O changelog do Jira vem em -03:00, mas a API de sprint (startDate/endDate)
    # vem em UTC ("Z") — convertemos ambos para o mesmo fuso local antes de
    # comparar em horário "de parede" (naive), senão a conta erra até 3h.
    if start.tzinfo is not None:
        start = start.astimezone(LOCAL_TZ).replace(tzinfo=None)
    if end.tzinfo is not None:
        end = end.astimezone(LOCAL_TZ).replace(tzinfo=None)

    if end <= start:
        return 0.0

    years = range(start.year, end.year + 1)
    br_holidays = _holidays_for_years(years)

    total_hours = 0.0
    current_date = start.date()

    while current_date <= end.date():
        if _is_business_day(current_date, br_holidays):
            day_start = datetime.combine(current_date, WORKDAY_START)
            day_end = datetime.combine(current_date, WORKDAY_END)

            window_start = max(day_start, start)
            window_end = min(day_end, end)

            if window_end > window_start:
                total_hours += (window_end - window_start).total_seconds() / 3600

        current_date += timedelta(days=1)

    return round(total_hours, 2)
