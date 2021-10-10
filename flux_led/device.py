from __future__ import print_function

import colorsys
import datetime
import logging
import select
import socket
import threading
import time
from enum import Enum

from .const import (  # imported for back compat, remove once Home Assistant no longer uses
    COLOR_MODE_ADDRESSABLE,
    COLOR_MODE_CCT,
    COLOR_MODE_DIM,
    COLOR_MODE_RGB,
    COLOR_MODE_RGBW,
    COLOR_MODE_RGBWW,
    COLOR_MODES_RGB,
    COLOR_MODES_RGB_CCT,
    COLOR_MODES_RGB_W,
    DEFAULT_MODE,
    MAX_TEMP,
    MIN_TEMP,
    MODE_COLOR,
    MODE_CUSTOM,
    MODE_MUSIC,
    MODE_PRESET,
    MODE_SWITCH,
    MODE_WW,
    MODEL_NUM_SWITCH,
    STATE_CHANGE_LATENCY,
    STATIC_MODES,
    WRITE_ALL_COLORS,
    WRITE_ALL_WHITES,
    LevelWriteMode,
)
from .models_db import (
    BASE_MODE_MAP,
    CHANNEL_REMAP,
    MODEL_MAP,
    RGBW_PROTOCOL_MODELS,
    USE_9BYTE_PROTOCOL_MODELS,
)
from .pattern import PresetPattern
from .protocol import (
    PROTOCOL_LEDENET_8BYTE,
    PROTOCOL_LEDENET_9BYTE,
    PROTOCOL_LEDENET_ORIGINAL,
    ProtocolLEDENET8Byte,
    ProtocolLEDENET9Byte,
    ProtocolLEDENETOriginal,
)
from .sock import _socket_retry
from .timer import BuiltInTimer, LedTimer
from .utils import color_temp_to_white_levels, utils, white_levels_to_color_temp

_LOGGER = logging.getLogger(__name__)


class DeviceType(Enum):
    Bulb = 0
    Switch = 1


class LEDENETDevice:
    """An LEDENET Device."""

    def __init__(self, ipaddr, port=5577, timeout=5):
        """Init the LEDENEt Device."""
        self.ipaddr = ipaddr
        self.port = port
        self.timeout = timeout
        self.raw_state = None
        self.available = None

        self._protocol = None
        self._is_on = False
        self._mode = None
        self._socket = None
        self._transition_complete_time = 0
        self._lock = threading.Lock()

    @property
    def model_num(self):
        """Return the model number."""
        return self.raw_state.model_num if self.raw_state else None

    @property
    def rgbwprotocol(self):
        """Devices that don't require a separate rgb/w bit."""
        return self.model_num in RGBW_PROTOCOL_MODELS

    @property
    def rgbwcapable(self):
        """Devices that actually support rgbw."""
        color_modes = self.color_modes
        return COLOR_MODE_RGBW in color_modes or COLOR_MODE_RGBWW in color_modes

    @property
    def device_type(self):
        """Return the device type."""
        return (
            DeviceType.Switch if self.model_num == MODEL_NUM_SWITCH else DeviceType.Bulb
        )

    @property
    def _rgbwwprotocol(self):
        """Device that uses the 9-byte protocol."""
        return self._uses_9byte_protocol(self.model_num)

    def _uses_9byte_protocol(self, model_num):
        """Devices that use a 9-byte protocol."""
        return model_num in USE_9BYTE_PROTOCOL_MODELS

    @property
    def white_active(self):
        """Any white channel is active."""
        return bool(self.raw_state.warm_white or self.raw_state.cool_white)

    @property
    def color_active(self):
        """Any color channel is active."""
        raw_state = self.raw_state
        return bool(raw_state.red or raw_state.green or raw_state.blue)

    @property
    def multi_color_mode(self):
        """The device supports multiple color modes."""
        return len(self.color_modes) > 1

    @property
    def color_modes(self):
        """The available color modes."""
        model_db_entry = MODEL_MAP.get(self.model_num)
        if not model_db_entry:
            # Default mode is RGB
            return BASE_MODE_MAP.get(self.raw_state.mode & 0x0F, {DEFAULT_MODE})
        return model_db_entry.mode_to_color_mode.get(
            self.raw_state.mode, model_db_entry.color_modes
        )

    @property
    def color_mode(self):
        """The current color mode."""
        color_modes = self.color_modes
        if COLOR_MODE_RGBWW in color_modes and not self.color_active:
            return COLOR_MODE_CCT
        if (
            color_modes == COLOR_MODES_RGB_CCT
        ):  # RGB/CCT split, only one active at a time
            return COLOR_MODE_CCT if self.white_active else COLOR_MODE_RGB
        if color_modes == COLOR_MODES_RGB_W:  # RGB/W split, only one active at a time
            return COLOR_MODE_DIM if self.white_active else COLOR_MODE_RGB
        if color_modes:
            return list(color_modes)[0]
        return None  # Usually a switch or non-light device

    @property
    def protocol(self):
        """Returns the name of the protocol in use."""
        if not self._protocol:
            return None
        return self._protocol.name

    @property
    def is_on(self):
        return self._is_on

    @property
    def mode(self):
        return self._mode

    @property
    def warm_white(self):
        return self.raw_state.warm_white if self._rgbwwprotocol else 0

    @property
    def cool_white(self):
        return self.raw_state.cool_white if self._rgbwwprotocol else 0

    # Old name is deprecated
    @property
    def cold_white(self):
        return self.cool_white

    @property
    def brightness(self):
        """Return current brightness 0-255.
        For warm white return current led level. For RGB
        calculate the HSV and return the 'value'.
        for CCT calculate the brightness.
        for ww send led level
        """
        color_mode = self.color_mode
        raw_state = self.raw_state

        if color_mode == COLOR_MODE_DIM:
            return int(raw_state.warm_white)
        elif color_mode == COLOR_MODE_CCT:
            _, b = self.getWhiteTemperature()
            return b

        r, g, b = self.getRgb()
        _, _, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        v_255 = v * 255
        if color_mode == COLOR_MODE_RGBW:
            return round((v_255 + raw_state.warm_white) / 2)
        if color_mode == COLOR_MODE_RGBWW:
            return round((v_255 + raw_state.warm_white + raw_state.cool_white) / 3)

        # Default color mode (RGB)
        return int(v_255)

    def _connect_if_disconnected(self):
        """Connect only if not already connected."""
        if self._socket is None:
            self.connect()

    @_socket_retry(attempts=0)
    def connect(self):
        self.close()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(self.timeout)
        _LOGGER.debug("%s: connect", self.ipaddr)
        self._socket.connect((self.ipaddr, self.port))

    def close(self):
        if self._socket is None:
            return
        try:
            self._socket.close()
        except socket.error:
            pass
        finally:
            self._socket = None

    def _determineMode(self):
        pattern_code = self.raw_state.preset_pattern
        if self.device_type == DeviceType.Switch:
            return MODE_SWITCH
        if pattern_code in (0x41, 0x61):
            if self.color_mode in {COLOR_MODE_DIM, COLOR_MODE_CCT}:
                return MODE_WW
            return MODE_COLOR
        elif pattern_code == 0x60:
            return MODE_CUSTOM
        elif pattern_code == 0x62:
            return MODE_MUSIC
        elif PresetPattern.valid(pattern_code):
            return MODE_PRESET
        elif BuiltInTimer.valid(pattern_code):
            return BuiltInTimer.valtostr(pattern_code)
        return "unknown"

    def _determine_protocol(self):
        # determine the type of protocol based of first 2 bytes.
        read_bytes = 2
        for protocol_cls in (ProtocolLEDENET8Byte, ProtocolLEDENETOriginal):
            protocol = protocol_cls()
            with self._lock:
                self._connect_if_disconnected()
                self._send_msg(protocol.construct_state_query())
                rx = self._read_msg(read_bytes)
                # if any response is recieved, use the protocol
                if len(rx) != read_bytes:
                    # We just sent a garage query which the old procotol
                    # cannot process, recycle the connection
                    self.close()
                    continue
                full_msg = rx + self._read_msg(
                    protocol.state_response_length - read_bytes
                )
                if protocol.is_valid_state_response(full_msg):
                    # Devices that use an 9-byte protocol
                    if self._uses_9byte_protocol(rx[1]):
                        self._protocol = ProtocolLEDENET9Byte()
                    else:
                        self._protocol = protocol
                return full_msg
        raise Exception("Cannot determine protocol")

    @_socket_retry(attempts=2)
    def query_state(self, led_type=None):
        if led_type:
            self.setProtocol(led_type)
        elif not self._protocol:
            return self._determine_protocol()

        with self._lock:
            self.connect()
            self._send_msg(self._protocol.construct_state_query())
            return self._read_msg(self._protocol.state_response_length)

    def update_state(self, retry=2):
        rx = self.query_state(retry=retry)
        if rx and self.process_state_response(rx):
            self.available = True
            return
        self.set_unavailable()

    def set_unavailable(self):
        self._is_on = False
        self.available = False

    def set_available(self):
        self.available = True

    def process_state_response(self, rx):
        if rx is None or len(rx) < self._protocol.state_response_length:
            self.set_unavailable()
            return False

        if not self._protocol.is_valid_state_response(rx):
            _LOGGER.warning(
                "%s: Recieved invalid response: %s",
                self.ipaddr,
                utils.raw_state_to_dec(rx),
            )
            return False

        raw_state = self._protocol.named_raw_state(rx)
        _LOGGER.debug("%s: State: %s", self.ipaddr, raw_state)

        if raw_state == self.raw_state:
            return

        if time.monotonic() < self._transition_complete_time:
            # Do not update the raw state if a transition is
            # in progress as the state will not be correct
            # until the transition is completed since devices
            # "FADE" into the state requested.
            return True

        self._set_raw_state(raw_state)
        self._set_power_state_from_raw_state()
        mode = self._determineMode()

        if mode == "unknown":
            _LOGGER.debug(
                "%s: Unable to determine mode from raw state: %s",
                self.ipaddr,
                utils.raw_state_to_dec(rx),
            )
            return False

        self._mode = mode
        return True

    def _set_raw_state(self, raw_state):
        """Set the raw state remapping channels as needed."""
        channel_map = CHANNEL_REMAP.get(raw_state.model_num)
        _LOGGER.debug(
            "%s: unmapped raw state: %s", self.ipaddr, utils.raw_state_to_dec(raw_state)
        )
        if not channel_map:  # Remap channels
            self.raw_state = raw_state
            return
        _LOGGER.debug(
            "%s: remapped raw state: %s", self.ipaddr, utils.raw_state_to_dec(raw_state)
        )
        self.raw_state = raw_state._replace(
            **{
                mapped: getattr(raw_state, actual)
                for mapped, actual in channel_map.items()
            }
        )

    def _set_power_state_from_raw_state(self):
        """Set the power state from the raw state."""
        power_state = self.raw_state.power_state
        if power_state == self._protocol.on_byte:
            self._is_on = True
        elif power_state == self._protocol.off_byte:
            self._is_on = False

    def __str__(self):
        rx = self.raw_state
        if not rx:
            return "No state data"
        mode = self.mode
        color_mode = self.color_mode
        pattern = rx.preset_pattern
        ww_level = rx.warm_white
        power_state = rx.power_state
        power_str = "Unknown power state"
        if power_state == self._protocol.on_byte:
            power_str = "ON "
        elif power_state == self._protocol.off_byte:
            power_str = "OFF "

        delay = rx.speed
        speed = utils.delayToSpeed(delay)
        if mode in STATIC_MODES:
            if color_mode in COLOR_MODES_RGB:
                red = rx.red
                green = rx.green
                blue = rx.blue
                mode_str = "Color: {}".format((red, green, blue))
                # Should add ability to get CCT from rgbwcapable*
                if self.rgbwcapable:
                    mode_str += " White: {}".format(ww_level)
                else:
                    mode_str += " Brightness: {}".format(self.brightness)
            elif color_mode == COLOR_MODE_DIM:
                mode_str = "Warm White: {}%".format(utils.byteToPercent(ww_level))
            elif color_mode == COLOR_MODE_CCT:
                cct_value = self.getWhiteTemperature()
                mode_str = "CCT: {}K Brightness: {}%".format(
                    cct_value[0], cct_value[1] / 255
                )
            elif color_mode == COLOR_MODE_ADDRESSABLE:
                mode_str = "Addressable"
        elif mode == MODE_PRESET:
            pat = PresetPattern.valtostr(pattern)
            mode_str = "Pattern: {} (Speed {}%)".format(pat, speed)
        elif mode == MODE_CUSTOM:
            mode_str = "Custom pattern (Speed {}%)".format(speed)
        elif BuiltInTimer.valid(pattern):
            mode_str = BuiltInTimer.valtostr(pattern)
        elif mode == MODE_MUSIC:
            mode_str = "Music"
        elif mode == MODE_SWITCH:
            mode_str = "Switch"
        else:
            mode_str = "Unknown mode 0x{:x}".format(pattern)
        mode_str += " raw state: "
        mode_str += utils.raw_state_to_dec(rx)
        return "{} [{}]".format(power_str, mode_str)

    @_socket_retry(attempts=2)
    def _change_state(self, turn_on=True):
        _LOGGER.debug("%s: Changing state to %s", self.ipaddr, turn_on)
        with self._lock:
            self._connect_if_disconnected()
            self._send_msg(self._protocol.construct_state_change(turn_on))
            # After changing state, the device replies with
            expected_response_len = 4
            # - 0x0F 0x71 [0x23|0x24] [CHECK DIGIT]
            rx = self._read_msg(expected_response_len)
            _LOGGER.debug("%s: state response %s", self.ipaddr, rx)
            if len(rx) == expected_response_len:
                new_power_state = (
                    self._protocol.on_byte if turn_on else self._protocol.off_byte
                )
                self._set_power_state(new_power_state)
            # The device will send back a state change here
            # but it will likely be stale so we want to recycle
            # the connetion so we do not have to wait as sometimes
            # it stalls
            self.close()

    def _set_power_state(self, new_power_state):
        """Set the power state in the raw state."""
        self._replace_raw_state({"power_state": new_power_state})
        self._set_power_state_from_raw_state()
        self._set_transition_complete_time()

    def _replace_raw_state(self, new_state):
        self._set_raw_state(self.raw_state._replace(**new_state))

    def turnOn(self, retry=2):
        self._change_state(retry=retry, turn_on=True)

    def turnOff(self, retry=2):
        self._change_state(retry=retry, turn_on=False)

    def isOn(self):
        return self.is_on

    def getWarmWhite255(self):
        if self.color_mode not in {COLOR_MODE_CCT, COLOR_MODE_DIM}:
            return 255
        return self.brightness

    def setWarmWhite(self, level, persist=True, retry=2):
        self.set_levels(w=utils.percentToByte(level), persist=persist, retry=retry)

    def setWarmWhite255(self, level, persist=True, retry=2):
        self.set_levels(w=level, persist=persist, retry=retry)

    def setColdWhite(self, level, persist=True, retry=2):
        self.set_levels(w2=utils.percentToByte(level), persist=persist, retry=retry)

    def setColdWhite255(self, level, persist=True, retry=2):
        self.set_levels(w2=level, persist=persist, retry=retry)

    def setWhiteTemperature(self, temperature, brightness, persist=True, retry=2):
        cold, warm = color_temp_to_white_levels(temperature, brightness)
        self.set_levels(w=warm, w2=cold, persist=persist, retry=retry)

    def getWhiteTemperature(self):
        # Assume input temperature of between 2700 and 6500 Kelvin, and scale
        # the warm and cold LEDs linearly to provide that
        raw_state = self.raw_state
        temp, brightness = white_levels_to_color_temp(
            raw_state.warm_white, raw_state.cool_white
        )
        return temp, brightness

    def getRgbw(self):
        """Returns red,green,blue,white (usually warm)."""
        if self.color_mode not in COLOR_MODES_RGB:
            return (255, 255, 255, 255)
        return self.rgbw

    @property
    def rgbw(self):
        """Returns red,green,blue,white (usually warm)."""
        return (
            self.raw_state.red,
            self.raw_state.green,
            self.raw_state.blue,
            self.raw_state.warm_white,
        )

    def getRgbww(self):
        """Returns red,green,blue,warm,cool."""
        if self.color_mode not in COLOR_MODES_RGB:
            return (255, 255, 255, 255, 255)
        return self.rgbww

    @property
    def rgbww(self):
        """Returns red,green,blue,warm,cool."""
        return (
            self.raw_state.red,
            self.raw_state.green,
            self.raw_state.blue,
            self.raw_state.warm_white,
            self.raw_state.cool_white,
        )

    def getRgbcw(self):
        """Returns red,green,blue,cool,warm."""
        if self.color_mode not in COLOR_MODES_RGB:
            return (255, 255, 255, 255, 255)
        return self.rgbcw

    @property
    def rgbcw(self):
        """Returns red,green,blue,cool,warm."""
        return (
            self.raw_state.red,
            self.raw_state.green,
            self.raw_state.blue,
            self.raw_state.cool_white,
            self.raw_state.warm_white,
        )

    def getCCT(self):
        if self.color_mode != COLOR_MODE_CCT:
            return (255, 255)
        return (self.raw_state.warm_white, self.raw_state.cool_white)

    def getSpeed(self):
        delay = self.raw_state.speed
        speed = utils.delayToSpeed(delay)
        return speed

    def setRgbw(
        self,
        r=None,
        g=None,
        b=None,
        w=None,
        persist=True,
        brightness=None,
        w2=None,
        retry=2,
    ):
        return self.set_levels(r, g, b, w, w2, persist, brightness, retry=retry)

    @_socket_retry(attempts=2)
    def set_levels(
        self,
        r=None,
        g=None,
        b=None,
        w=None,
        w2=None,
        persist=True,
        brightness=None,
    ):
        msg, updates = self._generate_levels_change(r, g, b, w, w2, persist, brightness)
        # send the message
        with self._lock:
            self._connect_if_disconnected()
            self._send_msg(msg)
            if updates:
                self._replace_raw_state(updates)
            self._set_transition_complete_time()

    def _generate_levels_change(
        self,
        r=None,
        g=None,
        b=None,
        w=None,
        w2=None,
        persist=True,
        brightness=None,
    ):
        """Generate the levels change request."""
        if (r or g or b) and (w or w2) and not self.rgbwcapable:
            print("RGBW command sent to non-RGBW device")
            raise ValueError("RGBW command sent to non-RGBW device")

        if brightness != None and r is not None and g is not None and b is not None:
            (r, g, b) = self._calculateBrightness((r, g, b), brightness)

        r_value = 0 if r is None else int(r)
        g_value = 0 if g is None else int(g)
        b_value = 0 if b is None else int(b)
        w_value = 0 if w is None else int(w)
        # ProtocolLEDENET9Byte devices support two white outputs for cold and warm.
        if w2 is None:
            # If we're only setting a single white value,
            # we set the second output to be the same as the first
            w2_value = (
                int(w) if w is not None and self.color_mode != COLOR_MODE_CCT else 0
            )
        else:
            w2_value = int(w2)

        write_mode = LevelWriteMode.ALL
        # rgbwprotocol always overwrite both color & whites
        if not self.rgbwprotocol:
            if w is None and w2 is None:
                write_mode = LevelWriteMode.COLORS
            elif r is None and g is None and b is None:
                write_mode = LevelWriteMode.WHITES

        _LOGGER.debug(
            "%s: _generate_levels_change using %s: persist=%s r=%s, g=%s b=%s, w=%s w2=%s write_mode=%s",
            self.ipaddr,
            self.protocol,
            persist,
            r_value,
            g_value,
            b_value,
            w_value,
            w2_value,
            write_mode,
        )

        msg = self._protocol.construct_levels_change(
            persist, r_value, g_value, b_value, w_value, w2_value, write_mode
        )
        updates = {}
        multi_mode = self.multi_color_mode
        if multi_mode or write_mode in WRITE_ALL_COLORS:
            updates.update({"red": r_value, "green": g_value, "blue": b_value})
        if multi_mode or write_mode in WRITE_ALL_WHITES:
            updates.update({"warm_white": w_value, "cool_white": w2_value})
        return msg, updates

    def _set_transition_complete_time(self):
        """Set the time we expect the transition will be completed.

        Devices fade to a specific state so we want to avoid
        consuming state updates into self.raw_state while a transition
        is in progress as this will provide unexpected results
        and the brightness values will be wrong until
        the transition completes.
        """
        transition_time = (
            STATE_CHANGE_LATENCY + utils.speedToDelay(self.raw_state.speed) / 100
        )
        self._transition_complete_time = time.monotonic() + transition_time
        _LOGGER.debug(
            "Transition time is %s, set _transition_complete_time to %s",
            transition_time,
            self._transition_complete_time,
        )

    def getRgb(self):
        if self.color_mode not in COLOR_MODES_RGB:
            return (255, 255, 255)
        return self.rgb

    @property
    def rgb(self):
        return (self.raw_state.red, self.raw_state.green, self.raw_state.blue)

    def setRgb(self, r, g, b, persist=True, brightness=None, retry=2):
        self.set_levels(r, g, b, persist=persist, brightness=brightness, retry=retry)

    def _calculateBrightness(self, rgb, level):
        hsv = colorsys.rgb_to_hsv(*rgb)
        return colorsys.hsv_to_rgb(hsv[0], hsv[1], level)

    def _send_msg(self, bytes):
        _LOGGER.debug(
            "%s => %s (%d)",
            self.ipaddr,
            " ".join("0x{:02X}".format(x) for x in bytes),
            len(bytes),
        )
        self._socket.send(bytes)

    def _read_msg(self, expected):
        remaining = expected
        rx = bytearray()
        begin = time.monotonic()
        while remaining > 0:
            timeout_left = self.timeout - (time.monotonic() - begin)
            if timeout_left <= 0:
                break
            try:
                self._socket.setblocking(0)
                read_ready, _, _ = select.select([self._socket], [], [], timeout_left)
                if not read_ready:
                    _LOGGER.debug(
                        "%s: timed out reading %d bytes", self.ipaddr, expected
                    )
                    break
                chunk = self._socket.recv(remaining)
                _LOGGER.debug(
                    "%s <= %s (%d)",
                    self.ipaddr,
                    " ".join("0x{:02X}".format(x) for x in chunk),
                    len(chunk),
                )
                if chunk:
                    begin = time.monotonic()
                remaining -= len(chunk)
                rx.extend(chunk)
            except socket.error as ex:
                _LOGGER.debug("%s: socket error: %s", self.ipaddr, ex)
                pass
            finally:
                self._socket.setblocking(1)
        return rx

    def getClock(self):
        msg = bytearray([0x11, 0x1A, 0x1B, 0x0F])
        with self._lock:
            self._connect_if_disconnected()
            self._send_msg(self._protocol.construct_message(msg))
            rx = self._read_msg(12)
        if len(rx) != 12:
            return
        year = rx[3] + 2000
        month = rx[4]
        date = rx[5]
        hour = rx[6]
        minute = rx[7]
        second = rx[8]
        # dayofweek = rx[9]
        try:
            dt = datetime.datetime(year, month, date, hour, minute, second)
        except:
            dt = None
        return dt

    def setClock(self):
        msg = bytearray([0x10, 0x14])
        now = datetime.datetime.now()
        msg.append(now.year - 2000)
        msg.append(now.month)
        msg.append(now.day)
        msg.append(now.hour)
        msg.append(now.minute)
        msg.append(now.second)
        msg.append(now.isoweekday())  # day of week
        msg.append(0x00)
        msg.append(0x0F)
        with self._lock:
            self._connect_if_disconnected()
            self._send_msg(self._protocol.construct_message(msg))
            # Setting the clock does not always respond so we
            # cycle the connection
            self.close()

    def setProtocol(self, protocol):
        if protocol == PROTOCOL_LEDENET_ORIGINAL:
            self._protocol = ProtocolLEDENETOriginal()
        elif protocol == PROTOCOL_LEDENET_8BYTE:
            self._protocol = ProtocolLEDENET8Byte()
        elif protocol == PROTOCOL_LEDENET_9BYTE:
            self._protocol = ProtocolLEDENET9Byte()
        else:
            raise ValueError(f"Invalid protocol: {protocol}")

    def _generate_preset_pattern(self, pattern, speed):
        """Generate the preset pattern protocol bytes."""
        PresetPattern.valtostr(pattern)
        if not PresetPattern.valid(pattern):
            raise ValueError("Pattern must be between 0x25 and 0x38")
        return self._protocol.construct_preset_pattern(pattern, speed)

    def setPresetPattern(self, pattern, speed):
        msg = self._generate_preset_pattern(pattern, speed)
        with self._lock:
            self._connect_if_disconnected()
            self._send_msg(msg)

    def getTimers(self):
        msg = bytearray([0x22, 0x2A, 0x2B, 0x0F])
        resp_len = 88
        with self._lock:
            self._connect_if_disconnected()
            self._send_msg(self._protocol.construct_message(msg))
            rx = self._read_msg(resp_len)
        if len(rx) != resp_len:
            print("response too short!")
            raise Exception

        # utils.dump_data(rx)
        start = 2
        timer_list = []
        # pass in the 14-byte timer structs
        for i in range(6):
            timer_bytes = rx[start:][:14]
            timer = LedTimer(timer_bytes)
            timer_list.append(timer)
            start += 14

        return timer_list

    def sendTimers(self, timer_list):
        # remove inactive or expired timers from list
        for t in timer_list:
            if not t.isActive() or t.isExpired():
                timer_list.remove(t)

        # truncate if more than 6
        if len(timer_list) > 6:
            print("too many timers, truncating list")
            del timer_list[6:]

        # pad list to 6 with inactive timers
        if len(timer_list) != 6:
            for i in range(6 - len(timer_list)):
                timer_list.append(LedTimer())

        msg_start = bytearray([0x21])
        msg_end = bytearray([0x00, 0xF0])
        msg = bytearray()

        # build message
        msg.extend(msg_start)
        for t in timer_list:
            msg.extend(t.toBytes())
        msg.extend(msg_end)
        with self._lock:
            self._connect_if_disconnected()
            self._send_msg(self._protocol.construct_message(msg))
            # not sure what the resp is, prob some sort of ack?
            self._read_msg(4)

    def _generate_custom_patterm(self, rgb_list, speed, transition_type):
        """Generate the custom pattern protocol bytes."""
        # truncate if more than 16
        if len(rgb_list) > 16:
            _LOGGER.warning(
                "Too many colors in %s, truncating list to %s", len(rgb_list), 16
            )
            del rgb_list[16:]
        # quit if too few
        if len(rgb_list) == 0:
            raise ValueError("setCustomPattern requires at least one color tuples")

        return self._protocol.construct_custom_effect(rgb_list, speed, transition_type)

    @_socket_retry(attempts=2)
    def setCustomPattern(self, rgb_list, speed, transition_type):
        """Set a custom pattern on the device."""
        msg = self._generate_custom_patterm(rgb_list, speed, transition_type)
        with self._lock:
            self._connect_if_disconnected()
            self._send_msg(msg)

    def refreshState(self):
        return self.update_state()


class WifiLedBulb(LEDENETDevice):
    """A LEDENET Wifi bulb device."""

    def __init__(self, ipaddr, port=5577, timeout=5):
        """Init and setup the bulb."""
        super().__init__(ipaddr, port, timeout)
        self.setup()

    def setup(self):
        """Setup the connection and fetch initial state."""
        self.connect(retry=2)
        self.update_state()
