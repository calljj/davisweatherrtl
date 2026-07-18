from davis_clientraw.source_rtldavis import DATA_LINE_RE, ReceivedPacket


def _parse_line(line: str) -> ReceivedPacket:
    match = DATA_LINE_RE.match(line)
    assert match is not None, f"line did not match: {line!r}"
    data_hex, repeated_str, repeater_info_hex, _hypothesis = match.groups()
    data = [int(data_hex[i:i+2], 16) for i in range(0, 16, 2)]
    repeated = repeated_str == "true"
    repeater_info = None
    if repeated and repeater_info_hex:
        repeater_info = [int(repeater_info_hex[i:i+2], 16) for i in range(0, len(repeater_info_hex), 2)]
    return ReceivedPacket(data=data, repeated=repeated, repeater_info=repeater_info)


def test_direct_packet_line_parses():
    line = "11:23:45.123456 8004700F990091AB 12 34 56 78 9 msg.ID=1 Repeated=false RepeaterInfo=00 Hypothesis="
    pkt = _parse_line(line)
    assert pkt.data == [0x80, 0x04, 0x70, 0x0F, 0x99, 0x00, 0x91, 0xAB]
    assert pkt.repeated is False
    assert pkt.repeater_info is None


def test_direct_packet_line_without_repeater_suffix_still_parses():
    # Backward compatible with a plain (non-repeater-aware) rtldavis build.
    line = "11:23:45.123456 8004700F990091AB 12 34 56 78 9 msg.ID=1"
    pkt = _parse_line(line)
    assert pkt.data == [0x80, 0x04, 0x70, 0x0F, 0x99, 0x00, 0x91, 0xAB]
    assert pkt.repeated is False
    assert pkt.repeater_info is None


def test_repeated_packet_line_parses_with_repeater_info():
    line = (
        "13:54:07.656112 8108812DD9001572 2 0 0 0 0 msg.ID=1 "
        "undefined:[0 0 0 0 0 0 0 0] Repeated=true RepeaterInfo=0502 "
        "Hypothesis=reorder_crc_last_with_header"
    )
    pkt = _parse_line(line)
    assert pkt.data == [0x81, 0x08, 0x81, 0x2D, 0xD9, 0x00, 0x15, 0x72]
    assert pkt.repeated is True
    assert pkt.repeater_info == [0x05, 0x02]


def test_non_data_line_does_not_match():
    line = "13:54:07.656112 Init channels: wait max 21 seconds for a message of each transmitter"
    assert DATA_LINE_RE.match(line) is None
