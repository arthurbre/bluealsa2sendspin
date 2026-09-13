# bluealsa2sendspin

Bridges a BlueALSA A2DP-sink source PCM stream (audio arriving from a paired phone) into a Sendspin SOURCE-role client.

## Language

**Read/feed chunk**:
The single PCM slice `_READ_CHUNK_MS` sizes in `bridge.py`'s `_feed_loop`: one `readexactly()` off the BlueALSA source PCM pipe, handed to `capture.feed()` in one call. Read granularity and feed/delivery granularity are the same value today — there is no separate accumulation or pacing step between them.
_Avoid_: buffer, tampon — this repo has no jitter/smoothing buffer of its own; conflating the read/feed chunk with one caused real confusion when tuning it (see the 2026-09 stuttering investigation).

**Playback/jitter buffer**:
The lookahead the *receiving* Sendspin server (Music Assistant, via aiosendspin's server role) holds before playout, absorbing uneven or bursty arrival. Owned entirely by the server side; bluealsa2sendspin has no equivalent of its own and cannot configure it.
_Avoid_: buffer (without saying which side) — always say "server-side" or "playback/jitter" to distinguish it from the read/feed chunk above.
