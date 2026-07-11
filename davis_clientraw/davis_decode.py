"""Davis ISS 8-byte packet CRC and field decode."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

CRC_POLY = 0x1021


def crc16_ccitt(data: bytes, crc: int = 0x0000) -> int:
    for byte in data:
        crc ^= byte << 8
        crc &= 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ CRC_POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def is_valid_packet(packet: list[int]) -> bool:
    if len(packet) != 8:
        return False
    return crc16_ccitt(bytes(packet)) == 0


@dataclass
class DecodedPacket:
    transmitter_id: int
    battery_low: bool
    message_type: int
    wind_speed_mph: int
    wind_dir_deg: Optional[float]
    temp_f: Optional[float] = None
    humidity_pct: Optional[float] = None
    wind_gust_mph: Optional[int] = None
    rain_count: Optional[int] = None
    rain_rate_mm_h: Optional[float] = None
    solar_wm2: Optional[float] = None
    uv_index: Optional[float] = None


def _signed16(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


def decode_wind_dir(byte2: int) -> float:
    if byte2 == 0:
        return 360.0
    return round(9 + byte2 * 342 / 255)


def decode_packet(packet: list[int]) -> DecodedPacket:
    if len(packet) != 8:
        raise ValueError(f"packet must be 8 bytes, got {len(packet)}")
    b0, b1, b2, b3, b4 = packet[0], packet[1], packet[2], packet[3], packet[4]

    message_type = b0 >> 4
    transmitter_id = b0 & 0x07
    battery_low = bool(b0 & 0x08)
    wind_speed_mph = b1
    wind_dir_deg = decode_wind_dir(b2)

    decoded = DecodedPacket(
        transmitter_id=transmitter_id,
        battery_low=battery_low,
        message_type=message_type,
        wind_speed_mph=wind_speed_mph,
        wind_dir_deg=wind_dir_deg,
    )

    if message_type == 0x8:
        raw = (b3 << 8) | b4
        decoded.temp_f = _signed16(raw) / 160.0
    elif message_type == 0xA:
        decoded.humidity_pct = (((b4 >> 4) << 8) + b3) / 10.0
    elif message_type == 0x9:
        decoded.wind_gust_mph = b3
    elif message_type == 0xE:
        decoded.rain_count = b3 & 0x7F
    elif message_type == 0x5:
        if b3 == 0xFF:
            decoded.rain_rate_mm_h = 0.0
        else:
            heavy = b4 & 0x40
            num = ((b4 & 0x30) >> 4) * 250 + b3
            if num == 0:
                decoded.rain_rate_mm_h = 0.0
            else:
                decoded.rain_rate_mm_h = (11520 / num) if heavy else (720 / num)
    elif message_type == 0x6:
        if b3 != 0xFF:
            decoded.solar_wm2 = (((b3 << 8) + b4) >> 6) * 1.757936
    elif message_type == 0x4:
        if b3 != 0xFF:
            decoded.uv_index = (((b3 << 8) + b4) >> 6) / 50.0

    return decoded
