import ast
import math
import operator
from fractions import Fraction
from datetime import datetime, timedelta
import logging as logger
from django.utils import timezone
import pytz

# Helper to sanitize variable names
def sanitize_variable_name(name):
    return name.replace("-", "_").replace(" ", "_")

# Valutazione sicura delle formule delle ComputedVariable: solo aritmetica sulle
# variabili del device. sympify usava eval() e permetteva di eseguire codice.
_FORMULA_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitXor: operator.pow,  # "^" era potenza con sympify: stesso significato
}
_FORMULA_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FORMULA_FUNCS = {"abs": abs, "min": min, "max": max, "sqrt": math.sqrt, "round": round}


def evaluate_formula(formula, variables):
    """Valuta `formula` (es. "Pin - Pout", "(V * I) / 1000") con i valori in
    `variables` ({nome: numero}). Solleva ValueError/ZeroDivisionError se la
    formula non è valida, usa una variabile assente o divide per zero."""

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise ValueError(f"Unknown variable '{node.id}'")
            return float(variables[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _FORMULA_BIN_OPS:
            left, right = ev(node.left), ev(node.right)
            if type(node.op) in (ast.Pow, ast.BitXor) and abs(right) > 100:
                raise ValueError("Exponent too large")  # evita calcoli enormi
            return _FORMULA_BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _FORMULA_UNARY_OPS:
            return _FORMULA_UNARY_OPS[type(node.op)](ev(node.operand))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _FORMULA_FUNCS and not node.keywords):
            return _FORMULA_FUNCS[node.func.id](*[ev(arg) for arg in node.args])
        raise ValueError(f"Unsupported element in formula: {type(node).__name__}")

    try:
        tree = ast.parse(str(formula).strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Invalid formula syntax: {e}") from e
    return float(ev(tree))

# Helper to round float values to 2 decimal places
def round_to_2_decimals(value):
    """Round a numeric value to 2 decimal places"""
    try:
        return round(float(value), 2)
    except (ValueError, TypeError):
        return 0.0

# Helper to convert raw value to float
def convert_value(raw_value, conversion_factor):
    """Ritorna il valore convertito, o None se il fattore non è valido (un
    fattore sbagliato non deve produrre uno 0 che sembra una lettura reale)."""
    try:
        logger.info(f"Conv factor from mapping: {conversion_factor}")
        if conversion_factor is None or str(conversion_factor).strip() == "":
            conversion_factor = 1.0  # campo non compilato: nessuna conversione
        else:
            # Accetta anche la virgola decimale ("0,1")
            conversion_factor = str(conversion_factor).strip().replace(",", ".")
            if "/" in conversion_factor:
                conversion_factor = float(Fraction(conversion_factor))
            else:
                conversion_factor = float(conversion_factor)
    except (ValueError, TypeError, ZeroDivisionError):
        logger.warning(f"Invalid conversion factor: {conversion_factor}")
        return None
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