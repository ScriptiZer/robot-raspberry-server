# Raspberry Pi Robot Server

TCP motor-control server and low-latency H.264 UDP video streamer for a four-wheel DC-motor robot.

This project is intended to run on **Raspberry Pi OS** on a **trusted private Wi-Fi network**. Do not expose it to the public internet.

## Architecture

```
                 trusted Wi-Fi LAN
  -------------------------------------------------
           TCP :5000 NDJSON              UDP :5001 H.264 MPEG-TS
  Laptop  =======================>  Raspberry Pi control server
          <=======================  FFmpeg video streamer
                                           |
                          +----------------+----------------+
                          |                                 |
                     MotorWatchdog                    USB webcam
                     (500 ms failsafe)                /dev/video0
                          |
                    MotorController
                     FL  FR  RL  RR
                          |
                    motor driver ICs
                    (external battery)
```

Control and video use **separate channels**. A lost video packet cannot stop command processing. A lost control socket or a frozen TCP selssion still trips the **watchdog**, which stops every motor.

## Safety

- **Never drive motors from Raspberry Pi GPIO.** GPIO pins are 3.3 V logic only. They must go to a motor driver (L298N, TB6612, DRV8833, etc.).
- Motor power must come from a battery / PSU sized for the motors and driver.
- Share a **common ground** between Raspberry Pi GND and the driver’s logic ground when the driver requires it.
- Voltage and current ratings depend on *your* motors and driver. This software does not select hardware.
- **STOP always wins.** On startup, shutdown, disconnect, auth failure, exception, SIGINT/SIGTERM, and watchdog timeout, motors are commanded to stop.
- If no valid `control` or `stop` packet arrives for **500 ms** (configurable), motors stop even if the TCP connection still looks open.

GPIO numbers in the examples are **not guaranteed to match your board**. Treat them as placeholders until you verify the wiring.

A single L298N provides two independently controlled motor channels, not four. A four-wheel chassis can pair the left motors on one channel and the right motors on the other only if the driver can handle each pair's combined stall current. The current software configuration expects four independent motor channels; do not use it for live motor control with one L298N until the wiring and control mapping are adapted.

## Requirements

- Raspberry Pi OS (Bookworm or later recommended)
- Python 3.11+
- USB webcam (typically `/dev/video0`)
- Four DC motors + one L298N dual H-bridge motor driver module
- `ffmpeg` and `v4l-utils`
- Laptop client on the same LAN (`robot-laptop-client`)

## Installation

```bash
sudo apt update
sudo apt install python3-venv python3-pip ffmpeg v4l-utils python3-lgpio python3-libgpiod
sudo usermod -aG gpio,video "$USER"
# Log out and back in (or reboot) so group membership applies.

cd robot-raspberry-server
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
cp config.yaml.example config.yaml
# Edit .env: CONTROL_TOKEN, LAPTOP_IP, CAMERA_DEVICE, SIMULATION_MODE
```

`--system-site-packages` lets the venv import distro `lgpio` if pip’s copy is unavailable.

List cameras:

```bash
chmod +x scripts/list_cameras.sh
./scripts/list_cameras.sh
# or:
python scripts/list_cameras.py
```

Those wrappers run:

```bash
v4l2-ctl --list-devices
v4l2-ctl --device=/dev/video0 --list-formats-ext
```

## Configuration

Values load in this order: built-in defaults → `config.yaml` → environment / `.env`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIMULATION_MODE` | `true` in `.env.example` | Log motor actions; do not open GPIO |
| `CONTROL_HOST` | `0.0.0.0` | TCP bind address |
| `CONTROL_PORT` | `5000` | TCP motor-control port |
| `VIDEO_PORT` | `5001` | UDP destination port on the laptop |
| `CONTROL_TOKEN` | `change-me` | Shared secret; empty disables auth |
| `LAPTOP_IP` | `192.168.1.20` | UDP unicast destination if no client has connected |
| `VIDEO_FOLLOW_CLIENT` | `true` | Retarget UDP stream to the connected laptop IP |
| `VIDEO_ENABLED` | `true` | Set `false` to run motors without a camera |
| `WATCHDOG_TIMEOUT` | `0.5` | Seconds without a valid control packet before STOP |
| `CAMERA_DEVICE` | `/dev/video0` | V4L2 device |
| `VIDEO_WIDTH` / `HEIGHT` / `FPS` | `1280` / `720` / `30` | Encode size and frame rate |
| `STEER_SLOW_FACTOR` | `0.45` | Inner-wheel ratio for forward/back diagonals |
| `IN_PLACE_TURN_RATIO` | `1.0` | Reverse-side ratio for in-place A/D turns |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every packet |
| `LOG_FILE` | empty | Optional rotating log path |

### Example motor pins (BCM, not physical header numbers)

This rear-wheel-drive example uses only the two rear motors:

```text
rear_left:  in1=24 in2=25 pwm=19
rear_right: in1=5  in2=6  pwm=26
```

Use the BCM numbers shown above; the physical Pi header pins are different. Always verify the mapping before powering the motor driver.

### GPIO wiring reference (rear-wheel drive)

```text
                               Raspberry Pi (BCM numbering)
                        +---------------------------------------------+
                        | GPIO24  GPIO25  GPIO19  GPIO5  GPIO6  GPIO26 |
                        |   o      o       o      o     o      o     |
                        +---------------------------------------------+
                                                  |
                         common ground  +--------------------------+
                                          |
                               +----------------------------+
                               |  Driver logic / H-bridge    |
                               |  2-motor rear-wheel driver   |
                               +----------------------------+
      rear_left IN1 ───────┐
      rear_left IN2 ───────┤◄─ GPIO24 / GPIO25 / GPIO19
      rear_left PWM ───────┘

      rear_right IN1 ──────┐
      rear_right IN2 ──────┤◄─ GPIO5 / GPIO6 / GPIO26
      rear_right PWM ──────┘

                               Driver outputs to motors
                       +-------------------------------+
                       | RL motor   RR motor           |
                       +-------------------------------+

                Pi GND ───────────────┬───────────────────────┐
                                       │
                               driver logic ground

                motor battery + ──────► driver VM / VCC
                motor battery - ──────► driver GND / power return
```

GPIO wiring table:

| Motor      | Driver pin | Pi GPIO BCM | Purpose                               |
| ---------- | ---------- | ----------- | ------------------------------------- |
| rear_left  | IN1        | 24          | Left motor forward command            |
| rear_left  | IN2        | 25          | Left motor reverse command            |
| rear_left  | PWM        | 19          | Left motor speed control              |
| rear_right | IN1        | 5           | Right motor forward command           |
| rear_right | IN2        | 6           | Right motor reverse command           |
| rear_right | PWM        | 26          | Right motor speed control             |
| Pi         | GND        | GND         | Shared logic ground with driver       |
| Driver     | VM / VCC   | -           | Motor battery positive supply         |
| Driver     | GND        | -           | Motor battery negative / power return |

Wiring details:

- Each motor channel uses a 3-wire control set: `IN1`, `IN2`, and `PWM` (speed control).
- This setup uses the two rear motors only: `rear_left` and `rear_right`.
- Use BCM GPIO numbers, not physical header pin numbers.
- Connect the Raspberry Pi GND and the driver logic ground together so the control signals have a valid reference.
- Motor power must come from a separate battery or PSU for the motors. Do not power motors from Raspberry Pi 5V.
- GPIO pins are 3.3 V logic only. The driver board must accept 3.3 V inputs or use the correct level-shifting / logic-voltage arrangement.
- Keep the motor battery disconnected while you verify the pin map. Only after the wiring is confirmed should you enable the motor supply.

The motor test helper can pulse each motor channel without starting the full server. Run it with the wheels lifted and clear before any live robot movement.

```bash
python scripts/test_motor.py --motor rear_left --direction forward
python scripts/test_motor.py --motor all --direction forward --speed 0.15 --duration 0.5
```

### Differential steering (tunable)

| Command | Wheels |
| --- | --- |
| Forward | all forward |
| Backward | all reverse |
| Left | left reverse (× `IN_PLACE_TURN_RATIO`), right forward |
| Right | left forward, right reverse |
| Forward-left | left × `STEER_SLOW_FACTOR`, right full, both forward |
| Forward-right | right slower, left full, both forward |
| Backward-left / right | same idea in reverse |

## Control protocol

Newline-delimited JSON over TCP. See `protocol.py` for the canonical description.

Laptop → Pi:

```json
{"type":"auth","token":"change-me"}
{"type":"control","forward":true,"backward":false,"left":false,"right":false,"speed":70}
{"type":"stop"}
{"type":"heartbeat"}
```

Pi → laptop: `auth_ok`, `auth_error`, `busy`, `error`.

The laptop should send `control` about every **100 ms**. Only `control` and `stop` refresh the motor watchdog. A `heartbeat` keeps the socket alive but will **not** prevent a watchdog STOP if movement packets stop.

Malformed JSON is logged and ignored; the process does not crash.

One controlling client at a time. Extra connections receive `busy` and are closed.

## Video

FFmpeg captures V4L2, encodes H.264, and sends MPEG-TS over UDP to `LAPTOP_IP:VIDEO_PORT`.

Encoder selection is automatic and logged:

1. `h264_v4l2m2m` (hardware, when present)
2. `h264_omx` (legacy hardware)
3. `libx264` ultrafast / zerolatency (CPU fallback)

Raspberry Pi 5 typically uses the CPU encoder. The log line `Video encoder selected: ...` tells you which path is active.

If FFmpeg exits, it is restarted after `VIDEO_RESTART_DELAY`. Motor control keeps running.

## Startup

Simulation (no GPIO, recommended until wiring is proven):

```bash
cd robot-raspberry-server
source .venv/bin/activate
cp .env.example .env   # if you have not already
# keep SIMULATION_MODE=true
python app.py --simulation
```

Live motors (only after the test checklist):

```bash
# in .env:
# SIMULATION_MODE=false
python app.py
```

## Local component checks

These checks run independently; they do not require the robot server to be running.

Check configured motor pins and GPIO access with a brief, low-speed pulse. Keep the wheels lifted and clear, and confirm the BCM pin mapping before testing. The command asks you to type `MOVE`; `--motor all` tests each channel one at a time.

```bash
python scripts/test_motor.py --help
python scripts/test_motor.py --motor rear_left --direction forward
python scripts/test_motor.py --motor all --direction forward --speed 0.15 --duration 0.5
```

Check camera detection and supported formats without starting FFmpeg's reconnect loop:

```bash
python scripts/list_cameras.py
```

Check the control server and watchdog without GPIO or camera hardware. It listens on the configured control port; press Ctrl+C to stop it.

```bash
SIMULATION_MODE=true VIDEO_ENABLED=false python app.py --simulation
```

For a live video test, first confirm the camera with `scripts/list_cameras.py`, then start the app with `VIDEO_ENABLED=true`. Avoid running multiple server instances because they compete for the same control port.

## systemd

Edit user and paths in `systemd/robot-server.service`, or run:

```bash
chmod +x scripts/install_service.sh
./scripts/install_service.sh
```

The unit:

- starts after `network-online.target`
- restarts on failure
- runs the project virtualenv
- uses `SupplementaryGroups=gpio video` so it does not need to be root

Manual install:

```bash
sudo cp systemd/robot-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robot-server.service
journalctl -u robot-server.service -f
```

## Firewall ports

| Port | Proto | Direction |
| --- | --- | --- |
| 5000 | TCP | inbound from laptop |
| 5001 | UDP | outbound to laptop |

```bash
sudo ufw allow 5000/tcp
sudo ufw allow out 5001/udp
```

Video is unicast **to** the laptop, so the Pi does not need an inbound UDP allow for 5001. The laptop must allow inbound UDP 5001.

## Logging

Console by default. Optional rotating file via `LOG_FILE`. Per-packet spam is `DEBUG` only. State changes (FORWARD, STOP, watchdog, connect/disconnect) are `INFO` / `WARNING`.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `lgpio` / permission errors | user in `gpio` group; reboot; `SIMULATION_MODE=true` until fixed |
| Camera not found | `scripts/list_cameras.sh`; `CAMERA_DEVICE`; user in `video` group |
| No video on laptop | `LAPTOP_IP` / `VIDEO_FOLLOW_CLIENT`; laptop firewall UDP 5001; `ffmpeg` installed |
| Motors never move | still in simulation; wrong BCM pins; driver enable jumper; shared ground |
| Motors cut out while driving | watchdog: laptop must send `control` every ~100 ms; Wi-Fi drop |
| Second GUI cannot connect | only one client; stop the first |
| Auth errors | same `CONTROL_TOKEN` on both devices |

## Testing checklist

1. Put the Pi and laptop on the same Wi-Fi.
2. On the Pi: `hostname -I` to get the IP.
3. From the laptop: `ping <pi-ip>`.
4. Start this server in simulation: `python app.py --simulation` with `SIMULATION_MODE=true`.
5. Start `robot-laptop-client`, set the Pi IP, click Connect.
6. Hold W/A/S/D (including chords). Logs should show `SIM MOTOR: FORWARD speed=…` and similar.
7. Stop sending commands (quit the laptop app or pull Wi-Fi). Within 500 ms you must see a watchdog or disconnect STOP.
8. Attach the USB webcam and run `./scripts/list_cameras.sh`.
9. Confirm the laptop GUI shows the live stream (not an external player).
10. Drop Wi-Fi briefly; the laptop must reconnect; this server must STOP motors while the link is gone.
11. Confirm STOP on disconnect **before** connecting motor power.
12. Only then wire IN1/IN2/PWM to a motor driver powered by an external battery and set `SIMULATION_MODE=false`.

Laptop-only GUI test (no Pi): run `python mock_pi_server.py` inside `robot-laptop-client`.

## Motor-driver warning (read before connecting hardware)

1. Leave `SIMULATION_MODE=true` until TCP + video + watchdog are proven.
2. Disconnect motor power while checking GPIO mapping.
3. Confirm IN1/IN2/PWM for each of FL, FR, RL, RR.
4. Confirm driver logic voltage (3.3 V vs 5 V) is compatible with the Pi.
5. Only then connect the motor battery and set `SIMULATION_MODE=false`.

## Project layout

```text
robot-raspberry-server/
  app.py
  motor_controller.py
  control_server.py
  video_streamer.py
  watchdog.py
  config.py
  protocol.py
  logging_setup.py
  requirements.txt
  .env.example
  config.yaml.example
  systemd/robot-server.service
  scripts/list_cameras.sh
  scripts/list_cameras.py
  scripts/install_service.sh
```
