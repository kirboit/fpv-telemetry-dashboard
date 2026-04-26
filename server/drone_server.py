import asyncio
import websockets
import serial
import serial.tools.list_ports
import struct
import json
import time

BAUD_RATE = 115200
MSP_BATTERY_STATE = 130
MSP_MOTOR = 104
MSP_ATTITUDE = 108


def find_flight_controller():
    ports = serial.tools.list_ports.comports()
    FC_VENDORS = ["STMicroelectronics", "Betaflight", "STM32"]
    all_ports = [{"port": p.device, "name": p.description} for p in ports]
    for port in ports:
        if any(vendor in str(port.manufacturer) for vendor in FC_VENDORS):
            return port.device, all_ports
    if ports:
        return ports[0].device, all_ports
    return None, []


def send_msp(ser, command):
    try:
        packet = struct.pack("<3cBBB", b"$", b"M", b"<", 0, command, command)
        ser.write(packet)
        return True
    except:
        return False


def read_msp(ser):
    try:
        header = ser.read(3)
        if len(header) < 3:
            return None, None  # timeout, not an error
        if header != b"$M>":
            return None, None
        size = struct.unpack("<B", ser.read(1))[0]
        cmd = struct.unpack("<B", ser.read(1))[0]
        data = ser.read(size)
        ser.read(1)
        return cmd, data
    except:
        return None, None


WORKING_VOLTAGE_CMD = None


def detect_voltage_command(ser):
    global WORKING_VOLTAGE_CMD

    candidates = [
        # (command_id, parse_function, description)
        (110, lambda d: round(struct.unpack("<B", d[0:1])[0] / 10.0, 2), "MSP_ANALOG"),
        (
            130,
            lambda d: round(struct.unpack("<H", d[1:3])[0] / 100.0, 2),
            "MSP_BATTERY_STATE",
        ),
        (
            60,
            lambda d: round(struct.unpack("<H", d[0:2])[0] / 100.0, 2),
            "MSP_VOLTAGE_METER",
        ),
    ]

    for cmd_id, parser, name in candidates:
        try:
            send_msp(ser, cmd_id)
            _, data = read_msp(ser)
            if data and len(data) >= 2:
                value = parser(data)
                if 2.0 < value < 5.0:  # sanity check — must be a real LiPo voltage
                    print(
                        f"✅ Voltage command detected: {name} (cmd {cmd_id}) → {value}V"
                    )
                    WORKING_VOLTAGE_CMD = (cmd_id, parser)
                    return
        except:
            pass

    print("⚠️ Could not auto-detect voltage command — voltage will show 0")
    WORKING_VOLTAGE_CMD = None


def get_battery(ser):
    global WORKING_VOLTAGE_CMD

    # If no command detected yet, keep trying
    if not WORKING_VOLTAGE_CMD:
        detect_voltage_command(ser)
        if not WORKING_VOLTAGE_CMD:
            return None

    cmd_id, parser = WORKING_VOLTAGE_CMD
    if not send_msp(ser, cmd_id):
        return None
    _, data = read_msp(ser)
    if data and len(data) >= 2:
        try:
            value = parser(data)
            if 2.0 < value < 5.0:
                return value
            else:
                # Value out of range — battery may have just been disconnected
                # Reset so we retry on next read
                if value == 0.0:
                    WORKING_VOLTAGE_CMD = None
                return None
        except:
            pass
    return None


def get_craft_name(ser):
    try:
        if not send_msp(ser, 10):
            return None
        _, data = read_msp(ser)
        if data and len(data) > 0:
            name = data.decode("utf-8", errors="ignore").strip()
            return name if name else None
    except:
        pass
    return None


def get_attitude(ser):
    if not send_msp(ser, MSP_ATTITUDE):
        return None
    cmd, data = read_msp(ser)
    if data and len(data) >= 6:
        roll = struct.unpack("<h", data[0:2])[0] / 10.0
        pitch = struct.unpack("<h", data[2:4])[0] / 10.0
        yaw = struct.unpack("<h", data[4:6])[0]
        return roll, pitch, yaw
    return None


def get_motors(ser):
    if not send_msp(ser, MSP_MOTOR):
        return None
    cmd, data = read_msp(ser)
    if data and len(data) >= 16:
        motors = struct.unpack("<8H", data[:16])
        return list(motors[:4])
    return None


def is_port_alive(port_name):
    """Check if COM port still physically exists"""
    existing = [p.device for p in serial.tools.list_ports.comports()]
    return port_name in existing


async def telemetry_handler(websocket):
    print(f"🔌 Browser connected")

    _, all_ports = find_flight_controller()
    await websocket.send(json.dumps({"status": "scanning", "ports": all_ports}))

    ser = None
    current_port = None
    fail_count = 0
    last_scan = 0
    MAX_FAILS = 8

    async def try_connect(port):
        nonlocal ser, current_port, fail_count
        try:
            if ser:
                try:
                    ser.close()
                except:
                    pass
            ser = serial.Serial(port, BAUD_RATE, timeout=0.5, write_timeout=0.5)
            current_port = port
            fail_count = 0
            detect_voltage_command(ser)
            craft_name = get_craft_name(ser) or "Unknown Drone"
            print(f"🚁 Craft name: {craft_name}")
            await websocket.send(
                json.dumps(
                    {
                        "status": "connected",
                        "port": port,
                        "craft_name": craft_name,
                    }
                )
            )
            print(f"✅ Connected to {port}")
            return True
        except Exception as e:
            print(f"❌ Failed to connect to {port}: {e}")
            await websocket.send(
                json.dumps(
                    {
                        "status": "error",
                        "message": str(e),
                        "ports": [
                            {"port": p.device, "name": p.description}
                            for p in serial.tools.list_ports.comports()
                        ],
                    }
                )
            )
            return False

    best_port, _ = find_flight_controller()
    if best_port:
        await try_connect(best_port)

    try:
        while True:
            # Handle browser commands
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                cmd = json.loads(msg)
                if cmd.get("action") == "connect":
                    await try_connect(cmd["port"])
                elif cmd.get("action") == "retry":
                    if current_port:
                        await try_connect(current_port)
                elif cmd.get("action") == "scan":
                    _, ports = find_flight_controller()
                    await websocket.send(
                        json.dumps({"status": "scanning", "ports": ports})
                    )
            except asyncio.TimeoutError:
                pass

            # Read telemetry
            if ser and ser.is_open:

                # Fast check: does the COM port still exist in Windows?
                if not is_port_alive(current_port):
                    print(f"⚠️ {current_port} vanished from system")
                    try:
                        ser.close()
                    except:
                        pass
                    _, ports = find_flight_controller()
                    await websocket.send(
                        json.dumps(
                            {
                                "status": "disconnected",
                                "message": "Drone unplugged",
                                "ports": ports,
                            }
                        )
                    )
                    ser = None
                    await asyncio.sleep(0.1)
                    continue

                # Read data
                voltage = get_battery(ser)
                attitude = get_attitude(ser)
                motors = get_motors(ser)

                got_data = voltage is not None or attitude is not None

                if not got_data:
                    fail_count += 1
                    print(f"⚠️ Empty read {fail_count}/{MAX_FAILS}")
                else:
                    fail_count = 0

                if fail_count >= MAX_FAILS:
                    print("⚠️ Too many failed reads — declaring disconnected")
                    fail_count = 0
                    try:
                        ser.close()
                    except:
                        pass
                    _, ports = find_flight_controller()
                    await websocket.send(
                        json.dumps(
                            {
                                "status": "disconnected",
                                "message": "Lost communication with drone",
                                "ports": ports,
                            }
                        )
                    )
                    ser = None
                    await asyncio.sleep(0.1)
                    continue

                if got_data:
                    roll, pitch, yaw = attitude if attitude else (0.0, 0.0, 0.0)
                    await websocket.send(
                        json.dumps(
                            {
                                "status": "connected",
                                "port": current_port,
                                "voltage": voltage if voltage is not None else 0.0,
                                "roll": roll,
                                "pitch": pitch,
                                "yaw": yaw,
                                "motors": motors if motors else [0, 0, 0, 0],
                            }
                        )
                    )

            # Auto-scan ports when disconnected
            if not ser or not ser.is_open:
                now = time.time()
                if now - last_scan > 2.0:
                    last_scan = now
                    _, ports = find_flight_controller()
                    await websocket.send(
                        json.dumps({"status": "scanning", "ports": ports})
                    )

            await asyncio.sleep(0.05)

    except websockets.exceptions.ConnectionClosed:
        print("🔌 Browser disconnected")
    finally:
        if ser:
            try:
                ser.close()
            except:
                pass


async def main():
    print("🚀 Drone WebSocket server started on ws://localhost:8765")
    async with websockets.serve(telemetry_handler, "localhost", 8765):
        await asyncio.Future()


asyncio.run(main())
