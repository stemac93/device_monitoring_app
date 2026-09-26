from fractions import Fraction
from datetime import datetime, timedelta
import logging as logger
from django.utils import timezone
import pytz

# Helper to sanitize variable names
def sanitize_variable_name(name):
    return name.replace("-", "_").replace(" ", "_")

# Helper to round float values to 2 decimal places
def round_to_2_decimals(value):
    """Round a numeric value to 2 decimal places"""
    try:
        return round(float(value), 2)
    except (ValueError, TypeError):
        return 0.0

# Helper to convert raw value to float
def convert_value(raw_value, conversion_factor):
    try:
        logger.info(f"Conv factor from mapping: {conversion_factor}")
        if conversion_factor.__contains__("/"):
            conversion_factor = float(Fraction(conversion_factor))
        else:
            conversion_factor = float(conversion_factor)
    except (ValueError, TypeError, ZeroDivisionError):
        logger.warning(f"Invalid conversion factor: {conversion_factor}. Defaulting to 0.")
        conversion_factor = 0.0
    logger.info(f"Conversion factor: {conversion_factor}")
    result = raw_value * conversion_factor
    return round_to_2_decimals(result)

def local_period_starts(now=None):
    """Inizio (aware, ora locale) di giorno, settimana (lunedì) e mese correnti.

    Django confronta correttamente datetime aware con i timestamp UTC del DB,
    quindi non serve riconvertire in UTC.
    """
    now_local = timezone.localtime(now or timezone.now())
    start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = start_of_day - timedelta(days=start_of_day.weekday())
    start_of_month = start_of_day.replace(day=1)
    # replace()/timedelta non ricalcolano l'offset DST: rilocalizzo
    tz = timezone.get_current_timezone()
    return tuple(
        timezone.make_aware(dt.replace(tzinfo=None), tz)
        for dt in (start_of_day, start_of_week, start_of_month)
    )

def convert_to_local_time(utc_dt):
    if timezone.is_aware(utc_dt):  # Se il datetime è già timezone-aware
        return timezone.localtime(utc_dt)
    else:  # Se il datetime è naive, assumiamo che sia UTC
        utc_dt = pytz.utc.localize(utc_dt)
        return timezone.localtime(utc_dt)