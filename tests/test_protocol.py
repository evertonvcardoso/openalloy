# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
from openalloy.infra import protocol as P


def test_buffer_positions_are_unique_and_in_range():
    seen = set()
    for idx in range(94):
        pos = P.buffer_pos(idx)
        for plane in (0, 16, 32):
            assert 0 <= pos + plane < P.BUFLEN
            assert pos + plane not in seen
            seen.add(pos + plane)


def test_buffer_pos_matches_openrgb_formula():
    # OpenRGB: segment = (i/12)*48, sector = ((i/6)&1)*8, sequence = i%6
    for i in range(94):
        assert P.buffer_pos(i) == (i // 12) * 48 + ((i // 6) & 1) * 8 + (i % 6)


def test_pack_is_grb_planar_and_roundtrips():
    colors = [(i, (i * 3) % 256, 255 - i) for i in range(94)]
    buf = P.pack_colors(colors)
    assert len(buf) == 380
    pos = P.buffer_pos(7)
    assert (buf[pos], buf[pos + 16], buf[pos + 32]) == (colors[7][1], colors[7][0], colors[7][2])
    assert P.unpack_colors(buf) == colors


def test_stream_packets_shape():
    pkts = P.stream_packets(bytes(range(256)) + bytes(124))
    assert [len(p) for p in pkts] == [65] * 6 + [25]
    for n, p in enumerate(pkts):
        assert p[0] == 0 and p[1] == P.CMD_STREAM and p[2] == n
    assert pkts[-1][4] == 20


def test_frame_header():
    f = P.frame(0x95, 1, 2, 1, b"\x03")
    assert len(f) == 65
    assert list(f[:6]) == [0, 0x95, 1, 2, 1, 3]


def test_anim_frame_packets_encode_profile_chunk_and_frame():
    pkts = P.anim_frame_packets(3, 17, bytes(380))
    assert len(pkts) == 7
    assert [p[2] for p in pkts] == [(3 << 4) | n for n in range(7)]
    assert all(p[3] == 17 and p[1] == P.CMD_ANIM_FRAME for p in pkts)


def test_config_block_matches_ngenuity_layout():
    cfg = P.config_block(100, (1, 2, 3))
    assert len(cfg) == 0x17
    assert cfg[0:2] == b"\x09\x18" and cfg[10] == 100 and cfg[12] == 4 and cfg[15] == 1
    assert cfg[16:19] == b"\x01\x02\x03"


def test_status_parsing():
    ok = bytes([0x95, 0, 0, 2, 0xEC, 0xAC]) + bytes(58)
    bad = bytes([0x22, 0, 0, 2, 0xEC, 0x01]) + bytes(58)
    data = bytes([0x22, 0, 0, 0, 0xEC, 0xAC]) + bytes(58)   # length 0 => data
    assert P.status_of(ok) == "ok"
    assert P.status_of(bad) == "bad-parameters"
    assert P.status_of(data) is None
    assert P.status_of(None) == "silent"


def test_parse_info():
    rep = bytearray(64)
    rep[0], rep[4], rep[5] = 0x10, 0x21, 0x08
    rep[8:8 + 9] = b"HyperX AO"
    rep[56] = 0x10
    info = P.parse_info(bytes(rep))
    assert info == {"firmware": "21.08", "name": "HyperX AO", "variant_byte": 0x10}


def test_only_exact_ids_supported():
    assert (0x0951, 0x16E6) in P.SUPPORTED_IDS and (0x03F0, 0x098F) in P.SUPPORTED_IDS
    assert len(P.SUPPORTED_IDS) == 2
