# Battery Controller - UPS Message Parser
# Author: IntelligentToasters
# License: GNU General Public License v3.0
#
# Provides deterministic helpers for translating newline-delimited JSON
# messages from UPS devices into Python native values, including attribute
# conversion and command response parsing.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Parsing helpers for UPS TCP messages.

This module provides a small set of deterministic, well-documented helpers
for translating newline-delimited JSON messages produced by the UPS device
into Python native values that are convenient for consumers.

Key functions
- parse_line(line: str) -> dict
    Validate and convert a single newline-delimited JSON line into a dict.
    Raises ValueError on invalid JSON or missing required fields.

- process_cmd10(msg: dict) -> dict[int, Any]
    Extract the "data" mapping from CMD10 (attribute) messages and convert
    known attributes to more friendly representations. Returns a mapping of
    attribute id -> converted Python value.

Helpers
- _convert_attr_value(attr_id, raw_value) -> Any
    Internal helper that converts individual attribute values where needed
    (for example, attribute 32: Fahrenheit*10 -> Celsius rounded to 0.1).

Notes and conventions
- Attribute 1 encodes relay bit flags and is returned as a dict with boolean
  fields plus a `raw` integer value.
- Unknown attributes are returned as ints when the numerical form is integral,
  otherwise floats or the original raw value are preserved.
"""
from __future__ import annotations

import json
from typing import Dict, Any

# Known attribute names for readability
# Attr 1 encodes relay states (bit flags) — see _convert_attr_value for details
ATTR_NAMES: Dict[int, str] = {
    1: "output_relays",
    3: "state_of_charge",
    5: "ac_load_watts",
    6: "dc_12v30a_watts",
    7: "dc_usbc_watts",
    8: "dc_usba_watts",
    21: "input_combined_watts",
    22: "grid_power_watts",
    30: "remaining_runtime_minutes",
    32: "battery_temp_c",
    # Derived attribute (computed by monitor): solar-only input = attr21 - attr22
    100: "solar_input_watts",
}  


def parse_line(line: str) -> Dict[str, Any]:
    """Parse a single newline-delimited JSON message from the device.

    Raises ValueError on invalid JSON or missing fields.
    """
    line = line.strip()
    if not line:
        raise ValueError("empty line")
    try:
        data = json.loads(line)
    except Exception as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc

    # Basic sanity
    if "cmd" not in data:
        raise ValueError("missing 'cmd' field")
    return data


def _convert_attr_value(attr_id: int, raw_value: Any) -> Any:
    """Convert raw attribute values to human-friendly values where necessary.

    For example:
    - attr 32: raw is temperature in Fahrenheit * 10. Convert to Celsius.

    This function centralizes attribute-specific conversions so tests and
    the rest of the codebase only need to call `process_cmd10` and get ready-
    to-use Python types.
    """
    if raw_value is None:
        return None
    try:
        v = float(raw_value)
    except Exception:
        # If we can't convert to float, return raw. This keeps behavior stable
        # for unexpected device payloads (robustness over strictness).
        return raw_value

    # Attribute 32 is battery temperature encoded as Fahrenheit * 10.
    if attr_id == 32:
        # raw: e.g., 861 -> 86.1 F -> convert to C
        fahrenheit = v / 10.0
        celsius = (fahrenheit - 32.0) * 5.0 / 9.0
        # Round to one decimal place for UI friendliness.
        return round(celsius, 1)

    # Attribute 1 encodes relay bit flags. Convert to a structured dict so
    # callers can test named booleans and still access the raw integer via
    # the `raw` field.
    if attr_id == 1:
        iv = int(v)
        return {
            "ac_inverter": bool(iv & 0x1),
            "dc_12v10a": bool(iv & 0x2),
            "usb_highpower": bool(iv & 0x4),
            "raw": iv,
        }

    # Default: if the float has no fractional component, return an int; this
    # simplifies equality and digestible displays elsewhere in the code.
    if v.is_integer():
        return int(v)
    return v


def process_cmd10(msg: Dict[str, Any]) -> Dict[int, Any]:
    """Extract attributes from a CMD10 message, converting known attributes.

    Returns a mapping attr_id -> converted_value.
    """
    out: Dict[int, Any] = {}
    if not msg:
        return out
    data = msg.get("data", {})
    for k_str, v in data.items():
        try:
            attr_id = int(k_str)
        except Exception:
            continue
        out[attr_id] = _convert_attr_value(attr_id, v)
    return out
