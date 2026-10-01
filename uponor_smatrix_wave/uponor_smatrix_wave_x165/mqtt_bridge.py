"""Optional receive-only MQTT bridge; no RF or decoder dependencies."""

from dataclasses import dataclass, field, replace
import json
import os
import threading

from .rooms import room_name
from .state import DeviceState, display_temperature


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    topic_prefix: str = "uponor"
    discovery_prefix: str = "homeassistant"
    stale_after: int = 900

    def __post_init__(self):
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("MQTT host and valid port (1-65535) are required")
        if self.stale_after <= 0:
            raise ValueError("MQTT stale timeout must be positive")
        for prefix in (self.topic_prefix, self.discovery_prefix):
            if not prefix or any(c in prefix for c in "+#\0") or prefix.endswith("/"):
                raise ValueError("MQTT prefixes must be nonempty, without wildcards or trailing slash")


def add_mqtt_arguments(parser):
    parser.add_argument("--mqtt", action="store_true", help="Enable MQTT publishing")
    parser.add_argument("--mqtt-host", default=os.getenv("UPONOR_MQTT_HOST"))
    parser.add_argument("--mqtt-port", type=int, default=os.getenv("UPONOR_MQTT_PORT", "1883"))
    parser.add_argument("--mqtt-username", default=os.getenv("UPONOR_MQTT_USERNAME"))
    parser.add_argument("--mqtt-topic-prefix", default=os.getenv("UPONOR_MQTT_TOPIC_PREFIX", "uponor"))
    parser.add_argument("--mqtt-discovery-prefix", default=os.getenv("UPONOR_MQTT_DISCOVERY_PREFIX", "homeassistant"))
    parser.add_argument("--mqtt-stale-after", type=int, default=os.getenv("UPONOR_MQTT_STALE_AFTER", "900"))


def config_from_args(args):
    return MqttConfig(args.mqtt_host, args.mqtt_port, args.mqtt_username,
                      os.getenv("UPONOR_MQTT_PASSWORD"), args.mqtt_topic_prefix,
                      args.mqtt_discovery_prefix, args.mqtt_stale_after)


def discovery_messages(config, device_id):
    identifier = f"uponor_{device_id}"
    name = room_name(bytes.fromhex(device_id))
    device = {"identifiers": [identifier], "name": name or f"Uponor {device_id}",
              "manufacturer": "Uponor", "model": "T-165 thermostat"}
    for sensor in ("temperature", "setpoint"):
        payload = {"name": sensor.title(), "unique_id": f"{identifier}_{sensor}",
                   "state_topic": f"{config.topic_prefix}/{device_id}/{sensor}",
                   "device_class": "temperature", "unit_of_measurement": "°C",
                   "suggested_display_precision": 1, "device": device}
        payload["availability_topic"] = f"{config.topic_prefix}/bridge/status"
        payload["expire_after"] = config.stale_after
        if sensor == "temperature":
            payload["state_class"] = "measurement"
        yield f"{config.discovery_prefix}/sensor/{identifier}/{sensor}/config", json.dumps(payload)
    for sensor, name, icon, device_class in (
        ("bypass", "Bypass", "mdi:pipe-valve", None),
        ("actuator_open", "Actuator", "mdi:valve-open", "opening"),
    ):
        payload = {
            "name": name,
            "unique_id": f"{identifier}_{sensor}",
            "state_topic": f"{config.topic_prefix}/{device_id}/{sensor}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": icon,
            "availability_topic": f"{config.topic_prefix}/bridge/status",
            "expire_after": config.stale_after,
            "device": device,
        }
        if device_class:
            payload["device_class"] = device_class
        yield f"{config.discovery_prefix}/binary_sensor/{identifier}/{sensor}/config", json.dumps(payload)


class MqttBridge:
    def __init__(self, config, *, client=None, log=print):
        self.config = config
        self.log = log
        if client is None:
            try:
                import paho.mqtt.client as mqtt
            except ImportError as exc:
                raise RuntimeError("MQTT requires: pip install -r requirements.txt") from exc
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client = client
        self._lock = threading.RLock()
        self._connected = False
        self._latest = {}
        self._latest_house = {}
        self._latest_mode = {}
        self._discovered = set()
        self._last_report = {}
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.max_queued_messages_set(256)
        client.will_set(f"{config.topic_prefix}/bridge/status", "offline", qos=1, retain=True)
        if config.username:
            client.username_pw_set(config.username, config.password)

    def start(self):
        self.client.connect_async(self.config.host, self.config.port, keepalive=60)
        self.client.loop_start()
        self.log(f"MQTT connecting to {self.config.host}:{self.config.port}")

    def close(self):
        if self._connected:
            info = self.client.publish(f"{self.config.topic_prefix}/bridge/status", "offline", qos=1, retain=True)
            try:
                info.wait_for_publish(timeout=2)
            except RuntimeError:
                pass
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            self._connected = reason_code == 0
            if not self._connected:
                self.log(f"MQTT connection rejected: {reason_code}; retrying")
                return
            self.log("MQTT connected")
            self._publish(f"{self.config.topic_prefix}/bridge/status", "online")
            self._discovered.clear()
            for state in self._latest.values():
                self._publish_state(state)
            for frame, observed_at in self._latest_house.values():
                self._publish_house(frame, observed_at)
            for controller_id, frame, observed_at in self._latest_mode.values():
                self._publish_mode(controller_id, frame, observed_at)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            self._connected = False
        if reason_code != 0:
            self.log(f"MQTT disconnected: {reason_code}; retrying while RF continues")

    def _on_connect_fail(self, client, userdata):
        self.log("MQTT connection failed; retrying while RF continues")

    def _publish(self, topic, payload):
        try:
            result = self.client.publish(topic, payload, qos=1, retain=True)
            if result.rc == 0:
                return True
            self.log(f"MQTT publish not queued (rc={result.rc}); latest state kept for retry")
        except (OSError, ValueError) as exc:
            self.log(f"MQTT publish failed ({type(exc).__name__}); latest state kept for retry")
        return False

    def _publish_state(self, state):
        device_id = state.device_id.hex().upper()
        if device_id not in self._discovered:
            results = [self._publish(topic, payload) for topic, payload in discovery_messages(self.config, device_id)]
            if not all(results):
                return False
            self._discovered.add(device_id)
            self.log(f"Home Assistant MQTT discovery queued: {device_id}")
        results = []
        for sensor, value in (("temperature", state.last_raw_temperature), ("setpoint", state.last_raw_setpoint)):
            if value is not None:
                results.append(self._publish(f"{self.config.topic_prefix}/{device_id}/{sensor}", display_temperature(value)))
        for sensor, value in (("bypass", state.bypass_enabled), ("actuator_open", state.actuator_open)):
            if value is not None:
                results.append(self._publish(f"{self.config.topic_prefix}/{device_id}/{sensor}", "ON" if value else "OFF"))
        results.append(self._publish(f"{self.config.topic_prefix}/{device_id}/last_seen", state.last_seen.isoformat()))
        return all(results)

    def observe(self, state: DeviceState):
        """Use RF observation time, not decoder completion time, for dedupe."""
        if state.last_seen is None or (state.temperature_c is None and state.setpoint_c is None):
            return "MQTT no values"
        with self._lock:
            key = state.device_id
            signature = (state.last_raw_temperature, state.last_raw_setpoint,
                         state.bypass_enabled, state.actuator_open)
            previous = self._last_report.get(key)
            if previous and previous[0] == signature and 0 <= (state.last_seen - previous[1]).total_seconds() < 2:
                return "MQTT duplicate suppressed"
            self._last_report[key] = (signature, state.last_seen)
            self._latest[key] = replace(state)
            if not self._connected:
                return "MQTT offline; latest state cached"
            return "MQTT queued" if self._publish_state(state) else "MQTT publish failed"

    def _publish_house(self, frame, observed_at):
        device_id = frame.interface_id.hex().upper()
        identifier = f"uponor_i167_{device_id}"
        base = f"{self.config.topic_prefix}/interface/{device_id}"
        if identifier not in self._discovered:
            payload = {
                "name": "House temperature", "unique_id": f"{identifier}_house_temperature",
                "state_topic": f"{base}/house_temperature", "device_class": "temperature",
                "state_class": "measurement", "unit_of_measurement": "°C",
                "suggested_display_precision": 1, "expire_after": self.config.stale_after,
                "availability_topic": f"{self.config.topic_prefix}/bridge/status",
                "device": {"identifiers": [identifier], "name": f"Uponor I-167 {device_id}",
                           "manufacturer": "Uponor", "model": "I-167 interface"}}
            if not self._publish(f"{self.config.discovery_prefix}/sensor/{identifier}/house_temperature/config", json.dumps(payload)):
                return False
            self._discovered.add(identifier)
            self.log(f"Home Assistant house-temperature discovery queued: {device_id}")
        results = [self._publish(f"{base}/house_temperature", display_temperature(frame.raw_temperature)),
                   self._publish(f"{base}/last_seen", observed_at.isoformat())]
        return all(results)

    def observe_house(self, frame, observed_at):
        with self._lock:
            previous = self._latest_house.get(frame.interface_id)
            if previous and previous[0].raw_temperature == frame.raw_temperature and 0 <= (observed_at - previous[1]).total_seconds() < 2:
                return "MQTT duplicate suppressed"
            self._latest_house[frame.interface_id] = (frame, observed_at)
            if not self._connected:
                return "MQTT offline; latest house temperature cached"
            return "MQTT queued" if self._publish_house(frame, observed_at) else "MQTT publish failed"

    def _publish_mode(self, controller_id, frame, observed_at):
        device_id = controller_id.hex().upper()
        identifier = f"uponor_x165_{device_id}"
        base = f"{self.config.topic_prefix}/controller/{device_id}"
        key = identifier + "_system_mode"
        if key not in self._discovered:
            payload = {
                "name": "System mode", "unique_id": key,
                "state_topic": f"{base}/system_mode", "device_class": "enum",
                "options": ["home", "away"], "icon": "mdi:home-export-outline",
                "json_attributes_topic": f"{base}/system_mode_attributes",
                "availability_topic": f"{self.config.topic_prefix}/bridge/status",
                "expire_after": self.config.stale_after,
                "device": {"identifiers": [identifier], "name": f"Uponor X-165 {device_id}",
                           "manufacturer": "Uponor", "model": "X-165 controller"}}
            if not self._publish(f"{self.config.discovery_prefix}/sensor/{identifier}/system_mode/config", json.dumps(payload)):
                return False
            self._discovered.add(key)
        attributes = {"source_interface_id": frame.interface_id.hex().upper(),
                      "raw_status": f"{frame.status_byte:02X}", "last_seen": observed_at.isoformat(),
                      "interpretation": "observed home/away; scheduled ECO unverified"}
        results = [self._publish(f"{base}/system_mode_attributes", json.dumps(attributes)),
                   self._publish(f"{base}/system_mode", frame.mode)]
        return all(results)

    def observe_mode(self, controller_id, frame, observed_at):
        if len(controller_id) != 4:
            raise ValueError("controller_id must contain exactly 4 bytes")
        with self._lock:
            previous = self._latest_mode.get(controller_id)
            if (previous and previous[1].interface_id == frame.interface_id
                    and previous[1].status_byte == frame.status_byte
                    and 0 <= (observed_at - previous[2]).total_seconds() < 2):
                return "MQTT duplicate suppressed"
            self._latest_mode[controller_id] = (controller_id, frame, observed_at)
            if not self._connected:
                return "MQTT offline; latest system mode cached"
            return "MQTT queued" if self._publish_mode(controller_id, frame, observed_at) else "MQTT publish failed"
