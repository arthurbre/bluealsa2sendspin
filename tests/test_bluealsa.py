import pytest

from bluealsa2sendspin.bluealsa import (
    PCM_INTERFACE,
    PcmFormat,
    UnsupportedPcmFormatError,
    _select_source_pcm,
)


def test_decode_unpacks_bit_fields() -> None:
    fmt = PcmFormat.decode(0x4210, rate=44100, channels=2)
    assert (fmt.signed, fmt.big_endian, fmt.byte_width, fmt.bit_depth) == (False, True, 2, 16)

    fmt = PcmFormat.decode(0x8418, rate=44100, channels=2)
    assert (fmt.signed, fmt.big_endian, fmt.byte_width, fmt.bit_depth) == (True, False, 4, 24)


def test_require_supported_rejects_big_endian_or_unsigned() -> None:
    with pytest.raises(UnsupportedPcmFormatError):
        PcmFormat.decode(0x4210, rate=44100, channels=2).require_supported()


def test_require_supported_rejects_padded_layouts_outside_the_known_set() -> None:
    # signed, little-endian, 24-bit samples padded into 4 bytes: not one of the
    # layouts bluealsa2sendspin knows how to feed onward unmodified.
    with pytest.raises(UnsupportedPcmFormatError):
        PcmFormat.decode(0x8418, rate=44100, channels=2).require_supported()


def test_require_supported_accepts_signed_little_endian_16bit() -> None:
    fmt = PcmFormat.decode(0x8210, rate=44100, channels=2)
    fmt.require_supported()
    assert fmt.frame_bytes == 4


def _pcm_object(*, mode: str, sequence: int, running: bool) -> dict[str, dict[str, object]]:
    return {
        PCM_INTERFACE: {
            "Transport": "A2DP-sink",
            "Mode": mode,
            "Format": 0x8210,
            "Channels": 2,
            "Rate": 44100,
            "Device": "/org/bluez/hci0/dev_AA",
            "Sequence": sequence,
            "Running": running,
        }
    }


def test_select_source_pcm_ignores_sink_mode_and_other_transports() -> None:
    managed = {
        "/org/bluealsa/hci0/dev_AA/a2dpsnk/source": _pcm_object(
            mode="source", sequence=1, running=True
        ),
        "/org/bluealsa/hci0/dev_AA/a2dpsnk/sink": _pcm_object(
            mode="sink", sequence=1, running=False
        ),
    }
    info = _select_source_pcm(managed)
    assert info is not None
    assert info.object_path == "/org/bluealsa/hci0/dev_AA/a2dpsnk/source"
    assert info.running is True
    assert info.pcm_format.sample_rate == 44100


def test_select_source_pcm_returns_none_when_absent() -> None:
    assert _select_source_pcm({}) is None


def test_select_source_pcm_picks_the_highest_sequence_when_several_are_present() -> None:
    managed = {
        "/org/bluealsa/hci0/dev_AA/a2dpsnk/source": _pcm_object(
            mode="source", sequence=1, running=False
        ),
        "/org/bluealsa/hci0/dev_BB/a2dpsnk/source": _pcm_object(
            mode="source", sequence=2, running=True
        ),
    }
    info = _select_source_pcm(managed)
    assert info is not None
    assert info.object_path == "/org/bluealsa/hci0/dev_BB/a2dpsnk/source"
