from davis_clientraw.davis_decode import (
    crc16_ccitt,
    decode_packet,
    is_valid_packet,
    is_valid_repeated_packet,
)

TEMP_PACKET = [0x80, 0x04, 0x70, 0x0F, 0x99, 0x00, 0x91, 0x11]
HUMIDITY_PACKET = [0xA0, 0x06, 0x52, 0x83, 0x38, 0x00, 0x5A, 0xC8]

# Real packets captured 2026-07-18 from a live station-2-via-repeater-A
# signal (10ft range), confirmed against the Go implementation
# (repeaterHypotheses["reorder_crc_last_with_header"]) -- see
# vendor/rtldavis/protocol/protocol.go and README.md.
REPEATED_PACKETS = [
    ([0x81, 0x08, 0x81, 0x2D, 0xD9, 0x00, 0x15, 0x72], [0x05, 0x02]),
    ([0x51, 0x08, 0x86, 0xFF, 0x70, 0x00, 0x81, 0xB6], [0x05, 0x02]),
    ([0xE1, 0x06, 0x81, 0x17, 0x03, 0x00, 0x0B, 0x51], [0x85, 0x01]),
    ([0x41, 0x08, 0x7C, 0xFF, 0xC0, 0x00, 0x71, 0x96], [0x85, 0x01]),
]


def test_temperature_packet_crc_valid():
    assert is_valid_packet(TEMP_PACKET)
    assert crc16_ccitt(bytes(TEMP_PACKET)) == 0


def test_temperature_packet_decodes_to_25_0f():
    decoded = decode_packet(TEMP_PACKET)
    assert decoded.message_type == 0x8
    assert round(decoded.temp_f, 1) == 25.0


def test_humidity_packet_crc_valid():
    assert is_valid_packet(HUMIDITY_PACKET)
    assert crc16_ccitt(bytes(HUMIDITY_PACKET)) == 0


def test_humidity_packet_decodes_to_89_9_pct():
    decoded = decode_packet(HUMIDITY_PACKET)
    assert decoded.message_type == 0xA
    assert round(decoded.humidity_pct, 1) == 89.9


def test_invalid_packet_fails_crc():
    corrupt = TEMP_PACKET[:]
    corrupt[3] ^= 0xFF
    assert not is_valid_packet(corrupt)


def test_wind_dir_zero_maps_to_360():
    from davis_clientraw.davis_decode import decode_wind_dir
    assert decode_wind_dir(0) == 360.0


def test_repeated_packets_fail_classic_crc():
    # This is exactly what makes repeater packets invisible to a decoder
    # that only implements the classic formula -- confirmed real packets,
    # rejected by is_valid_packet.
    for packet, _repeater_info in REPEATED_PACKETS:
        assert not is_valid_packet(packet)


def test_repeated_packets_pass_repeater_crc():
    for packet, repeater_info in REPEATED_PACKETS:
        assert is_valid_repeated_packet(packet, repeater_info)


def test_repeated_packet_decodes_to_sane_values():
    # 0x81 0x08 0x81 0x2D 0xD9 0x00 0x15 0x72, message_type=8 (temp)
    packet, _ = REPEATED_PACKETS[0]
    decoded = decode_packet(packet)
    assert decoded.transmitter_id == 1  # Davis console "station 2"
    assert decoded.message_type == 0x8
    assert 0 <= decoded.wind_dir_deg <= 360
    assert decoded.temp_f is not None and 50 < decoded.temp_f < 100


def test_repeater_crc_rejects_mismatched_repeater_info():
    packet, _ = REPEATED_PACKETS[0]
    assert not is_valid_repeated_packet(packet, [0x00, 0x00])


def test_repeater_crc_requires_exact_lengths():
    packet, repeater_info = REPEATED_PACKETS[0]
    assert not is_valid_repeated_packet(packet[:7], repeater_info)
    assert not is_valid_repeated_packet(packet, repeater_info[:1])
