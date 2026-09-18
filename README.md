# Helvar showroom tools

Custom control for a Helvar Imagine 950 showroom: ~39 display islands,
2–5 luminaires each, driven from a tablet.

**Step one is `helvar_mapper.py`** — it works out which DALI address drives
which physical luminaire. Nothing else can be built until that mapping exists.

## Why the mapper comes first

There are no DALI groups configured and no Designer project to hand, so
nobody has told you that `@1.1.2.15` is the third luminaire on Island 12.
The mapper blinks one address at a time while you walk the floor with the
tablet, and you tap the island it belongs to. It writes `islands.json`,
which the scene panel will later read.

Because it drives devices directly by address, **no Designer work and no DALI
groups are needed** — the island→address map lives in a JSON file you can edit.

## Running it

```
python3 helvar_mapper.py
```

No dependencies beyond the standard library. First run creates
`mapper_config.json`; set `router_ip` and restart. Then open
`http://<machine>:8080/` on the tablet.

### Configuration

| Key | Meaning |
|---|---|
| `router_ip` / `router_port` | The 950. Port 50000 is fixed by HelvarNet. |
| `cluster` / `router` | First two fields of a device address. |
| `subnets` | `[1,2,3,4]` — the 950 has four DALI subnets. |
| `max_device_address` | Highest address probed per subnet (64). |
| `cmd_*` | HelvarNet command numbers — see the caveat below. |

### The command-number caveat

The command numbers in `mapper_config.json` follow the HelvarNet overview
document, but they are **not verified against a real router**. If a scan
finds nothing, suspect a wrong query number before you suspect the wiring.
Use the raw-command box on the Tools tab to check one by hand:

```
>V:1,C:103,@1.1.1.1#     query device type
>V:1,C:14,L:100,F:0,@1.1.1.1#   full level on one luminaire
```

A reply starting `?` is an answer; `!` is an error.

## Workflow

1. **Tools → Start scan.** Probes every address on every subnet and keeps the
   ones that answer, reading their descriptions where the system has them.
   Reduces ~256 possible addresses to the ~120 real ones.
2. **Islands → Create islands.** Make 39 in one go, rename them any time.
3. **Walk.** One address blinks; tap the island it belongs to. It's assigned
   and the next unmapped address starts blinking. `Skip` parks an address you
   can't place; `Back` returns to the previous one. Progress is saved after
   every tap, so you can stop and resume.
4. **Islands → Light.** Lights everything mapped to an island — the check that
   you got it right.
5. **Tools → Write islands.json.**

`Steady on` instead of `Blink` is useful in a bright showroom where a pulse is
hard to spot. `All off` kills every known device — worth pressing before you
start so a lit luminaire is unambiguous.

## Raspberry Pi setup

Any Pi from the 3B+ onward (both ethernet and wifi). Not the Zero 2 W — no
ethernet.

- **eth0** → static IP on the 950's subnet
- **wlan0** → office wifi, default route
- **Do not enable IP forwarding.** The Pi is an application gateway, not a
  router: the tablet reaches only the web server, and only this process opens
  a socket to port 50000. The lighting network stays segregated.

HelvarNet has no authentication — anyone who can open a socket to port 50000
controls the lighting. Keep this off guest wifi.

## Files

| File | |
|---|---|
| `helvar_mapper.py` | The tool. Single file, stdlib only. |
| `mapper_config.json` | Created on first run. Router IP and command numbers. |
| `mapper_state.json` | Scan results and mapping. Written after every action. |
| `islands.json` | The export. Input to the scene panel. |

`islands.json` looks like this:

```json
{
  "router_ip": "192.168.1.50",
  "islands": [
    {
      "id": 1,
      "name": "Oak ceiling",
      "photo": "photos/island-1.jpg",
      "addresses": ["@1.1.1.1", "@1.1.1.2"]
    }
  ]
}
```

The `photo` paths are placeholders the panel will use later; nothing reads
them yet.

## Not built yet

The photo-tile panel — the grid of island photographs where a tap toggles the
island on and off. It reads `islands.json` and needs the mapping to exist
first.
