# bluealsa2sendspin

Bridges a Bluetooth A2DP audio sink into [Music Assistant](https://music-assistant.io/)'s built-in Sendspin server (or any standalone Sendspin server),
as a [Sendspin](https://github.com/Sendspin/spec) **source** client — without going
through PipeWire or PulseAudio.

Status: core pairing/capture/streaming logic is implemented and verified end-to-end
against a real Sendspin server (see [Development](#development)). The BlueALSA/D-Bus
side is implemented against the official `org.bluealsa` interfaces and currently being exercised against real hardware.

## Why

Music Assistant 2.10 introduces major improvements in its implementation of Sendspin, its native sync protocol, along with a
`sendspin_source` plugin that exposes any connected Sendspin client implementing the
`source` role (line-in, turntable, microphone, Bluetooth receiver, ...) as a playable
audio source. The Sendspin protocol itself is transport-only: it doesn't care how a
source client captured its audio, so nothing ships out of the box for Bluetooth.

Capturing Bluetooth A2DP audio via PipeWire/WirePlumber works, but the source device
name (`bluez_input.<MAC>.N`) gets renumbered by WirePlumber across reconnects, making
`pactl list sources` unreliable for anything long-running and headless.

This project avoids that entirely by using [BlueALSA](https://github.com/arkq/bluez-alsa)
(`bluealsa`/`bluealsad`) directly as the A2DP sink instead. BlueALSA talks to BlueZ directly over
D-Bus and exposes the PCM stream on its own `org.bluealsa` D-Bus interface — no PipeWire
involved.

## Architecture

```
 A2DP source (e.g. phone)
        │ Bluetooth
        ▼
   BlueZ + bluealsad  (A2DP sink profile)
        │ org.bluealsa D-Bus (PCM1.Open)
        ▼
 bluealsa2sendspin  (this project)
        │ Sendspin protocol (WebSocket, SOURCE role)
        ▼
 Music Assistant server  (sendspin ;erver + sendspin_source, both builtin)
```

Nothing on the Music Assistant server side needs to change: `sendspin` and
`sendspin_source` are already builtin providers. This project is a standalone client —
its only dependencies are [`aiosendspin`](https://pypi.org/project/aiosendspin/) (the
Sendspin protocol library) and BlueALSA. It can run on the same host as the MA server,
or on a separate device dedicated to Bluetooth reception.

Behavior:

- Discovers the BlueALSA A2DP-sink "source" PCM for whichever phone is currently
  connected, opens its pipe over `org.bluealsa.PCM1.Open()`, and feeds it to a
  Sendspin `SourceCapture` (`aiosendspin.client`) once the server asks for it.
- Maps the PCM's `Running` property to the Sendspin `line_sense` signal
  (`SignalState.PRESENT` / `ABSENT`), so `sendspin_source`'s built-in
  autostart/autostop (play when the phone starts streaming, stop after the
  configured silence timeout) works without extra logic on the MA side.
- Handles the Sendspin pairing handshake (static PIN) with the MA server, separately
  from the phone's own Bluetooth pairing, and reconnects with exponential backoff if
  the connection to the MA server drops.

## Requirements

- Python 3.12+
- `bluez-alsa` (`bluealsa`/`bluealsad`) running and configured as an A2DP sink
- A Music Assistant server (2.10+) with the `sendspin_source` plugin enabled

## Usage

```console
uv tool install bluealsa2sendspin
```

Pair once with the Music Assistant server:

```console
bluealsa2sendspin pair --server-url ws://ma-host:8927/sendspin
```

This prints a client ID and an 8-digit PIN, and waits (up to 5 minutes) for pairing.
Add this source in Music Assistant (it should actually appear automatically, showing a message 'This player needs to be configured' or somethinng similar) and enter the PIN when prompted, then run the
bridge itself:

```console
bluealsa2sendspin run --server-url ws://ma-host:8927/sendspin
```

`run` connects to BlueALSA over D-Bus, bridges whichever phone is currently paired
as an A2DP source into the Sendspin connection, and keeps reconnecting to the MA
server if the connection drops. Identity and pairing state persist under
`$XDG_STATE_HOME/bluealsa2sendspin` (`~/.local/state/bluealsa2sendspin` by default);
override with `--state-dir`. See `bluealsa2sendspin --help` for all options.

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
- `uv run pytest` — unit tests plus an integration test that runs a real
  `aiosendspin` Sendspin server in-process (BlueALSA itself is faked; see
  `tests/test_integration.py`)
- `uv run pre-commit run --all-files` — lint/format/type-check, same as CI

CI (GitHub Actions) runs the same pre-commit hooks and the test suite on every push
and pull request.

## License

Apache License 2.0, see [LICENSE](LICENSE).
