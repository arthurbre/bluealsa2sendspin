# bluealsa2sendspin

Bridges a Bluetooth A2DP audio sink into [Music Assistant](https://music-assistant.io/)
as a [Sendspin](https://github.com/Sendspin/spec) **source** client — without going
through PipeWire or PulseAudio.

Status: early scaffolding, not yet functional.

## Why

Music Assistant 2.10 introduces Sendspin, its native sync protocol, along with a
`sendspin_source` plugin that exposes any connected Sendspin client implementing the
`source` role (line-in, turntable, microphone, Bluetooth receiver, ...) as a playable
audio source. The Sendspin protocol itself is transport-only: it doesn't care how a
source client captured its audio, so nothing ships out of the box for Bluetooth.

Capturing Bluetooth A2DP audio via PipeWire/WirePlumber works, but the source device
name (`bluez_input.<MAC>.N`) gets renumbered by WirePlumber across reconnects, making
`pactl list sources` unreliable for anything long-running and headless.

This project avoids that entirely by using [BlueALSA](https://github.com/arkq/bluez-alsa)
(`bluealsa`/`bluealsad`) as the A2DP sink instead. BlueALSA talks to BlueZ directly over
D-Bus and exposes the PCM stream on its own `org.bluealsa` D-Bus interface — no PipeWire
involved.

## Architecture

```
 Phone (A2DP source)
        │ Bluetooth
        ▼
   BlueZ + bluealsad  (A2DP sink profile)
        │ org.bluealsa D-Bus (PCM1.Open)
        ▼
 bluealsa2sendspin  (this project)
        │ Sendspin protocol (WebSocket, SOURCE role)
        ▼
 Music Assistant server  (sendspin + sendspin_source, both builtin)
```

Nothing on the Music Assistant server side needs to change: `sendspin` and
`sendspin_source` are already builtin providers. This project is a standalone client —
its only dependencies are [`aiosendspin`](https://pypi.org/project/aiosendspin/) (the
Sendspin protocol library) and BlueALSA. It can run on the same host as the MA server,
or on a separate device dedicated to Bluetooth reception.

Planned behavior:

- Open the BlueALSA PCM stream for the paired phone and feed it to a Sendspin
  `SourceCapture` (`aiosendspin.client`).
- Map the BlueALSA/BlueZ transport state (`idle` / `active`) to the Sendspin
  `line_sense` signal (`SignalState.PRESENT` / `ABSENT`), so `sendspin_source`'s
  built-in autostart/autostop (play when the phone starts streaming, stop after the
  configured silence timeout) works without extra logic on the MA side.
- Handle the Sendspin pairing handshake (PSK/PIN) with the MA server, separately from
  the phone's own Bluetooth pairing.

## Requirements

- Python 3.12+
- `bluez-alsa` (`bluealsa`/`bluealsad`) running and configured as an A2DP sink
- A Music Assistant server (2.10+) with the `sendspin_source` plugin enabled

## Development

Dependencies and the Python version are managed with [uv](https://docs.astral.sh/uv/),
which provisions its own interpreter and virtualenv — no reliance on whatever Python is
installed system-wide.

```console
uv sync --all-groups     # creates .venv, installs runtime + dev dependencies
uv run pre-commit install
```

- `uv run ruff check .` / `uv run ruff format .` — lint and format
- `uv run mypy src` — type check
- `uv run pre-commit run --all-files` — everything above, same as CI

CI (GitHub Actions) runs the same pre-commit hooks on every push and pull request.

## License

Apache License 2.0, see [LICENSE](LICENSE).
