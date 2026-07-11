from davis_clientraw.davis_decode import crc16_ccitt, decode_packet, is_valid_packet

TEMP_PACKET = [0x80, 0x04, 0x70, 0x0F, 0x99, 0x00, 0x91, 0x11]
HUMIDITY_PACKET = [0xA0, 0x06, 0x52, 0x83, 0x38, 0x00, 0x5A, 0xC8]


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
