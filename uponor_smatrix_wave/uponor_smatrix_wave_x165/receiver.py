#!/usr/bin/env python3
"""Live receive-only Uponor Smatrix Wave decoder for an RTL-SDR."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout, redirect_stderr
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import datetime, timedelta
import multiprocessing
from pathlib import Path
import queue
import os
import sys
import signal
import threading
import time
import traceback

from uponor_smatrix_wave_x165.demod import demodulate_live_burst, u8_iq_to_complex
from uponor_smatrix_wave_x165.live import LiveBurst, StreamingBurstDetector
from uponor_smatrix_wave_x165.protocol import DEFAULT_CONTROLLER_ID, FrameError, KNOWN_THERMOSTAT_IDS, parse_thermostat_frame
from uponor_smatrix_wave_x165.rtl_input import RtlError, RtlSdrDllStream, RtlSdrProcessStream, discover_rtlsdr_dll
from uponor_smatrix_wave_x165.state import DeviceRegistry, display_temperature
from uponor_smatrix_wave_x165.mqtt_bridge import MqttBridge, add_mqtt_arguments, config_from_args
from uponor_smatrix_wave_x165.frame_log import FrameJournal
from uponor_smatrix_wave_x165.interface import parse_house_temperature, parse_system_mode
from uponor_smatrix_wave_x165.rooms import room_name


def thermostat_id(value: str) -> bytes:
    try:
        device_id = bytes.fromhex(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use an 8-digit hex thermostat ID, e.g. 1234ABCD") from None
    if len(device_id) != 4:
        raise argparse.ArgumentTypeError("Thermostat ID must contain exactly 4 bytes")
    return device_id


def arguments(default_controller_id=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", choices=("auto", "dll", "rtl_sdr"), default="auto")
    parser.add_argument("--rtl-dll", help="Path to rtlsdr.dll")
    parser.add_argument("--rtl-sdr", default="rtl_sdr", help="Path/name of rtl_sdr.exe fallback")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--controller-id", type=thermostat_id,
                        default=os.getenv("UPONOR_CONTROLLER_ID", default_controller_id),
                        help="Your X-165 controller ID (or UPONOR_CONTROLLER_ID)")
    parser.add_argument("--retry-rf", action="store_true", help="Retry RF input errors after 5 seconds")
    parser.add_argument("--interface-id", type=thermostat_id, default=os.getenv("UPONOR_INTERFACE_ID") or None,
                        help="Opt into provisional I-167 house-temperature support for this interface ID")
    parser.add_argument("--frequency", type=int, default=868_250_000)
    parser.add_argument("--sample-rate", type=int, default=250_000)
    parser.add_argument("--ppm", type=int, default=0)
    parser.add_argument("--block-samples", type=int, default=32_768)
    parser.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)), help="parallel burst-demodulation workers")
    parser.add_argument("--threshold-db", type=float, default=8.0)
    parser.add_argument("--duration", type=float, help="Stop after this many seconds")
    parser.add_argument("--record", type=Path, help="Save raw unsigned 8-bit interleaved I/Q without blocking decode")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-device", type=thermostat_id,
                        help="Show debug for this thermostat only; implies --debug. MQTT still receives all devices.")
    parser.add_argument("--log-file", type=Path, help="Append Python console output to a UTF-8 file as well as the terminal")
    parser.add_argument("--frame-log", type=Path, help="Append CRC-valid research frames to JSONL; all families supported")
    parser.add_argument("--trace-id", type=thermostat_id, action="append", default=[],
                        help="Limit research frames to this ID in the body; repeatable; thermostat context is also saved")
    parser.add_argument("--quiet", action="store_true", help="Hide routine console frames; logging and MQTT continue")
    add_mqtt_arguments(parser)
    args = parser.parse_args()
    if args.controller_id is None:
        parser.error("Set --controller-id to your X-165 ID (8 hex digits), or UPONOR_CONTROLLER_ID")
    if args.trace_id and args.frame_log is None:
        parser.error("--trace-id requires --frame-log")
    output_paths = [p.resolve() for p in (args.log_file, args.frame_log, args.record) if p is not None]
    if len(output_paths) != len(set(output_paths)):
        parser.error("Console log, frame log and IQ recording must use different files")
    if args.record is not None and args.record.exists():
        parser.error("--record refuses to overwrite an existing IQ file; choose a new filename")
    return args


def open_source(args: argparse.Namespace):
    dll = discover_rtlsdr_dll(args.rtl_dll)
    if args.input in ("auto", "dll") and dll is not None:
        return RtlSdrDllStream(
            dll,
            device_index=args.device,
            center_frequency=args.frequency,
            sample_rate=args.sample_rate,
            ppm=args.ppm,
        )
    if args.input == "dll":
        raise RtlError("rtlsdr.dll not found; pass --rtl-dll PATH")
    return RtlSdrProcessStream(
        args.rtl_sdr,
        device_index=args.device,
        center_frequency=args.frequency,
        sample_rate=args.sample_rate,
        ppm=args.ppm,
    )


def format_value(value: float | None, *, cached: bool) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f} C" + (" (cached)" if cached else "")


def device_label(device_id: bytes) -> str:
    """Hex device_id, annotated with its room name when known."""
    name = room_name(device_id)
    hex_id = device_id.hex().upper()
    return f"{hex_id} ({name})" if name else hex_id


def print_frame(frame, state, observed_at: datetime, result: dict, burst: LiveBurst, debug: bool, sample_rate: int) -> None:
    temperature_cached = frame.raw_temperature is None and state.temperature_c is not None
    setpoint_cached = frame.raw_setpoint is None and state.setpoint_c is not None
    known = " known" if frame.device_id in KNOWN_THERMOSTAT_IDS else " discovered"
    print(f"[{observed_at:%H:%M:%S}] Uponor {device_label(frame.device_id)} ({known.strip()})")
    print(f"  temperature: {format_value(state.temperature_c, cached=temperature_cached)}")
    print(f"  setpoint:    {format_value(state.setpoint_c, cached=setpoint_cached)}")
    if state.bypass_enabled is not None:
        print(f"  bypass:      {'enabled' if state.bypass_enabled else 'disabled'}")
    if state.actuator_open is not None:
        print(f"  actuator:    {'open' if state.actuator_open else 'closed'}")
    print(f"  frame:       {frame.frame_name}")
    print("  crc:         OK")
    print(f"  valid count: {state.valid_packet_count}")
    if debug:
        tlvs = " ".join(f"{tag:02X}={value:04X}" for tag, value in frame.tlv_items)
        print(f"  raw:         {frame.raw.hex(' ').upper()}")
        print(f"  endpoints:   {frame.controller_id.hex().upper()} -> {frame.device_id.hex().upper()}")
        print(f"  TLVs:        {tlvs}")
        print(f"  trailer:     {frame.unknown_inner_word.hex().upper()}")
        print(
            "  RF:          "
            f"{result['tone_low_hz']:+.0f}/{result['tone_high_hz']:+.0f} Hz, "
            f"bitrate {result['estimated_bitrate']:.1f}, quality {result['fsk_quality']:.2f}, "
            f"clock {result['clock_score']:.3f}, burst {1000*(burst.end_sample-burst.start_sample)/sample_rate:.2f} ms"
        )
        print(
            "  demod:       "
            f"{result['bit_order']}, inverted={result['inverted']}, "
            f"sync_errors={result['sync_errors']}, confidence={result['confidence']:.2f}"
        )
    print(flush=True)


def decode_burst(
    burst: LiveBurst,
    *,
    sample_rate: int,
    controller_id: bytes | None = DEFAULT_CONTROLLER_ID,
) -> tuple[dict | None, object | None, str | None]:
    try:
        result = demodulate_live_burst(
            burst.samples,
            sample_rate=sample_rate,
            threshold_power=burst.threshold_power,
        )
    except (ValueError, FloatingPointError) as exc:
        return None, None, f"rejected by DSP: {exc}"
    if result is None:
        return None, None, "no exact sync"
    packet = result["packet"]
    try:
        frame = parse_thermostat_frame(packet, controller_id=controller_id)
    except FrameError as exc:
        return result, None, str(exc)
    return result, frame, None


def present_decoded(
    burst: LiveBurst,
    decoded: tuple[dict | None, object | None, str | None],
    *,
    sample_rate: int,
    capture_start: datetime,
    registry: DeviceRegistry,
    debug: bool,
    mqtt_bridge: MqttBridge | None = None,
    debug_device: bytes | None = None,
    frame_journal: FrameJournal | None = None,
    quiet: bool = False,
    interface_id: bytes | None = None,
    controller_id: bytes | None = None,
) -> tuple[bool, bool]:
    result, frame, error = decoded
    if result is None:
        if debug and debug_device is None and not quiet:
            print(f"[debug] burst @{burst.start_sample/sample_rate:.6f}s: {error}", flush=True)
        return False, False
    observed_at = capture_start + timedelta(seconds=burst.start_sample / sample_rate)
    if frame_journal is not None:
        frame_journal.observe(result["packet"], observed_at, frame=frame, result=result,
                              sample_offset=burst.start_sample, sample_rate=sample_rate)
    if frame is None:
        if interface_id is not None and controller_id is not None:
            try:
                mode = parse_system_mode(result["packet"], interface_id=interface_id)
            except FrameError:
                pass
            else:
                status = mqtt_bridge.observe_mode(controller_id, mode, observed_at) if mqtt_bridge else "MQTT disabled"
                if not quiet:
                    print(f"[{observed_at:%H:%M:%S}] X-165 {controller_id.hex().upper()}  mode={mode.mode} via I-167 {interface_id.hex().upper()}  {status}", flush=True)
                return True, False
        if interface_id is not None:
            try:
                house = parse_house_temperature(result["packet"], interface_id=interface_id)
            except FrameError:
                pass
            else:
                status = mqtt_bridge.observe_house(house, observed_at) if mqtt_bridge else "MQTT disabled"
                if not quiet:
                    print(f"[{observed_at:%H:%M:%S}] I-167 {interface_id.hex().upper()}  house={display_temperature(house.raw_temperature)} C  {status}", flush=True)
                return True, False  # Preserve the existing thermostat-only valid count.
        if debug and debug_device is None and not quiet:
            raw = result["packet"].hex(" ").upper()
            print(f"[debug] exact-sync candidate @{burst.start_sample/sample_rate:.6f}s rejected: {error}")
            print(f"[debug] raw candidate: {raw}", flush=True)
        return True, False
    show = not quiet and (debug_device is None or frame.device_id == debug_device)
    if show and frame.device_id not in registry.devices:
        print(f"[{observed_at:%H:%M:%S}] New thermostat discovered: {device_label(frame.device_id)}", flush=True)
    state = registry.update(frame, observed_at)
    if mqtt_bridge is not None:
        status = mqtt_bridge.observe(state)
    if not show:
        return True, True
    if mqtt_bridge is not None:
        temp = display_temperature(state.last_raw_temperature)
        target = display_temperature(state.last_raw_setpoint)
        print(f"[{observed_at:%H:%M:%S}] {device_label(frame.device_id)}  temp={temp}  setpoint={target}  {status}", flush=True)
    if debug or debug_device is not None or mqtt_bridge is None:
        print_frame(frame, state, observed_at, result, burst, debug or debug_device is not None, sample_rate)
    return True, True


def run(args: argparse.Namespace, shutdown: threading.Event | None = None) -> int:
    if args.sample_rate <= 0 or args.block_samples <= 0 or args.workers <= 0:
        raise SystemExit("sample rate, block size, and worker count must be positive")
    mqtt_bridge = MqttBridge(config_from_args(args)) if args.mqtt else None
    source = open_source(args)
    try:
        frame_journal = FrameJournal(args.frame_log, trace_ids=args.trace_id, metadata={
            "controller_id": args.controller_id.hex().upper(), "frequency": args.frequency,
            "sample_rate": args.sample_rate, "ppm": args.ppm}) if args.frame_log else None
    except OSError:
        source.close()
        raise
    if mqtt_bridge is not None:
        try:
            mqtt_bridge.start()
        except Exception:
            source.close()
            if frame_journal:
                frame_journal.close()
            raise
    print(f"RTL-SDR opened: {source.device_name}")
    print(f"Input: {args.frequency/1e6:.6f} MHz, {args.sample_rate} samples/s, PPM {args.ppm}")
    if hasattr(source, "dll_path"):
        print(f"Library: {source.dll_path}")
    if args.record:
        print(f"Raw recording: {args.record.resolve()}")
    print("Receive-only decoder running. Press Ctrl+C to stop.", flush=True)

    raw_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
    record_queue: queue.Queue[bytes | None] | None = queue.Queue(maxsize=64) if args.record else None
    shutdown = shutdown if shutdown is not None else threading.Event()
    stop = threading.Event()
    reader_error: list[BaseException] = []
    recorder_error: list[BaseException] = []
    record_drops = [0]
    capture_start = datetime.now().astimezone()
    monotonic_start = time.monotonic()

    def reader() -> None:
        try:
            while not stop.is_set() and not shutdown.is_set():
                if args.duration is not None and time.monotonic() - monotonic_start >= args.duration:
                    break
                raw = source.read(args.block_samples)
                if not raw:
                    if not stop.is_set() and not shutdown.is_set():
                        raise RtlError("RF input ended unexpectedly; check the USB receiver")
                    break
                if record_queue is not None:
                    try:
                        record_queue.put_nowait(raw)
                    except queue.Full:
                        record_drops[0] += 1
                while not stop.is_set():
                    try:
                        raw_queue.put(raw, timeout=0.25)
                        break
                    except queue.Full:
                        pass
        except BaseException as exc:
            reader_error.append(exc)
        finally:
            while True:
                try:
                    raw_queue.put(None, timeout=0.25)
                    break
                except queue.Full:
                    if stop.is_set():
                        break
            if record_queue is not None:
                while True:
                    try:
                        record_queue.put(None, timeout=0.25)
                        break
                    except queue.Full:
                        if recorder_thread is not None and not recorder_thread.is_alive():
                            break

    def recorder() -> None:
        assert args.record is not None and record_queue is not None
        try:
            with args.record.open("wb") as output:
                while True:
                    raw = record_queue.get()
                    if raw is None:
                        break
                    output.write(raw)
        except BaseException as exc:
            recorder_error.append(exc)
            # Keep draining so an optional recording failure can never stall
            # the receive/decode path or its shutdown sentinel.
            while True:
                raw = record_queue.get()
                if raw is None:
                    break

    reader_thread = threading.Thread(target=reader, name="rtl-reader", daemon=True)
    recorder_thread = threading.Thread(target=recorder, name="iq-recorder", daemon=True) if args.record else None
    if recorder_thread:
        recorder_thread.start()
    reader_thread.start()

    detector = StreamingBurstDetector(args.sample_rate, threshold_db=args.threshold_db)
    registry = DeviceRegistry()
    stats = {"bursts": 0, "sync": 0, "valid": 0, "bytes": 0}
    pending: list[tuple[LiveBurst, Future]] = []

    def collect(*, block: bool) -> None:
        while pending and (block or pending[0][1].done()):
            burst, future = pending.pop(0)
            synced, valid = present_decoded(
                burst,
                future.result(),
                sample_rate=args.sample_rate,
                capture_start=capture_start,
                registry=registry,
                debug=args.debug,
                mqtt_bridge=mqtt_bridge,
                debug_device=args.debug_device,
                frame_journal=frame_journal,
                quiet=args.quiet,
                interface_id=args.interface_id,
                controller_id=args.controller_id,
            )
            stats["sync"] += int(synced)
            stats["valid"] += int(valid)
            if not block:
                continue
            break

    # The proven timing search is CPU-heavy Python work. Separate processes
    # keep acquisition real-time on CPython where threads would contend on
    # the GIL during the search loops.
    # Explicit spawn avoids forking an active MQTT/network thread on Linux.
    executor = ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn"))
    try:
        while not stop.is_set() and not shutdown.is_set():
            try:
                raw = raw_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if raw is None:
                break
            stats["bytes"] += len(raw)
            for burst in detector.feed(u8_iq_to_complex(raw)):
                stats["bursts"] += 1
                pending.append((burst, executor.submit(decode_burst, burst, sample_rate=args.sample_rate, controller_id=args.controller_id)))
                collect(block=False)
                if len(pending) >= args.workers * 8:
                    collect(block=True)
        for burst in ([] if stop.is_set() or shutdown.is_set() else detector.flush()):
            stats["bursts"] += 1
            pending.append((burst, executor.submit(decode_burst, burst, sample_rate=args.sample_rate, controller_id=args.controller_id)))
        while pending and not stop.is_set() and not shutdown.is_set():
            collect(block=True)
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        stop.set()
        reader_thread.join(timeout=3)
        source.close()
        executor.shutdown(wait=True, cancel_futures=True)
        if recorder_thread:
            recorder_thread.join(timeout=3)
        if mqtt_bridge is not None:
            mqtt_bridge.close()
        if frame_journal is not None:
            frame_journal.close()

    if reader_error and not shutdown.is_set():
        raise RtlError(f"RTL-SDR reader failed: {reader_error[0]}") from reader_error[0]
    seconds = stats["bytes"] / (2.0 * args.sample_rate)
    print(
        f"Summary: {seconds:.1f}s I/Q, {stats['bursts']} RF bursts, "
        f"{stats['sync']} exact-sync candidates, {stats['valid']} valid thermostat frames."
    )
    if record_drops[0]:
        print(f"Warning: raw recording dropped {record_drops[0]} blocks; decoding was prioritized.")
    if recorder_error:
        print(f"Warning: raw recording failed: {recorder_error[0]}")
    if stats["valid"] == 0:
        print("No valid thermostat frame received; the observed T-165 period is about 225.5 seconds.")
    return 0


class ConsoleLog:
    """Mirror Python output without piping the receiver through PowerShell."""

    def __init__(self, console, logfile, lock):
        self.console = console
        self.logfile = logfile
        self.lock = lock

    def write(self, text):
        with self.lock:
            self.console.write(text)
            self.logfile.write(text)
            self.logfile.flush()
        return len(text)

    def flush(self):
        with self.lock:
            self.console.flush()
            self.logfile.flush()


def run_with_errors(args, shutdown=None) -> int:
    shutdown = shutdown if shutdown is not None else threading.Event()
    try:
        while not shutdown.is_set():
            try:
                # RF cleanup uses its own stop event; a retry must not cancel
                # the process-wide shutdown event.
                return run(args, shutdown)
            except RtlError as exc:
                if not args.retry_rf or shutdown.is_set():
                    raise
                print(f"RF input failed: {exc}; retrying in 5 seconds", file=sys.stderr, flush=True)
                shutdown.wait(5)
        return 0
    except RtlError as exc:
        print(f"RTL-SDR error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        return 2


def main(default_controller_id=None) -> int:
    args = arguments(default_controller_id)
    shutdown = threading.Event()
    previous = {}
    def request_stop(signum, frame):
        shutdown.set()
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, request_stop)
    try:
        if args.log_file is None:
            return run_with_errors(args, shutdown)
        with args.log_file.open("a", encoding="utf-8", buffering=1) as logfile:
            lock = threading.RLock()
            with redirect_stdout(ConsoleLog(sys.stdout, logfile, lock)), redirect_stderr(ConsoleLog(sys.stderr, logfile, lock)):
                print(f"\n--- Receiver started {datetime.now().astimezone().isoformat()} ---", flush=True)
                return run_with_errors(args, shutdown)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
