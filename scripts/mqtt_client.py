#!/usr/bin/env python3
# Battery Controller - MQTT Client
# Author: IntelligentToasters
# License: GNU General Public License v3.0
#
# Connects to a Powertech S2 UPS device, polls status data, and publishes to
# an MQTT broker for HomeAssistant integration. Handles relay controls from
# HomeAssistant and forwards them to the UPS device.
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
"""
MQTT Client for Powertech S2 UPS
Publishes UPS status data for HomeAssistant integration

This client connects to a Powertech S2 UPS via the UPSMonitor library,
polls the device for status, and publishes to an MQTT broker for HomeAssistant integration.
Relay controls from HomeAssistant are passed through to the device.

Usage:
    scripts/mqtt_client.py --ups-host 192.168.1.1 --ups-port 5555 --interval 1
"""

import json
import time
import signal
import sys
import argparse
from datetime import datetime
import paho.mqtt.client as mqtt
import logging

# Add parent directory to path so we can import powertech_ups
sys.path.insert(0, '.')

from powertech_ups import UPSMonitor

# Import secrets
from secrets import MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD

log = logging.getLogger(__name__)

# Topic configuration
AVAILABILITY_TOPIC = "ups/powertech_s2/availability"
STATE_TOPIC = "ups/powertech_s2/state"
CONFIG_TOPIC = "ups/powertech_s2/config"

# Switch command topics (where HomeAssistant sends commands TO the device)
USB_POWER_COMMAND_TOPIC = "ups/powertech_s2/usb_power/set"
DC12V10A_COMMAND_TOPIC = "ups/powertech_s2/dc12v10a/set"
AC_POWER_COMMAND_TOPIC = "ups/powertech_s2/ac_power/set"

# Switch state topics (where device publishes current state TO HomeAssistant)
USB_POWER_STATE_TOPIC = "ups/powertech_s2/usb_power/state"
DC12V10A_STATE_TOPIC = "ups/powertech_s2/dc12v10a/state"
AC_POWER_STATE_TOPIC = "ups/powertech_s2/ac_power/state"

# Device configuration for HomeAssistant
DEVICE_CONFIG = {
    "identifiers": ["powertech_s2"],
    "name": "Powertech S2 UPS",
    "manufacturer": "Powertech",
    "model": "S2",
    "sw_version": "1.0"
}

# Sensor configurations for HomeAssistant discovery
SENSOR_CONFIGS = {
    "state_of_charge": {
        "name": "UPS State of Charge",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.state_of_charge }}",
        "unit_of_measurement": "%",
        "device_class": "battery",
        "state_class": "measurement",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_state_of_charge",
        "device": DEVICE_CONFIG
    },
    "battery_temperature": {
        "name": "UPS Battery Temperature",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.battery_temperature }}",
        "unit_of_measurement": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_battery_temperature",
        "device": DEVICE_CONFIG
    },
    "combined_input_power": {
        "name": "UPS Combined Input Power",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.combined_input_power }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_combined_input_power",
        "device": DEVICE_CONFIG
    },
    "grid_input_power": {
        "name": "UPS Grid Input Power",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.grid_input_power }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_grid_input_power",
        "device": DEVICE_CONFIG
    },
    "remaining_runtime": {
        "name": "UPS Remaining Runtime",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.remaining_runtime }}",
        "unit_of_measurement": "min",
        "device_class": "duration",
        "state_class": "measurement",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_remaining_runtime",
        "device": DEVICE_CONFIG
    },
    "output_usba_power": {
        "name": "UPS Output USB-A Power",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.output_usba_power }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_output_usba_power",
        "device": DEVICE_CONFIG
    },
    "output_usbc_power": {
        "name": "UPS Output USB-C Power",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.output_usbc_power }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_output_usbc_power",
        "device": DEVICE_CONFIG
    },
    "DC12V10A_power": {
        "name": "UPS DC 12V 10A Power",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.DC12V10A_power }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_dc12v10a_power",
        "device": DEVICE_CONFIG
    },
    "ac_load_power": {
        "name": "UPS AC Load Power",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.ac_load_power }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_ac_load_power",
        "device": DEVICE_CONFIG
    },
    "relay_ac_inverter": {
        "name": "UPS Relay AC Inverter",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.relay_ac_inverter }}",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_relay_ac_inverter",
        "device": DEVICE_CONFIG
    },
    "relay_dc_12v10a": {
        "name": "UPS Relay DC 12V 10A",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.relay_dc_12v10a }}",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_relay_dc_12v10a",
        "device": DEVICE_CONFIG
    },
    "relay_usb_highpower": {
        "name": "UPS Relay USB High Power",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.relay_usb_highpower }}",
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_relay_usb_highpower",
        "device": DEVICE_CONFIG
    }
}

# Binary Switch configurations for HomeAssistant discovery
SWITCH_CONFIGS = {
    "usb_power": {
        "name": "USB Power",
        "command_topic": USB_POWER_COMMAND_TOPIC,
        "state_topic": USB_POWER_STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_usb_power",
        "device_class": "switch",
        "device": DEVICE_CONFIG
    },
    "dc12v10a": {
        "name": "DC 12V 10A",
        "command_topic": DC12V10A_COMMAND_TOPIC,
        "state_topic": DC12V10A_STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_dc12v10a",
        "device_class": "switch",
        "device": DEVICE_CONFIG
    },
    "ac_power": {
        "name": "AC Power",
        "command_topic": AC_POWER_COMMAND_TOPIC,
        "state_topic": AC_POWER_STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "unique_id": "powertech_s2_ac_power",
        "device_class": "switch",
        "device": DEVICE_CONFIG
    }
}


class PowertechMQTTClient:
    def __init__(self, ups_host="192.168.1.1", ups_port=5555, broker=MQTT_BROKER, port=MQTT_PORT, username=None, password=None):
        self.ups_host = ups_host
        self.ups_port = ups_port
        self.broker = broker
        self.mqtt_port = port
        self.username = username
        self.password = password
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="powertech_s2_client")
        self.running = True
        
        # UPS Monitor connection - calls the monitor.py library
        self.monitor = UPSMonitor(host=ups_host, port=ups_port)
        
        # Relay bit mapping for set_relays() calls
        # Bit 0 (0x1) = AC inverter
        # Bit 1 (0x2) = DC 12V 10A
        # Bit 2 (0x4) = USB high power
        self.relay_bits = {
            'ac_inverter': 0x1,
            'dc_12v10a': 0x2,
            'usb_highpower': 0x4
        }
        
        # Flag to prevent command processing until initial state is published
        self.initialization_complete = False
        
        # Track relay command timestamps to avoid publishing stale state during UPS processing
        # Maps relay_name -> timestamp when command was last received
        self.relay_command_timestamps = {
            'ac_inverter': None,
            'dc_12v10a': None,
            'usb_highpower': None
        }
        
        # Grace period: don't publish relay state for this many seconds after a command
        # Allows UPS time to process the change before we poll it
        self.relay_grace_period = 1.5
        
        # Track commanded relay states during grace period
        self.relay_commanded_states = {
            'ac_inverter': False,
            'dc_12v10a': False,
            'usb_highpower': False
        }
        
        # Track UPS connection state for detecting disconnect/reconnect
        self.ups_was_connected = True
        
        # Set up callbacks
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_publish = self.on_publish
        self.client.on_message = self.on_message
        
    def on_connect(self, client, userdata, connect_flags, reason_code, properties):
        """Callback for when the client connects to the broker"""
        if reason_code == 0:
            log.info(f"Connected to MQTT broker at {self.broker}:{self.mqtt_port}")
            # Publish availability status
            self.client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
            log.debug(f"Published: {AVAILABILITY_TOPIC} = online")
            # Note: Subscriptions will be added in run() method after initialization is complete
            # This prevents processing stale retained messages while initialization_complete = False
        else:
            log.error(f"Failed to connect to MQTT broker, return code {reason_code}")
    
    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """Callback for when the client disconnects from the broker"""
        log.info(f"Disconnected from MQTT broker (code: {reason_code})")
    
    def on_publish(self, client, userdata, mid, reason_code, properties):
        """Callback for when a message is published"""
        pass  # Silently ignore publish confirmations for clean output
    
    def on_message(self, client, userdata, msg):
        """Callback for when a message is received from a subscribed topic"""
        # Ignore all messages until initialization is complete
        if not self.initialization_complete:
            return
        
        payload = msg.payload.decode('utf-8').lower().strip()
        
        # Validate payload - only accept "on" or "off"
        if payload not in ("on", "off"):
            log.warning(f"Invalid payload on {msg.topic}: '{payload}' (ignored)")
            return
        
        is_on = payload == "on"
        
        # Map topics to relay names and bits
        relay_name = None
        relay_bit = None
        
        if msg.topic == USB_POWER_COMMAND_TOPIC:
            relay_name = "USB Power"
            relay_bit = self.relay_bits['usb_highpower']
        elif msg.topic == DC12V10A_COMMAND_TOPIC:
            relay_name = "DC 12V 10A"
            relay_bit = self.relay_bits['dc_12v10a']
        elif msg.topic == AC_POWER_COMMAND_TOPIC:
            relay_name = "AC Power"
            relay_bit = self.relay_bits['ac_inverter']
        
        if relay_name and relay_bit is not None:
            # Call monitor.py's set_relays() with proper mask and values
            # mask: only affect this specific relay bit
            # values: set to relay_bit if on, 0 if off
            values = relay_bit if is_on else 0
            ok = self.monitor.set_relays(relay_bit, values, timeout=2.0)
            state = "ON" if is_on else "OFF"
            status = "OK" if ok else "FAILED"
            log.info(f"Switch Command: {relay_name} = {state} ({status})")
            
            # Track command for grace period - don't publish stale state while UPS processes change
            if ok:
                relay_key = None
                if msg.topic == USB_POWER_COMMAND_TOPIC:
                    relay_key = 'usb_highpower'
                elif msg.topic == DC12V10A_COMMAND_TOPIC:
                    relay_key = 'dc_12v10a'
                elif msg.topic == AC_POWER_COMMAND_TOPIC:
                    relay_key = 'ac_inverter'
                
                if relay_key:
                    self.relay_command_timestamps[relay_key] = time.time()
                    self.relay_commanded_states[relay_key] = is_on
    
    def publish_config(self):
        """Publish sensor configurations for HomeAssistant autodiscovery"""
        log.debug("Publishing sensor configurations...")
        for sensor_id, config in SENSOR_CONFIGS.items():
            topic = f"homeassistant/sensor/powertech_s2/{sensor_id}/config"
            payload = json.dumps(config)
            self.client.publish(topic, payload, qos=1, retain=True)
            log.debug(f"Published config for {sensor_id}")
        
        log.debug("Publishing switch configurations...")
        for switch_id, config in SWITCH_CONFIGS.items():
            topic = f"homeassistant/switch/powertech_s2/{switch_id}/config"
            payload = json.dumps(config)
            self.client.publish(topic, payload, qos=1, retain=True)
            log.debug(f"Published config for {switch_id}")
    
    def publish_state(self, state_data):
        """Publish current UPS state"""
        payload = json.dumps(state_data)
        self.client.publish(STATE_TOPIC, payload, qos=1, retain=True)
        
        # Publish individual relay states to their STATE topics (not command topics)
        # During grace period after a command, use the commanded state instead of polled state
        # This prevents brief flickers when HA commands a relay but UPS hasn't updated yet
        current_time = time.time()
        
        # Check each relay: are we in grace period after a command?
        for relay_key in ['ac_inverter', 'dc_12v10a', 'usb_highpower']:
            last_cmd_time = self.relay_command_timestamps[relay_key]
            in_grace_period = (last_cmd_time is not None and 
                               current_time - last_cmd_time < self.relay_grace_period)
            
            if in_grace_period:
                # Use commanded state during grace period
                is_on = self.relay_commanded_states[relay_key]
            else:
                # Use polled state from UPS
                is_on = state_data.get(f"relay_{relay_key}", False)
            
            state_str = "ON" if is_on else "OFF"
            
            if relay_key == 'ac_inverter':
                self.client.publish(AC_POWER_STATE_TOPIC, state_str, qos=1, retain=True)
            elif relay_key == 'dc_12v10a':
                self.client.publish(DC12V10A_STATE_TOPIC, state_str, qos=1, retain=True)
            elif relay_key == 'usb_highpower':
                self.client.publish(USB_POWER_STATE_TOPIC, state_str, qos=1, retain=True)
        
        log.debug(f"Published state: {json.dumps(state_data, indent=2)}")
    
    def get_ups_status(self):
        """Fetch current UPS status from the monitor library and format for MQTT"""
        # Call monitor.py's get_all() to fetch all attributes
        all_attrs = self.monitor.get_all()
        
        # Map attribute IDs to MQTT field names
        # Attr 3 = state_of_charge
        # Attr 32 = battery_temperature
        # Attr 21 = combined_input_power
        # Attr 22 = grid_input_power
        # Attr 30 = remaining_runtime
        # Attr 8 = output_usba_power
        # Attr 7 = output_usbc_power
        # Attr 6 = dc_12v30a_power (DC 12V 10A)
        # Attr 5 = ac_load_power
        # Attr 1 = output_relays (dict with ac_inverter, dc_12v10a, usb_highpower)
        
        state_data = {}
        
        # Basic attributes
        state_data["state_of_charge"] = all_attrs.get(3)
        state_data["battery_temperature"] = all_attrs.get(32)
        state_data["combined_input_power"] = all_attrs.get(21)
        state_data["grid_input_power"] = all_attrs.get(22)
        state_data["remaining_runtime"] = all_attrs.get(30)
        state_data["output_usba_power"] = all_attrs.get(8)
        state_data["output_usbc_power"] = all_attrs.get(7)
        state_data["DC12V10A_power"] = all_attrs.get(6)
        state_data["ac_load_power"] = all_attrs.get(5)
        
        # Relay states - extract from attribute 1 (dict with relay booleans)
        relay_attr = all_attrs.get(1)
        if isinstance(relay_attr, dict):
            state_data["relay_ac_inverter"] = relay_attr.get("ac_inverter", False)
            state_data["relay_dc_12v10a"] = relay_attr.get("dc_12v10a", False)
            state_data["relay_usb_highpower"] = relay_attr.get("usb_highpower", False)
        else:
            # Fallback if relay state not available
            state_data["relay_ac_inverter"] = False
            state_data["relay_dc_12v10a"] = False
            state_data["relay_usb_highpower"] = False
        
        return state_data
    
    def connect(self):
        """Connect to UPS device and MQTT broker"""
        # Start UPS monitor connection (calls monitor.py)
        try:
            log.debug(f"Connecting to UPS at {self.ups_host}:{self.ups_port}...")
            self.monitor.start()
            time.sleep(1)  # Give monitor time to establish connection
            log.debug("UPS monitor started")
        except Exception as e:
            log.error(f"Failed to start UPS monitor: {e}")
            return False
        
        # Connect to MQTT broker
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)
        
        try:
            log.debug(f"Connecting to MQTT broker at {self.broker}:{self.mqtt_port}...")
            self.client.connect(self.broker, self.mqtt_port, keepalive=60)
            self.client.loop_start()
            time.sleep(1)  # Give connection time to establish
            return True
        except Exception as e:
            log.error(f"Failed to connect to MQTT broker: {e}")
            return False
    
    def publish_initial_relay_states(self):
        """Publish initial relay states from UPS to switch state topics"""
        # Wait for monitor to have received initial relay state from UPS
        max_retries = 10
        for attempt in range(max_retries):
            relay_attr = self.monitor.get_attr(1)
            if relay_attr is not None:
                break
            time.sleep(0.2)
        
        if relay_attr is None:
            log.warning(f"Could not get relay state from UPS after {max_retries} attempts")
            return
        
        if isinstance(relay_attr, dict):
            # Publish each relay state to its corresponding switch STATE topic (not command topic)
            ac_inverter_state = "ON" if relay_attr.get("ac_inverter", False) else "OFF"
            dc_12v10a_state = "ON" if relay_attr.get("dc_12v10a", False) else "OFF"
            usb_highpower_state = "ON" if relay_attr.get("usb_highpower", False) else "OFF"
            
            self.client.publish(AC_POWER_STATE_TOPIC, ac_inverter_state, qos=1, retain=True)
            log.debug(f"Published initial relay state: AC Inverter (attr1.ac_inverter) = {ac_inverter_state}")
            
            self.client.publish(DC12V10A_STATE_TOPIC, dc_12v10a_state, qos=1, retain=True)
            log.debug(f"Published initial relay state: DC 12V 10A (attr1.dc_12v10a) = {dc_12v10a_state}")
            
            self.client.publish(USB_POWER_STATE_TOPIC, usb_highpower_state, qos=1, retain=True)
            log.debug(f"Published initial relay state: USB High Power (attr1.usb_highpower) = {usb_highpower_state}")
        else:
            log.warning(f"Relay state from UPS is not a dict: {relay_attr}")
    
    def disconnect(self):
        """Disconnect from MQTT broker and UPS device"""
        log.info("Shutting down...")
        self.client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        log.debug(f"Published: {AVAILABILITY_TOPIC} = offline")
        time.sleep(0.5)  # Give publish time to complete
        self.client.loop_stop()
        self.client.disconnect()
        self.monitor.stop()
        self.running = False
    
    def run(self, interval=1):
        """Main loop - continuously poll UPS and publish status data to MQTT"""
        if not self.connect():
            return
        
        # Publish sensor configurations on startup
        time.sleep(1)
        self.publish_config()
        time.sleep(1)
        
        # Publish initial relay states from the UPS to the switch topics
        self.publish_initial_relay_states()
        time.sleep(1)
        
        # Now subscribe to command topics and mark initialization complete
        # This ensures we have published the actual relay state before HomeAssistant can send commands
        self.client.subscribe(USB_POWER_COMMAND_TOPIC, qos=1)
        self.client.subscribe(DC12V10A_COMMAND_TOPIC, qos=1)
        self.client.subscribe(AC_POWER_COMMAND_TOPIC, qos=1)
        self.initialization_complete = True
        log.info("Initialization complete - subscribed to command topics and relay control enabled")
        time.sleep(1)
        
        log.info(f"Publishing UPS status every {interval} second(s)...")
        log.info("Press Ctrl+C to stop")
        
        try:
            while self.running:
                # Check if UPS is still connected
                conn_status = self.monitor.get_connection_status()
                ups_is_connected = conn_status.get('connected', False)
                
                # Detect connection state change
                if ups_is_connected != self.ups_was_connected:
                    if ups_is_connected:
                        # UPS just reconnected
                        log.info("UPS reconnected")
                        result = self.client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
                        if result.rc == mqtt.MQTT_ERR_SUCCESS:
                            log.debug("Published AVAILABILITY = online")
                        else:
                            log.error(f"ERROR publishing availability: {mqtt.error_string(result.rc)}")
                    else:
                        # UPS just disconnected
                        error_msg = conn_status.get('last_error', 'Unknown error')
                        log.info(f"UPS disconnected: {error_msg}")
                        result = self.client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
                        if result.rc == mqtt.MQTT_ERR_SUCCESS:
                            log.debug("Published AVAILABILITY = unavailable")
                        else:
                            log.error(f"ERROR publishing availability: {mqtt.error_string(result.rc)}")
                    self.ups_was_connected = ups_is_connected
                
                # Only poll and publish state if UPS is connected
                if ups_is_connected:
                    state_data = self.get_ups_status()
                    self.publish_state(state_data)
                
                # Wait before next update
                time.sleep(interval)
        
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            self.disconnect()
    
    def signal_handler(self, sig, frame):
        """Handle termination signals gracefully (SIGINT from Ctrl+C, SIGTERM from kill)"""
        self.disconnect()
        sys.exit(0)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="MQTT Client for Powertech S2 UPS")
    parser.add_argument("--ups-host", default="192.168.1.1", help="UPS device hostname or IP (default: 192.168.1.1)")
    parser.add_argument("--ups-port", default=5555, type=int, help="UPS device port (default: 5555)")
    parser.add_argument("--interval", default=1, type=int, help="Publish interval in seconds (default: 1)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose (DEBUG) logging")
    args = parser.parse_args()
    
    # Configure logging with timestamp
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    log_format = '[%(asctime)s] %(levelname)s: %(message)s'
    logging.basicConfig(level=log_level, format=log_format, datefmt='%H:%M:%S')
    
    # Create MQTT client
    mqtt_client = PowertechMQTTClient(
        ups_host=args.ups_host,
        ups_port=args.ups_port,
        broker=MQTT_BROKER,
        port=MQTT_PORT,
        username=MQTT_USERNAME,
        password=MQTT_PASSWORD
    )
    
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, mqtt_client.signal_handler)
    signal.signal(signal.SIGTERM, mqtt_client.signal_handler)
    
    # Run the client
    mqtt_client.run(interval=args.interval)


if __name__ == "__main__":
    main()
