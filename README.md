# Helvar showroom control

Custom tablet control for a Helvar Imagine 950 showroom: ~39 display islands,
2–5 luminaires each. Replaces the SceneSet app with a grid of island
photographs — tap a photo, that island lights; tap again, it goes off.

| File | |
|---|---|
| `helvar_controller.py` | **The controller.** Photo grid, tap to toggle. |
| `helvar_mapper.py` | Works out which DALI address drives which luminaire. |
| `helvarnet.py` | Shared HelvarNet transport used by both. |

Standard library only — no pip install. Runs on a Raspberry Pi.

## The controller

```
python3 helvar_controller.py
```

First run creates `controller_config.json` and a `photos/` folder. Open
`http://<machine>:8080/` on the tablet.

It reads `islands.json`, which lists what each island owns. Islands can be
driven by **device address**, by **DALI group**, or both:

```json
{
  "router_ip": "192.168.1.50",
  "islands": [
    {"id": 1, "name": "Oak ceiling", "photo": "photos/island-1.jpg",
     "addresses": ["@1.1.1.1", "@1.1.1.2"]},
    {"id": 2, "name": "Brass pendants", "groups": [7], "on_level": 80}
  ]
}
```

Because addresses work directly, **no DALI groups need to exist and no
Designer work is required**. The mapping is a JSON file you can edit.

### What it does

- **Tap to toggle.** Tiles respond instantly — the state flips locally and is
  reconciled when the server confirms, so there's no lag on a tap.
- **All on / All off.** Opening and closing the showroom.
- **Solo.** When enabled, lighting one island turns every other island off.
  The showroom gesture: a customer asks about one fixture and you isolate it
  without tapping thirty-eight others off.
- **Shared state.** Every tablet polls the same server, so two tablets agree.
  State survives a restart.
- **Honest about problems.** An island with nothing mapped to it is greyed out
  and says so; if the router stops answering, a banner says taps are being
  recorded but the lights aren't moving.

### Photos

Drop them in `photos/` as `island-1.jpg`, `island-2.jpg`, … or set the `photo`
path per island. Islands without a photo show a numbered placeholder, so the
panel is usable before you've taken any.

**Resize them first.** 39 phone photos at full resolution is 200MB+ and will
feel sluggish. ~600px wide at 80% quality is ~60KB each:

```
mogrify -resize 600x -quality 80 photos/*.jpg        # ImageMagick
```

The controller sets a one-day cache header, so the tablet re-fetches them
rarely.

### Settings

| Key | |
|---|---|
| `on_level` | Level sent when an island turns on (0–100). Per-island `on_level` overrides it. |
| `fade` | Fade time in hundredths of a second. `100` = 1.0s. |
| `title` | Shown in the header. |
| `http_port` | Default 8080. |

`islands.json` wins over `controller_config.json` for the router address, so
the two files can't silently disagree about which router is being driven.

## The mapper

Only needed once, and only if you don't already know which address is which.

```
python3 helvar_mapper.py        # port 8080 by default - stop the controller first
```

1. **Tools → Start scan.** Probes every address on all four subnets, keeps the
   ones that answer. Turns ~256 possible addresses into your ~120 real ones.
2. **Islands → Create islands.** 39 in one go; rename any time.
3. **Walk.** One luminaire blinks, you tap the island it's on. It's assigned
   and the next unmapped address starts blinking. Progress saves after every
   tap, so you can stop and resume.
4. **Islands → Light.** Lights everything mapped to an island — the check that
   you got it right.
5. **Tools → Write islands.json.** Which the controller then reads.

`Steady on` instead of `Blink` helps in a bright showroom. `All off` before
you start makes a lit luminaire unambiguous.

## The command-number caveat

The HelvarNet command numbers in both config files follow the protocol
overview document, but are **not verified against real hardware**. If tapping
a tile does nothing, suspect these before the wiring. Check one by hand with
the mapper's raw-command box:

```
>V:1,C:14,L:100,F:0,@1.1.1.1#    full level on one luminaire
>V:1,C:13,G:7,L:100,F:0#         full level on group 7
>V:1,C:103,@1.1.1.1#             query device type
```

A reply starting `?` is an answer, `!` is an error. If a luminaire lights, the
addressing and command numbers are right.

## Raspberry Pi setup

Any Pi from the 3B+ onward — it needs both ethernet and wifi. Not the Zero 2 W
(no ethernet).

- **eth0** → static IP on the 950's subnet
- **wlan0** → office wifi, default route
- **Do not enable IP forwarding.** The Pi is an application gateway, not a
  router: the tablet reaches only the web server, and only this process opens
  a socket to port 50000. The lighting network stays segregated.

Run it as a systemd service so it comes back after a power cut:

```ini
[Unit]
Description=Helvar showroom controller
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/helvar/helvar_controller.py
WorkingDirectory=/home/pi/helvar
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

Boot from a USB SSD rather than a microSD if you can — cheap cards are the
usual failure on a 24/7 Pi.

### Security

HelvarNet has no authentication: anyone who can open a socket to port 50000
controls the lighting. Helvar's own guidance is to run Imagine on a segregated
network with no internet access. Keep this off guest wifi — that, not a
password on the web page, is what protects it.

### Tablet

Chrome bookmarked to the home screen runs fullscreen. For a permanent wall
tablet use Fully Kiosk Browser to lock it to the page and keep the screen
awake. Give the Pi a static IP so the bookmark never breaks.

## State files

`controller_config.json`, `controller_state.json`, `mapper_config.json`,
`mapper_state.json` and `islands.json` are generated at runtime and gitignored
— they hold your router's IP and your site's mapping.

## Testing

Both tools were verified against a mock HelvarNet router and driven in
Chromium at tablet and phone widths. Not yet tested against real hardware.
