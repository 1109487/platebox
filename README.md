# PlateBox

A small, portable box that records vehicle number plates from an IP camera.
Plug it in, join it to the camera's network, point the camera at it, and a web page shows every
plate it has seen: the picture, the number, and every time it came by.

It is a **separate product** from the Business Assistant platform in the rest of this repository.
It shares only the idea (and the plate-reading approach); it has its own code, database, login and
installer, and needs no server, cloud account or internet connection once installed.

```
 IP camera ──(SFTP, on motion)──►  PlateBox  ──►  web page (phone / laptop on the same network)
 e.g. CP Plus                      reads plates       plate picture · number · all times seen
 CP-UNC-TA41L6C-D-Q2               deletes the rest
```

## What it does

1. **First-run wizard** (about five minutes, no technical knowledge):
   1. Claim the box: setup code (printed on its label) and an administrator password.
   2. **Network**: use the cable it is on, or pick a Wi-Fi network and enter its password.
   3. **Camera**: the box searches the network (ONVIF discovery, then a port sweep) and lists what it
      finds; or type the camera's address.
   4. **Connect**: shows exactly what to enter on the camera (type, server address, port, user,
      password, folder) with click-to-copy, plus the two other camera switches. The screen then
      waits, live, for the camera to connect and for the first picture, and says **Setup is done**.
2. **Plates screen**: one card per plate: picture, number, "Format OK" or "Check the number",
   how many times it was seen, and every time (the camera's own clock). It updates by itself.
3. **Live view** (optional add-on): the camera's picture in the browser. Off until you give the camera's
   login on the Live page. It runs only while someone is watching, uses the camera's small second
   stream, and cannot slow down or stop plate reading (see *Live view* below).
4. **Motion log**: a small picture and the time for every movement in front of the camera, with the plate
   shown on the pictures that have one. Capped by total size (default 50 MB, set in Settings): when full,
   the **oldest pictures are deleted first**.
5. **Settings**: camera and network details, confidence and merge-window, retention, change password,
   CSV export, delete everything, restart / shut down.

One administrator account. No users, roles or cloud.

## Run it on a development PC (Docker)

```bash
cd plate-box
ADVERTISE_IP=<this PC's address on the camera's network> docker compose -f dev/docker-compose.yml up -d
docker compose -f dev/docker-compose.yml logs | grep "Setup code"
```

Open <http://localhost:8088>. The camera receiver listens on port 2222. Wi-Fi and the setup hotspot
do not exist in a container (a canned network list stands in), and camera discovery only sees the
container's own network, so type the camera's address at that step.

Tests (55, no camera needed):

```bash
docker run --rm -v "$PWD:/opt/platebox:ro" -w /opt/platebox platebox-dev python -m pytest -q tests
```

## Run it on a server (a VPS), for cameras that send over the internet

For a camera that has no computer next to it: it sends its pictures over the internet (for example through the 4G
gateway in `firmware/gateway`), the server reads the plates, and the page is opened from anywhere.

```
 IP camera ──► 4G gateway ──(SFTP over the internet, port 2222)──► PlateBox on the server ──► https://your-name
```

You need a server with Docker, and a name that points at it (a DNS "A" record), for example `plates.example.com`.
Open ports 80, 443 and 2222 in its firewall.

```bash
cd plate-box
cp server/.env.example server/.env          # put the server's name in it
docker compose --env-file server/.env -f server/docker-compose.yml up -d --build
docker compose -f server/docker-compose.yml logs platebox | grep "Setup code"
```

Open `https://<your name>`. The wizard is shorter than on a box: **password → camera settings → done**. Enter the
settings it shows on the camera (Type SFTP, the server's name, port 2222, user name, password, folder), press Save and
then test on the camera; the page says so as soon as the camera connects and the first picture arrives.

What is different from a box at the site (`PLATEBOX_SERVER=1` switches all of it):

| | Box at the site | Server |
|---|---|---|
| Wizard | password, network, find the camera, connect | password, connect |
| Camera's pictures | SFTP on the local network | SFTP over the internet, received by the program itself |
| Page | `http://` on the local network | `https://` (Caddy gets and renews the certificate) |
| Live view, watching the video directly | yes | no: the camera is behind the site's connection and cannot be reached |
| Address guard, DNS/time answering, Wi-Fi, restart buttons | yes | off and hidden |

Things to know:

* **The camera's port is on the internet.** It accepts one account with a made-up 12-character password, only SFTP,
  only one folder, no shell, and it slows down wrong passwords. Keep the password long; change it in Settings if it
  may have leaked. If the camera's connection has a fixed public address, also limit port 2222 to it in the firewall.
* **Picture size decides the delay.** At a measured 1.3 Mbit/s of 4G upload a 200 KB picture takes about a second and
  a 1 MB picture about six. Set the camera's snapshot size as small as keeps the plate readable.
* **Re-sent old pictures.** The rule that ignores pictures much older than the camera's own clock needs to ask the
  camera its time, which a server cannot do. A picture with exactly the same content as one already read is still
  ignored.
* The data (plates, pictures, passwords) is in the Docker volume `platebox_data`. Back that up.

Without Docker, for a test on any computer: `PLATEBOX_SERVER=1 PLATEBOX_PUBLIC_HOST=<name> PLATEBOX_PORT=8090 python -m platebox`.

## Gateways and firmware over the air

A gateway (`firmware/gateway`: ESP32-S3, W5500, 4G modem, GNSS) is the 4G box between a camera and this server.

1. **Gateways → Add a gateway.** The page shows a token once. On the gateway's console type
   `server host <this server's name>` and `server token <the token>`.
2. The gateway signs in over HTTPS every two minutes (`ota check` does it at once): firmware version, signal,
   position. The page shows each one as **Live** or **Offline** with when it was last heard, and refreshes itself.
3. **Gateways → Firmware → Upload** the new `build/ba-gateway.bin`. Its name and version are read from the file
   (raise `PROJECT_VER` in the gateway's `CMakeLists.txt` for every build; the same version is refused twice).
4. At its next sign-in each gateway downloads it into the spare half of its flash, checks the SHA-256 the server
   gave, and restarts into it. The new firmware is **on trial**: it must sign in once to be kept. If it cannot within
   15 minutes, the gateway restarts into the firmware it had before.

The gateway API (`/device/v1/checkin`, `/device/v1/firmware/<id>`, `/device/v1/positions`) accepts only a gateway's
token, and slows down wrong ones. Firmware is not signed: what protects it is HTTPS to your own server, the token and
the checksum. Whoever can sign in to this page can put firmware on your gateways, so keep that password strong.

## Bus mode: passengers, distance and fare

**Settings → What this records → Bus passengers.** One mode at a time: every picture the camera sends is then read
for faces instead of number plates, and the gateway is told at its next sign-in and reports its position every 5 s.

```
 door camera ──► gateway ──► server:  face at the door ──► nobody like it on board  → a ride starts (position A)
                    └ position every 5 s                 ──► like somebody on board   → that ride ends  (position B)
                                                              distance along the reported route A→B → fare
```

* **Passengers page:** on board now, rides finished, distance and fares for the last 24 hours; for each ride the face
  at the entry and at the exit, both positions (linked to a map), kilometres, minutes, fare and how alike the two faces
  were. CSV export. *Last stop: everybody off* closes every open ride where the bus is.
* **Fare** = starting fare + fare per kilometre × distance, rounded. Both are in Settings.
* **Two doors:** tell each camera whether it is the entry or the exit door (Passengers → Doors). The entry camera then
  only starts rides and the exit camera only ends them.
* **Late pictures:** a picture is placed on the route by the camera's own clock (from its file name), not by when it
  arrived, so a gap in coverage does not move it. Keep the camera's clock right and set its offset from UTC in Settings.
* **Models:** OpenCV's YuNet (finds faces) and SFace (compares them), on the CPU. The server image downloads both when
  it is built. Elsewhere put the two `.onnx` files in a folder and set `PLATEBOX_FACE_MODELS` to it.

What it cannot do, and what to check before relying on it:

* **It makes mistakes.** Two strangers can look alike to it (one ends the other's ride), and a person can be missed on
  the way out (scarf, mask, looking away, a crowd at the door): that ride is closed as *exit not seen* after the longest
  ride time, with no fare. The *match* number on each ride and the threshold in Settings are there to tune this on your
  own buses. It has not been measured on real passengers yet.
* **It does not know who a person is**, so by itself it cannot charge anybody. A fare needs something that identifies
  the payer (a card, a QR ticket, an account) tied to the ride.
* **Faces are personal data.** The numbers describing a face are kept only while the person is on board and deleted when
  the ride ends; the pictures are kept for the retention time in Settings (30 days by default). Passengers must be told,
  and in India the Digital Personal Data Protection Act applies: take legal advice before a public roll-out.
* **No position, no fare.** The gateway's satellite antenna needs a view of the sky.

## Install it on a Windows computer (one Setup.exe)

`PlateBox-Setup-<version>.exe` contains everything: Python, every library, the plate models, the camera
receiver and ffmpeg for the live view. Nothing else needs installing and no internet is needed.

* Run it as administrator (it asks). It installs to `C:\Program Files\PlateBox`, keeps its data in
  `C:\ProgramData\PlateBox` (locked to administrators: it holds passwords), allows itself through the firewall for
  the local network only, and starts with the computer (a scheduled task running as SYSTEM, restarted if it stops).
* The setup code is in `C:\ProgramData\PlateBox\setup-code.txt`. The Start-menu shortcut opens the page.
* The camera's pictures are received by a receiver **inside the program** (no sshd, no extra service): SFTP on port
  2222, one account, password only, confined to one folder, no shell. Web page on port 80, or 8080 if 80 is taken.
* Wi-Fi: the wizard lists networks and joins one with Windows' own `netsh`. There is no setup hotspot on Windows.
* Uninstalling asks whether to keep the saved plates; a silent uninstall keeps them.

To build the installer yourself (about 10 minutes, ~1.5 GB of working space, internet the first time):

```powershell
powershell -ExecutionPolicy Bypass -File plate-box\windows\build.ps1 -FfmpegDir C:\path\to\ffmpeg\bin
```

It needs 64-bit Python 3.11 and [Inno Setup 6](https://jrsoftware.org/isinfo.php). Without `-FfmpegDir` the installer is
built without the live view. The build packages the program, **starts the packaged program and checks that it
serves its pages**, and only then makes the installer. The installer is not code-signed, so Windows SmartScreen
will warn on first run ("More info", "Run anyway"); signing needs a code-signing certificate.

## Install it on a box (Raspberry Pi 5 or a mini PC)

Raspberry Pi OS Bookworm 64-bit (or Debian 12 / Ubuntu 22.04+), with NetworkManager (the Pi OS default):

```bash
git clone <this repository> && cd <repository>/plate-box
sudo sh deploy/install.sh
```

It needs the internet once. It installs the app and its models, creates a locked-down SFTP receiver
for the camera, sets the name `platebox.local`, and prints the **setup code**. The same code and the
box's own setup Wi-Fi name and password are written to `platebox-label.txt` on the boot partition,
for the label.

**No network at all?** After 45 seconds the box raises its own Wi-Fi (`PlateBox-XXXX`, password on the
label). Join it with a phone, open <http://10.42.0.1>, and the wizard takes over.

### Moving the box to another site

1. Plug it in. If it cannot find a network it raises its setup Wi-Fi: join it, open the page,
   choose the site's Wi-Fi (Settings → *Change the Wi-Fi*).
2. Find the camera again if it moved, and **update only the camera's Server address** to the one the
   page shows. The SFTP user name, password and folder never change, so that is the only edit.
   If you forget, the page shows a red banner: "The box's address changed".

Tip: give the box a fixed address (a DHCP reservation in the site router) and this step disappears.

## How it is built

| Piece | What | Where |
|---|---|---|
| Web + wizard | FastAPI, server-rendered pages, a little JavaScript | `platebox/web.py`, `templates/`, `static/` |
| Login | One admin; scrypt password hash; signed `HttpOnly` `SameSite=Strict` cookie; attempt throttling | `platebox/auth.py` |
| Camera receiver | A **separate** `sshd` on port 2222: one account (`ftpcam`), password only, chrooted, SFTP only, no shell, no forwarding | `deploy/sshd_config` |
| Intake | Watches the receiver's folder; settled `.jpg` → plate reader → sighting → file deleted | `platebox/intake.py` |
| Reader | `fast-alpr` (ONNX plate detector + OCR), CPU only, models cached at install | `platebox/reader.py` |
| Sightings | Merges the pictures of one pass, keeps the best reading, lists every later pass | `platebox/sightings.py` |
| Network | `nmcli` wrapper (list/join Wi-Fi, hotspot), fake backend for tests | `platebox/netmgr.py`, `system.py` |
| Camera discovery | ONVIF WS-Discovery, then a sweep for RTSP/web | `platebox/discovery.py` |
| Storage | SQLite (WAL) + JPEG files; 90-day retention by default | `platebox/db.py` |

Design rules that matter:

* **A picture without a plate is never kept.** Only pictures with a recorded plate are stored (full
  picture + a small crop). Everything else the camera sends is deleted as soon as it has been read.
* **Only things that look like a registration are recorded as plates** (Settings, on by default). The
  detector also finds plate-shaped *text*, above all the camera's own on-screen name and clock, which are on
  every picture; none of that looks like an Indian registration, so it is ignored. Untick the setting for
  foreign or unusual plates. A button removes entries recorded before the rule existed.
* **One pass = one sighting.** A vehicle gives several pictures a second apart and the reader may
  guess differently on each; the best reading wins (a valid Indian format beats an invalid one, then
  higher confidence). Two *different valid* registrations are always two vehicles, however alike.
* **The camera's credentials do not depend on the network.** The user name, password and folder are
  fixed for the life of the box.
* **The web app is not root.** It may ask root for exactly two things, through `sudo`: set the
  camera account's password, and restart / shut down.

## Motion log

`/motion` lists, newest first, every picture kept as a record of movement, whether or not a plate was in it.

* **Reduced copies.** Each is 800 px wide (JPEG 70), about 35-60 KB, so 50 MB holds roughly 800-1,500 pictures.
  The full-size picture is kept only for plates, separately (Plates screen).
* **Throttled.** The camera sends about a picture a second while anything moves; at most one is logged per
  *N* seconds (default 3). A picture with a plate in it is always logged, and tagged with the plate.
* **Size cap, oldest first.** After every new picture the total is checked against the limit
  (Settings, 5 MB to 5000 MB). Over it, the oldest pictures are deleted, down to 90% of the limit, so it does
  not delete on every picture. Lowering the limit trims immediately. Files and rows are removed together.
* **Never in the way.** If the log fails (disk full, bad picture) the plate is still read and recorded.

## Live view (add-on)

`/live` shows the camera's picture. A browser cannot play the camera's stream (RTSP over TLS, H.265),
so **ffmpeg** (installed by `install.sh`) turns the camera's *second, smaller stream* (720p) into JPEG
pictures that any browser shows in an `<img>`.

It is built so it cannot disturb the real job:

* **Off by default.** Give the camera's user name and password on the Live page (a view-only camera
  account is best). They are stored on the box only and never shown again.
* **Only while watched.** The first viewer starts ffmpeg; up to 3 viewers share it; it stops about 8 seconds
  after the last one leaves (closing the tab, pausing, or going to another page all count).
* **Separate and low priority.** ffmpeg is its own process (`nice 10`); the plate reader never waits on it,
  and any failure in it is reported on the Live page instead of reaching the intake.
* Lighter than the plate pictures: about 1 s of CPU per 7 s watched on a PC; tune with `live_fps`/`live_width`.

## Configuration

Environment variables (all optional; the defaults suit the box):

| Variable | Default | Meaning |
|---|---|---|
| `PLATEBOX_DATA` | `/var/lib/platebox` | Database, pictures, secrets |
| `PLATEBOX_SFTP_ROOT` | `/srv/platebox-sftp` | The receiver's chroot (root-owned, as OpenSSH requires) |
| `PLATEBOX_PORT` | `80` | Web port |
| `PLATEBOX_SFTP_PORT` | `2222` | Port shown to the user for the camera |
| `PB_DETECTOR`, `PB_OCR` | `yolo-v9-t-384-license-plate-end2end`, `cct-s-v2-global-model` | Reading models |
| `PLATEBOX_FAKE_NET` | off | No NetworkManager (development) |
| `PLATEBOX_ADVERTISE_IP` | none | Development: the address to tell the camera |
| `PLATEBOX_FFMPEG` | `ffmpeg` on the path | Where ffmpeg is, for the live view |

Runtime settings (confidence floor, merge window, retention) are on the Settings page.

## What has and has not been verified

Verified, by automated tests and by a run against a real CP Plus camera on a LAN:

* Claiming, login, throttling, same-origin check, the whole wizard, plates screen, settings (31 tests).
* The SFTP receiver, with a standard client: right password uploads; wrong password, a shell, and port
  forwarding are refused; the account sees only its upload folder.
* ONVIF discovery found the camera and named it; the intake read real plate pictures end to end.

**Not yet verified on real hardware** (be aware before shipping a unit):

* Listing/joining Wi-Fi and the setup hotspot: written against `nmcli`, tested only with the fake
  backend. Needs one run on a Raspberry Pi.
* ARM (Raspberry Pi) wheels for `onnxruntime`/OpenCV and reading speed on a Pi 5.
* The camera's SFTP client against the receiver's key types, on the box itself.
* `install.sh` has been run in a clean Debian 12 container with services skipped, not yet on a Pi.

## Limits to know

* Reading accuracy depends on how many pixels wide the plate is: about 400 or more reads well, under
  about 200 is unreliable. Each card shows the plate's width in pixels so you can judge a mounting.
* The web page is plain HTTP on the local network (a certificate for a name like `platebox.local`
  cannot be obtained publicly). Use it on a network you trust, and change the camera's own default password.
* A Raspberry Pi has no clock battery. Without internet its own time can be wrong after a power cut;
  the times shown are the **camera's** clock (printed in each file name), which the camera keeps.
