# Battery Controller - UPS Monitoring Library
# Author: IntelligentToasters
# License: GNU General Public License v3.0
#
# This module provides a lightweight UPS monitoring and control library for
# newline-delimited JSON protocol devices, with thread-safe TCP communication
# and attribute polling capabilities.
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

"""PowerTech UPS monitoring package

This package provides a lightweight, thread-safe monitor for interacting with
UPS-like devices that speak a newline-delimited JSON protocol. It exposes:

- `UPSMonitor`: a background TCP monitor that maintains a persistent
  connection to the device, parses device messages (CMD0/CMD2/CMD10), and
  offers helpers for polling attributes and controlling relays.
- `parse_line`, `process_cmd10`: parsing helpers for validating and
  converting device messages to Python-native values.

The module is intentionally small and dependency-free so it can be embedded
in CLIs or long-running services.
"""

from .monitor import UPSMonitor
from .parser import parse_line, process_cmd10, ATTR_NAMES

__all__ = ["UPSMonitor", "parse_line", "process_cmd10", "ATTR_NAMES"]
__version__ = "0.1.0"
