#!/usr/bin/env python3
"""
Genera i preset JSON di user_devices/presets/inverter/ dalle definizioni
Modbus di progetti open source per Home Assistant:

  - ha-solarman (davidrapan, MIT): inverter_definitions/*.yaml
  - home_assistant_solarman (StephanJoubert, Apache-2.0): solo i file che
    non esistono in ha-solarman
  - homeassistant-solax-modbus (wills106, Apache-2.0): plugin_*.py, un preset
    per ogni famiglia di modelli riconosciuta dal plugin

Uso (gira sull'host, non serve Django; richiede PyYAML):

    git clone --depth 1 https://github.com/davidrapan/ha-solarman /tmp/up/ha-solarman
    git clone --depth 1 https://github.com/StephanJoubert/home_assistant_solarman /tmp/up/home_assistant_solarman
    git clone --depth 1 https://github.com/wills106/homeassistant-solax-modbus /tmp/up/homeassistant-solax-modbus
    python tools/build_presets.py \\
        --solarman /tmp/up/ha-solarman \\
        --solarman-legacy /tmp/up/home_assistant_solarman \\
        --solax /tmp/up/homeassistant-solax-modbus

Lo script cancella e rigenera tutti i JSON della cartella di output.

Cosa viene importato: solo misure numeriche lette da input/holding register
(16/32/64 bit, con segno o senza, scala e offset). Vengono scartati entità
scrivibili (number/select/switch/time/button), stringhe, enum/lookup, bit e
maschere, valori calcolati da funzioni Python e sensori composti.
Se un valore a 32 bit ha i due registri non consecutivi, diventa due variabili
a 16 bit (" lo"/" hi") più una variabile calcolata che le somma.
Ogni preset ha inoltre una variabile calcolata "Power" (alias della potenza
AC di uscita) usata da compute_energy per l'integrale di energia.
"""

import argparse
import asyncio
import logging
import dataclasses
import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "user_devices" / "presets" / "inverter"

MODBUS_MAX_READ = 125
MIN_VARIABLES = 3  # preset con meno variabili non servono a nulla

# Nomi della potenza AC di uscita, in ordine di preferenza, per l'alias "Power"
POWER_CANDIDATES = [
    "Output Power", "Inverter Power", "Total Active Power", "Active Power",
    "AC Power", "AC Output Power", "Output AC Power", "Output Active Power",
    "Inverter Output Power", "Total AC Power", "Total Output Power", "Inverter Active Power",
    "Inverter AC Power", "Total AC Output Power Active", "Active Power Output Total",
    "ActivePower_Output_Total", "Output active power", "ActivePower", "Generation Power",
    "PV Instant Generated PW", "Total Power",
]
# Se manca il totale: somma delle potenze di fase con questi nomi ({} = 1, 2, 3)
POWER_PHASE_PATTERNS = ["Inverter L{} Power", "Inverter Power L{}", "Output L{} Power", "L{} Power"]
# Ultima scelta: potenza fotovoltaica, totale o somma delle stringhe
POWER_FALLBACK = ["PV Power", "PV Power Total", "PV Total Power", "Total DC Power"]
POWER_PV_PATTERNS = ["PV Power {}", "PV{} Power"]


# --------------------------------------------------------------------------
# Utility comuni
# --------------------------------------------------------------------------

def clean_name(name) -> str:
    """Nome leggibile e utilizzabile nelle formule sympy (dopo sanitize_variable_name)."""
    name = re.sub(r"[^A-Za-z0-9 _-]+", " ", str(name))
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return ""
    if name[0].isdigit():
        name = "V " + name
    return name


def sanitized(name: str) -> str:
    # Stessa trasformazione di helper_funcs.sanitize_variable_name
    return name.replace("-", "_").replace(" ", "_")


def fmt_number(value) -> str:
    """Fattore di conversione come stringa senza errori di virgola mobile."""
    value = float(value)
    if value == int(value):
        return str(int(value))
    return f"{value:.10g}"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def git_commit(path: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def make_variable(name, register_type, address, bit_length, signed, endianness, scale=1, offset=0, unit=""):
    return {
        "name": name,
        "register_type": register_type,
        "address": f"0x{address:04X}",
        "unit": unit or "",
        "conversion_factor": fmt_number(scale),
        "offset": offset,
        "bit_length": bit_length,
        "is_signed": bool(signed),
        "endianness": endianness,
    }


def registers_to_variables(name, register_type, registers, signed, scale, offset, unit):
    """
    `registers` in ordine di peso crescente (prima la word meno significativa),
    come in ha-solarman. Ritorna (variabili, variabili_calcolate).
    """
    n = len(registers)
    if n == 1:
        return [make_variable(name, register_type, registers[0], 16, signed, "big", scale, offset, unit)], []
    if n in (2, 4):
        if registers == list(range(registers[0], registers[0] + n)):
            # word meno significativa all'indirizzo più basso
            return [make_variable(name, register_type, registers[0], 16 * n, signed, "little", scale, offset, unit)], []
        if registers == list(range(registers[0], registers[0] - n, -1)):
            # word più significativa all'indirizzo più basso
            return [make_variable(name, register_type, registers[-1], 16 * n, signed, "big", scale, offset, unit)], []
    if n == 2:
        # Registri non consecutivi: lo (senza segno) + hi * 65536 (con il segno del valore).
        # Scala e offset stanno sulle due variabili perché la formula non può contenere
        # "-" né spazi (compute_variables li trasforma in "_").
        lo, hi = f"{name} lo", f"{name} hi"
        return (
            [make_variable(lo, register_type, registers[0], 16, False, "big", scale, offset, unit),
             make_variable(hi, register_type, registers[1], 16, signed, "big", float(scale) * 65536, 0, unit)],
            [{"name": name, "unit": unit or "", "formula": f"{sanitized(lo)}+{sanitized(hi)}"}],
        )
    return None, None


def build_blocks(variables, min_span=25, max_size=MODBUS_MAX_READ, newblock_addrs=()):
    """
    Raggruppa i registri usati in blocchi di lettura, come fa ha-solarman:
    nuovo blocco se il buco supera min_span (min_span < 0: mai) o se il blocco
    supererebbe max_size registri.
    """
    max_size = min(max_size, MODBUS_MAX_READ)
    blocks = []
    for register_type in ("input", "holding"):
        regs = set()
        for v in variables:
            if v["register_type"] != register_type:
                continue
            start = int(v["address"], 16)
            regs.update(range(start, start + v["bit_length"] // 16))
        newblock = {a for (t, a) in newblock_addrs if t == register_type}
        start = prev = None
        for reg in sorted(regs):
            if start is not None:
                gap_split = min_span >= 0 and reg - prev > min_span
                if gap_split or reg - start + 1 > max_size or reg in newblock:
                    blocks.append((register_type, start, prev))
                    start = None
            if start is None:
                start = reg
            prev = reg
        if start is not None:
            blocks.append((register_type, start, prev))
    return [
        {"register_type": t, "start_address": f"0x{s:04X}", "word_count": e - s + 1}
        for t, s, e in blocks
    ]


def add_power_alias(variables, computed):
    """
    compute_energy integra la variabile "Power": se il preset non ce l'ha,
    aggiunge una variabile calcolata "Power" in W (alias della potenza AC di
    uscita, o somma delle fasi, o in mancanza potenza fotovoltaica).
    Ritorna la sorgente scelta.
    """
    for v in variables + computed:
        if v["name"] in ("Power", "P"):
            v["show_on_graph"] = v["show_in_homepage"] = True
            return v["name"]
    by_lower = {}
    for v in variables + computed:
        if v.get("unit") in ("W", "kW"):
            by_lower.setdefault(v["name"].lower(), v)

    def term(v):
        return sanitized(v["name"]) + ("*1000" if v["unit"] == "kW" else "")

    def alias(sources, label):
        computed.append({
            # niente spazi: compute_variables li trasforma in "_"
            "name": "Power", "unit": "W", "formula": "+".join(term(v) for v in sources),
            "show_on_graph": True, "show_in_homepage": True,
        })
        return label

    def series(pattern, start=1):
        found = []
        i = start
        while (v := by_lower.get(pattern.format(i).lower())):
            found.append(v)
            i += 1
        return found

    for candidate in POWER_CANDIDATES:
        if (source := by_lower.get(candidate.lower())):
            return alias([source], source["name"])
    for pattern in POWER_PHASE_PATTERNS:
        phases = series(pattern)
        if len(phases) in (2, 3):
            return alias(phases, pattern.format(f"1..{len(phases)}"))
    for candidate in POWER_FALLBACK:
        if (source := by_lower.get(candidate.lower())):
            return alias([source], source["name"])
    for pattern in POWER_PV_PATTERNS:
        strings = series(pattern)
        if strings:
            return alias(strings, pattern.format(f"1..{len(strings)}"))
    return None


def write_preset(out_dir, preset_id, manufacturer, model, source, variables, computed, blocks, notes=None):
    if len(variables) < MIN_VARIABLES:
        return None
    power_source = add_power_alias(variables, computed)
    preset = {
        "id": f"inverter/{preset_id}",
        "category": "inverter",
        "manufacturer": manufacturer,
        "model": model,
        "source": source,
        "power_variable": power_source,
        "notes": notes or [],
        "blocks": blocks,
        "variables": variables,
        "computed": computed,
    }
    path = out_dir / f"{preset_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(preset, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return preset


# --------------------------------------------------------------------------
# Solarman YAML
# --------------------------------------------------------------------------

SOLARMAN_SKIP_KEYS = ("lookup", "mask", "bit", "bitmask", "divide", "magnitude", "sensors", "value", "configurable")


def request_code(request, default_code):
    return request.get("code", request.get("mb_functioncode", default_code))


def solarman_code(item, default_code, code_table):
    """Come ha-solarman: code dell'item, poi tabella dai `requests`, poi default."""
    code = item.get("code")
    if isinstance(code, dict):
        code = code.get("read")
    if code is None:
        code = code_table.get(item["registers"][0], default_code)
    return {3: "holding", 4: "input"}.get(code)


def convert_solarman_file(path: Path, source_base: dict, manufacturer_hint: str):
    profile = yaml.safe_load(open(path, encoding="utf-8"))
    default = profile.get("default") or {}
    default_code = default.get("code", 3)
    variables, computed, skipped = [], [], 0
    seen = set()
    # Senza requests_fine_control i `requests` dicono solo il codice funzione per indirizzo
    fine_control = "requests_fine_control" in profile and profile.get("requests")
    code_table = {}
    if profile.get("requests") and not fine_control:
        for req in profile["requests"]:
            for addr in range(int(req["start"]), int(req["end"]) + 1):
                code_table[addr] = request_code(req, default_code)

    for group in profile.get("parameters", []):
        for item in group.get("items", []):
            name = clean_name(item.get("name", ""))
            registers = item.get("registers")
            if (
                not name or name in seen or not registers
                or item.get("platform") not in (None, "sensor")
                or "attribute" in item
                or item.get("rule") not in (1, 2, 3, 4)
                or any(k in item for k in SOLARMAN_SKIP_KEYS)
                or not all(isinstance(r, int) for r in registers)
                or not isinstance(item.get("scale", 1), (int, float))
                or not isinstance(item.get("offset", 0) or 0, (int, float))
            ):
                skipped += 1
                continue
            register_type = solarman_code(item, default_code, code_table)
            if register_type is None:
                skipped += 1
                continue
            signed = item["rule"] in (2, 4)
            new_vars, new_computed = registers_to_variables(
                name, register_type, list(registers), signed,
                item.get("scale", 1), item.get("offset", 0) or 0, item.get("uom", ""),
            )
            if new_vars is None:
                skipped += 1
                continue
            seen.add(name)
            variables += new_vars
            computed += new_computed

    if fine_control:
        # Il file definisce esplicitamente i blocchi di lettura
        blocks = []
        for req in profile["requests"]:
            register_type = {3: "holding", 4: "input"}.get(request_code(req, default_code))
            start, end = int(req["start"]), int(req["end"])
            for s in range(start, end + 1, MODBUS_MAX_READ):
                e = min(end, s + MODBUS_MAX_READ - 1)
                blocks.append({"register_type": register_type, "start_address": f"0x{s:04X}", "word_count": e - s + 1})
    else:
        blocks = build_blocks(variables, default.get("min_span", 25), default.get("max_size", MODBUS_MAX_READ))

    info = profile.get("info") or {}
    manufacturer = info.get("manufacturer") or manufacturer_hint
    model = info.get("model") or path.stem.split("_", 1)[-1]
    if isinstance(model, list):
        model = ", ".join(model)
    model = f"{model} ({path.stem})"
    return manufacturer, model, variables, computed, blocks, skipped


SOLARMAN_MANUFACTURERS = {
    "afore": "Afore", "anenji": "Anenji", "astro-energy": "Astro-Energy", "chint": "CHINT",
    "deye": "Deye", "hinen": "Hinen", "invt": "INVT", "kstar": "KSTAR", "maxge": "Maxge",
    "megarevo": "Megarevo", "pylontech": "Pylontech", "renon": "Renon", "sofar": "Sofar",
    "solarman": "Solarman", "solis": "Solis", "srne": "SRNE", "swatten": "Swatten",
    "tsun": "TSUN", "zcs": "ZCS Azzurro", "hyd-zss-hp-3k-6k": "Sofar / ZCS",
}


def solarman_manufacturer(stem: str) -> str:
    key = stem.lower()
    for prefix, name in SOLARMAN_MANUFACTURERS.items():
        if key == prefix or key.startswith(prefix + "_") or key.startswith(prefix):
            return name
    return stem.split("_")[0].capitalize()


def build_solarman(repo: Path, out_dir: Path, project: str, url: str, license_: str, skip_stems=()):
    defs = repo / "custom_components" / "solarman" / "inverter_definitions"
    commit = git_commit(repo)
    done = []
    for path in sorted(defs.glob("*.yaml")):
        if path.stem.lower() in skip_stems:
            continue
        manufacturer, model, variables, computed, blocks, skipped = convert_solarman_file(
            path, {}, solarman_manufacturer(path.stem))
        source = {
            "project": project, "url": url, "license": license_, "commit": commit,
            "file": f"custom_components/solarman/inverter_definitions/{path.name}",
        }
        preset_id = f"solarman_{slug(path.stem)}"
        preset = write_preset(out_dir, preset_id, manufacturer, model, source, variables, computed, blocks)
        if preset:
            done.append((preset_id, len(variables), skipped, preset["power_variable"]))
    return done


# --------------------------------------------------------------------------
# homeassistant-solax-modbus
# --------------------------------------------------------------------------

UNITS = {
    "UnitOfPower": {"WATT": "W", "KILO_WATT": "kW", "MEGA_WATT": "MW"},
    "UnitOfEnergy": {"WATT_HOUR": "Wh", "KILO_WATT_HOUR": "kWh", "MEGA_WATT_HOUR": "MWh"},
    "UnitOfElectricPotential": {"VOLT": "V", "MILLIVOLT": "mV"},
    "UnitOfElectricCurrent": {"AMPERE": "A", "MILLIAMPERE": "mA"},
    "UnitOfFrequency": {"HERTZ": "Hz"},
    "UnitOfTemperature": {"CELSIUS": "°C", "FAHRENHEIT": "°F", "KELVIN": "K"},
    "UnitOfApparentPower": {"VOLT_AMPERE": "VA", "KILO_VOLT_AMPERE": "kVA"},
    "UnitOfReactivePower": {"VOLT_AMPERE_REACTIVE": "var", "KILO_VOLT_AMPERE_REACTIVE": "kvar"},
    "UnitOfTime": {"SECONDS": "s", "MINUTES": "min", "HOURS": "h", "DAYS": "d"},
}
CONSTS = {"PERCENTAGE": "%", "POWER_VOLT_AMPERE_REACTIVE": "var"}


class Sym:
    """Valore simbolico per qualunque nome importato da Home Assistant."""

    def __init__(self, path):
        self._path = path

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return Sym(f"{self._path}.{attr}")

    def __call__(self, *a, **k):
        return Sym(f"{self._path}()")

    def __or__(self, other):
        return self

    __ror__ = __or__

    def __iter__(self):
        return iter(())

    def __mro_entries__(self, bases):
        return (object,)

    def unit(self):
        parts = self._path.split(".")
        if len(parts) >= 2 and parts[-2] in UNITS:
            return UNITS[parts[-2]].get(parts[-1], "")
        return CONSTS.get(parts[-1], "")

    def __repr__(self):
        return f"Sym({self._path})"


def collect_description_kwargs(plugin_dir: Path):
    """Tutti i nomi di keyword usati nelle chiamate *EntityDescription(...) dei plugin."""
    import ast
    names = set()
    for path in plugin_dir.glob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if fname.endswith("EntityDescription") or fname == "replace":
                    names.update(k.arg for k in node.keywords if k.arg)
    return names


def install_ha_stubs(plugin_dir: Path):
    """Moduli homeassistant.* finti, sufficienti a importare const.py e i plugin."""
    kwargs = collect_description_kwargs(plugin_dir) | {
        "key", "name", "translation_key", "device_class", "state_class", "entity_category",
        "native_unit_of_measurement", "icon", "entity_registry_enabled_default", "options",
    }
    fields = [(k, object, dataclasses.field(default=None)) for k in sorted(kwargs)]
    description_base = dataclasses.make_dataclass(
        "EntityDescription", fields, kw_only=True, frozen=True)

    class StubModule(types.ModuleType):
        def __getattr__(self, attr):
            if attr.startswith("__"):
                raise AttributeError(attr)
            if attr.endswith("EntityDescription"):
                return description_base
            if attr.endswith("Error") or attr.endswith("Exception"):
                return type(attr, (Exception,), {})
            if attr in CONSTS:
                return CONSTS[attr]
            return Sym(attr)

    class Finder:
        def find_module(self, name, path=None):
            return None

        def find_spec(self, name, path=None, target=None):
            if name == "homeassistant" or name.startswith("homeassistant."):
                return importlib.util.spec_from_loader(name, self)
            return None

        def create_module(self, spec):
            mod = StubModule(spec.name)
            mod.__path__ = []
            return mod

        def exec_module(self, module):
            pass

    sys.meta_path.insert(0, Finder())

    # Pacchetto custom_components.solax_modbus senza eseguire __init__.py
    for pkg, path in (("custom_components", plugin_dir.parent), ("custom_components.solax_modbus", plugin_dir)):
        mod = types.ModuleType(pkg)
        mod.__path__ = [str(path)]
        sys.modules[pkg] = mod


class FakeResponse:
    def __init__(self, count):
        self.registers = [0x4141] * count

    def isError(self):
        return False


class FakeHub:
    name = "preset-builder"
    _modbus_addr = 1
    modbus_protocol_version = None

    def __init__(self):
        self.seriesnumber = None
        self._seriesnumber = None

    async def async_read_holding_registers(self, unit=None, address=0, count=1, **kw):
        return FakeResponse(count)

    async def async_read_input_registers(self, unit=None, address=0, count=1, **kw):
        return FakeResponse(count)

    def __getattr__(self, attr):
        return None


def serial_candidates(module, source: str):
    """Prefissi seriali usati dal plugin per riconoscere i modelli."""
    prefixes = set(re.findall(r'startswith\(\s*"([^"]+)"\s*\)', source))
    for tup in re.findall(r"startswith\(\s*\(([^)]*)\)\s*\)", source):
        prefixes.update(re.findall(r'"([^"]+)"', tup))
    for attr in dir(module):
        val = getattr(module, attr)
        if attr.endswith("PREFIX_TYPES") and isinstance(val, (dict, list, tuple)):
            items = val.keys() if isinstance(val, dict) else [v[0] if isinstance(v, (list, tuple)) else v for v in val]
            prefixes.update(str(p) for p in items)
    return sorted(prefixes)


def detect_types(module, plugin):
    """Ritorna {invertertype: [(prefisso, nome modello), ...]} eseguendo il riconoscimento del plugin."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    found = {}

    async def run(serial, model_code=None):
        inst = plugin.create_hub_instance()

        async def fake_serial(hub, *a, **k):
            return serial

        async def fake_model(hub, *a, **k):
            return model_code

        async def fake_firmware(hub, *a, **k):
            return 1.0

        saved = {}
        for fname, fake in (("async_read_serialnr", fake_serial), ("_read_serialnr", fake_serial),
                            ("_read_string", fake_serial), ("_read_model", fake_model),
                            ("async_read_firmware", fake_firmware)):
            if hasattr(module, fname):
                saved[fname] = getattr(module, fname)
                setattr(module, fname, fake)
        try:
            itype = await inst.async_determineInverterType(FakeHub(), {})
        finally:
            for fname, orig in saved.items():
                setattr(module, fname, orig)
        return itype, inst.inverter_model

    candidates = [(s + "0" * 16, s) for s in serial_candidates(module, source)]
    if hasattr(module, "_read_model"):  # Solinteg: modello da registro numerico
        candidates = [("SOLINTEG", (bh * 256 + bl, f"model {bh}{bl:02d}")) for bh in (30, 31, 32, 40, 41, 43) for bl in (0, 8)]
    if not candidates:
        candidates = [("UNKNOWN0000000000", "")]

    for serial, label in candidates:
        model_code = None
        if isinstance(label, tuple):
            model_code, label = label
        try:
            itype, model = asyncio.run(run(serial, model_code))
        except Exception:
            continue
        if not itype:
            continue
        found.setdefault(itype, []).append((label, model or ""))
    return found


def decode_flags(module, itype):
    """Nomi dei flag (quelli usati in allowedtypes=) presenti nel tipo, es. "HYBRID GEN4 X3"."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    vocab = set()
    for expr in re.findall(r"allowedtypes\s*=\s*([A-Z0-9_| ]+)", source):
        vocab.update(n.strip() for n in expr.split("|") if n.strip())
    names = []
    for name in sorted(vocab):
        val = getattr(module, name, None)
        if isinstance(val, int) and val and (val & (val - 1)) == 0 and itype & val:
            names.append(name)
    return " ".join(names)


def entity_variables(plugin, itype, serial):
    """Variabili e blocchi per un tipo di inverter, filtrando le entità del plugin."""
    const = sys.modules["custom_components.solax_modbus.const"]
    reg_types = {const.REG_HOLDING: "holding", const.REG_INPUT: "input"}
    data_types = {
        const.REGISTER_U16: (16, False), const.REGISTER_S16: (16, True),
        const.REGISTER_U32: (32, False), const.REGISTER_S32: (32, True),
        getattr(const, "REGISTER_ULSB16MSB16", "-"): (32, False),
    }
    variables, newblocks, seen, skipped = [], set(), set(), 0

    for descr in plugin.SENSOR_TYPES:
        try:
            if not plugin.matchInverterWithMask(itype, descr.allowedtypes, serial, descr.blacklist):
                continue
        except Exception:
            continue
        register_type = reg_types.get(descr.register_type)
        data_type = data_types.get(descr.register_data_type or const.REGISTER_U16)
        scale = descr.scale
        if (
            register_type is None or data_type is None or descr.register is None or descr.register < 0
            or descr.internal or descr.value_function is not None
            or isinstance(scale, bool) or not isinstance(scale, (int, float))
            or not isinstance(descr.name, str)
        ):
            skipped += 1
            continue
        bits, signed = data_type
        order32 = descr.order32 or plugin.order32 or "big"
        unit_sym = descr.native_unit_of_measurement
        unit = unit_sym.unit() if isinstance(unit_sym, Sym) else (unit_sym or "")
        factor = scale * (descr.read_scale or 1)
        series = range(descr.value_series) if descr.value_series else [None]
        for i in series:
            raw_name = descr.name if i is None else descr.name.replace("{}", str(i + 1))
            name = clean_name(raw_name)
            address = descr.register + (i or 0)
            if not name or name in seen:
                continue
            seen.add(name)
            variables.append(make_variable(
                name, register_type, address, bits, signed,
                "little" if (bits == 32 and order32 == "little") else "big", factor, 0, unit))
            if descr.newblock:
                newblocks.add((register_type, address))
    return variables, newblocks, skipped


SOLAX_PLUGINS = {
    # file: (produttore mostrato, includere?)
    "plugin_solax.py": "SolaX",
    "plugin_solax_a1j1.py": "SolaX",
    "plugin_solax_lv.py": "SolaX",
    "plugin_solax_mega_forth.py": "SolaX",
    "plugin_growatt.py": "Growatt",
    "plugin_sofar.py": "Sofar",
    "plugin_sofar_old.py": "Sofar",
    "plugin_solis.py": "Solis",
    "plugin_solis_fb00.py": "Solis",
    "plugin_solis_old.py": "Solis",
    "plugin_srne.py": "SRNE",
    "plugin_alphaess.py": "AlphaESS",
    "plugin_solinteg.py": "Solinteg",
    "plugin_sunway.py": "SunWay",
    "plugin_swatten.py": "Swatten",
    "plugin_viessmann.py": "Viessmann / GoodWe",
    "plugin_Enertech.py": "Enertech",
    # plugin_solax_ev_charger.py: wallbox, non inverter
}


def build_solax(repo: Path, out_dir: Path):
    plugin_dir = repo / "custom_components" / "solax_modbus"
    install_ha_stubs(plugin_dir)
    logging.disable(logging.CRITICAL)  # i plugin loggano ogni seriale non riconosciuto
    commit = git_commit(repo)
    done, failed = [], []

    for fname, manufacturer in SOLAX_PLUGINS.items():
        modname = f"custom_components.solax_modbus.{fname[:-3]}"
        try:
            module = importlib.import_module(modname)
        except Exception as exc:
            failed.append((fname, f"import: {exc!r}"))
            continue
        plugin = module.plugin_instance
        types_found = detect_types(module, plugin)
        if not types_found:
            failed.append((fname, "nessun tipo riconosciuto"))
            continue

        source = {
            "project": "homeassistant-solax-modbus",
            "url": "https://github.com/wills106/homeassistant-solax-modbus",
            "license": "Apache-2.0", "commit": commit,
            "file": f"custom_components/solax_modbus/{fname}",
        }
        # Tipi diversi con la stessa mappa di registri diventano un solo preset
        groups = {}
        for itype, members in sorted(types_found.items()):
            serial = members[0][0]
            inst = plugin.create_hub_instance()
            variables, newblocks, skipped = entity_variables(inst, itype, serial + "0" * 16)
            if len(variables) < MIN_VARIABLES:
                continue
            key = json.dumps(variables, sort_keys=True)
            group = groups.setdefault(key, {"variables": variables, "newblocks": set(), "skipped": skipped,
                                            "types": [], "members": []})
            group["newblocks"] |= newblocks
            group["types"].append(itype)
            group["members"] += members

        for group in groups.values():
            models = sorted({
                re.sub(r"-[\w.]*kw\b.*$", "", m, flags=re.IGNORECASE)
                for _, m in group["members"]
                if m and "0000" not in m and m.lower() != "unknown"
            })
            group["model"] = ", ".join(models) or decode_flags(module, group["types"][0]) or plugin.plugin_name
        label_count = {}
        for group in groups.values():
            label_count[group["model"]] = label_count.get(group["model"], 0) + 1

        for group in groups.values():
            members, types_ = group["members"], group["types"]
            prefixes = sorted({p for p, _ in members if p and p != "UNKNOWN0000000000"})
            model = group["model"]
            if label_count[model] > 1:
                flags = decode_flags(module, types_[0])
                model = f"{model} (0x{types_[0]:X})" if model == flags else f"{model} ({flags})"
            notes = []
            if prefixes:
                notes.append("Prefissi numero di serie: " + ", ".join(prefixes))
            notes += [f"Tipo plugin: 0x{t:X} ({decode_flags(module, t)})" for t in types_]
            variables = group["variables"]
            blocks = build_blocks(
                variables, min_span=plugin.block_size, max_size=plugin.block_size, newblock_addrs=group["newblocks"])
            preset_id = f"{slug(fname[7:-3])}_{slug(model)[:60]}_{types_[0]:x}"
            preset = write_preset(
                out_dir, preset_id, manufacturer, f"{model} [{fname[7:-3]}]", source,
                variables, [], blocks, notes)
            if preset:
                done.append((preset_id, len(variables), group["skipped"], preset["power_variable"]))
    return done, failed


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--solarman", type=Path, required=True)
    parser.add_argument("--solarman-legacy", type=Path)
    parser.add_argument("--solax", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("*.json"):
        old.unlink()

    report = build_solarman(args.solarman, args.out, "ha-solarman",
                            "https://github.com/davidrapan/ha-solarman", "MIT")
    if args.solarman_legacy:
        existing = {p.stem.lower() for p in (args.solarman / "custom_components/solarman/inverter_definitions").glob("*.yaml")}
        report += build_solarman(args.solarman_legacy, args.out, "home_assistant_solarman",
                                 "https://github.com/StephanJoubert/home_assistant_solarman", "Apache-2.0",
                                 skip_stems=existing)
    solax_report, failed = build_solax(args.solax, args.out)
    report += solax_report

    for preset_id, n_vars, skipped, power in report:
        print(f"{preset_id:70s} vars={n_vars:4d} skipped={skipped:4d} power={power}")
    for fname, reason in failed:
        print(f"FAILED {fname}: {reason}", file=sys.stderr)
    print(f"\n{len(report)} preset scritti in {args.out}")


if __name__ == "__main__":
    main()
