"""A minimal FIT ENCODER for synthetic test fixtures.

WHY A REAL FILE AND NOT A MOCKED READER. Other tests mock
`fit_file.read_messages` and hand `build_rows` a dict. That proves the
trimming logic, and it cannot prove the LOOP: a rejection that names a
command, a command that resolves a raw file by its Garmin id and hashes it,
and a re-sync that walks the raw directory. Those need bytes on disk that
fitdecode parses exactly as it parses a watch file -- so this writes them.

WHAT IT WRITES. A FIT 2.0 file: the 14-byte header with its CRC, one
definition message per message type, the data messages, and the trailing
file CRC. Only the messages and fields ingest reads are encoded: file_id,
record, split (the watch's rwd_* run/walk/stand segmentation), lap, session
and activity. Field numbers, base types, scales and offsets are the FIT
profile's, so fitdecode decodes them to the same names and units it would
give a real file.

NOTHING HERE IS ANYONE'S DATA. The position is an arbitrary point chosen by
the caller, the clock is a fixed synthetic date, and the "device" is a model
id from the public FIT profile. Files are generated at test time inside a
TemporaryDirectory and never committed.
"""

from __future__ import annotations

import datetime as dt
import struct
from pathlib import Path

FIT_EPOCH = dt.datetime(1989, 12, 31, tzinfo=dt.UTC)
SEMICIRCLES_PER_DEGREE = 2**31 / 180.0

# FIT base types: (type byte, struct code, invalid value).
ENUM = (0x00, "B", 0xFF)
UINT8 = (0x02, "B", 0xFF)
UINT16 = (0x84, "H", 0xFFFF)
SINT32 = (0x85, "i", 0x7FFFFFFF)
UINT32 = (0x86, "I", 0xFFFFFFFF)
UINT32Z = (0x8C, "I", 0x00000000)

# (global message number, [(field name, field number, base type, scale, offset)])
# Scales and offsets are the FIT profile's: a value is stored as
# round((value + offset) * scale).
MESSAGES = {
    "file_id": (
        0,
        [
            ("type", 0, ENUM, 1, 0),
            ("manufacturer", 1, UINT16, 1, 0),
            ("product", 2, UINT16, 1, 0),
            ("serial_number", 3, UINT32Z, 1, 0),
            ("time_created", 4, UINT32, 1, 0),
        ],
    ),
    "record": (
        20,
        [
            ("timestamp", 253, UINT32, 1, 0),
            ("position_lat", 0, SINT32, 1, 0),
            ("position_long", 1, SINT32, 1, 0),
            ("distance", 5, UINT32, 100, 0),
            ("heart_rate", 3, UINT8, 1, 0),
            ("cadence", 4, UINT8, 1, 0),
            ("fractional_cadence", 53, UINT8, 128, 0),
            ("enhanced_speed", 73, UINT32, 1000, 0),
            ("enhanced_altitude", 78, UINT32, 5, 500),
        ],
    ),
    "split": (
        312,
        [
            ("split_type", 0, ENUM, 1, 0),
            ("total_elapsed_time", 1, UINT32, 1000, 0),
            ("total_timer_time", 2, UINT32, 1000, 0),
            ("total_distance", 3, UINT32, 100, 0),
            ("start_time", 9, UINT32, 1, 0),
            ("end_time", 27, UINT32, 1, 0),
        ],
    ),
    "lap": (
        19,
        [
            ("timestamp", 253, UINT32, 1, 0),
            ("start_time", 2, UINT32, 1, 0),
            ("total_elapsed_time", 7, UINT32, 1000, 0),
            ("total_timer_time", 8, UINT32, 1000, 0),
            ("total_distance", 9, UINT32, 100, 0),
            ("avg_heart_rate", 15, UINT8, 1, 0),
            ("max_heart_rate", 16, UINT8, 1, 0),
            ("lap_trigger", 24, ENUM, 1, 0),
        ],
    ),
    "session": (
        18,
        [
            ("timestamp", 253, UINT32, 1, 0),
            ("start_time", 2, UINT32, 1, 0),
            ("sport", 5, ENUM, 1, 0),
            ("sub_sport", 6, ENUM, 1, 0),
            ("total_elapsed_time", 7, UINT32, 1000, 0),
            ("total_timer_time", 8, UINT32, 1000, 0),
            ("total_distance", 9, UINT32, 100, 0),
            ("avg_heart_rate", 16, UINT8, 1, 0),
            ("max_heart_rate", 17, UINT8, 1, 0),
        ],
    ),
    "activity": (
        34,
        [
            ("timestamp", 253, UINT32, 1, 0),
            ("total_timer_time", 0, UINT32, 1000, 0),
            ("num_sessions", 1, UINT16, 1, 0),
            ("type", 2, ENUM, 1, 0),
            ("event", 3, ENUM, 1, 0),
            ("event_type", 4, ENUM, 1, 0),
            ("local_timestamp", 5, UINT32, 1, 0),
        ],
    ),
}

# Enum values from the public FIT profile.
FILE_ACTIVITY = 4
MANUFACTURER_GARMIN = 1
PRODUCT_FR265_LARGE = 4257
SPORT_RUNNING = 1
SPLIT_TYPE = {"rwd_run": 17, "rwd_walk": 18, "rwd_stand": 22}
EVENT_ACTIVITY, EVENT_TYPE_STOP = 26, 1

_CRC_TABLE = (
    0x0000,
    0xCC01,
    0xD801,
    0x1400,
    0xF001,
    0x3C00,
    0x2800,
    0xE401,
    0xA001,
    0x6C00,
    0x7800,
    0xB401,
    0x5000,
    0x9C01,
    0x8801,
    0x4400,
)


def fit_crc(data: bytes, crc: int = 0) -> int:
    """The FIT CRC-16, nibble-table form from the FIT SDK."""
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def fit_time(t: dt.datetime) -> int:
    """Seconds since the FIT epoch. Naive datetimes are taken as UTC."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return int(round((t - FIT_EPOCH).total_seconds()))


def _encode(value, base, scale, offset) -> bytes:
    code, fmt, invalid = base
    if value is None:
        raw = invalid
    elif isinstance(value, dt.datetime):
        raw = fit_time(value)
    else:
        raw = int(round((value + offset) * scale))
    return struct.pack("<" + fmt, raw)


def encode(messages: list[tuple[str, dict]]) -> bytes:
    """FIT bytes for a list of (message name, {field: value}) in file order.

    Every message of one type uses the type's full field list (a missing
    field is written as the base type's INVALID value, which fitdecode reads
    as None), so one definition per type is enough. Local message numbers are
    assigned per type in first-seen order.
    """
    body = bytearray()
    local: dict[str, int] = {}
    for name, fields in messages:
        gnum, spec = MESSAGES[name]
        unknown = set(fields) - {f[0] for f in spec}
        if unknown:
            raise KeyError(f"{name}: fields not in the encoder's spec: {unknown}")
        if name not in local:
            local[name] = len(local)
            if local[name] > 15:
                raise ValueError("more than 16 message types")
            body += struct.pack("<BBBHB", 0x40 | local[name], 0, 0, gnum, len(spec))
            for _fname, fnum, base, _s, _o in spec:
                size = struct.calcsize("<" + base[1])
                body += struct.pack("<BBB", fnum, size, base[0])
        body += struct.pack("<B", local[name])
        for fname, _fnum, base, scale, offset in spec:
            body += _encode(fields.get(fname), base, scale, offset)
    header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(body), b".FIT")
    header += struct.pack("<H", fit_crc(header))
    data = header + bytes(body)
    return data + struct.pack("<H", fit_crc(data))


# --------------------------------------------------------------------------
# THE FIXTURE: a run that ends in a car.
# --------------------------------------------------------------------------


def run_then_drive(
    start: dt.datetime,
    lat0: float = 46.5,
    lon0: float = 7.5,
    run_s: int = 1700,
    walk_s: int = 100,
    stand_s: int = 20,
    ramp_s: int = 10,
    drive_s: int = 300,
    drive_mps: float = 20.0,
    cadence: bool = True,
    tail: tuple = (),
) -> tuple[list, dict]:
    """(messages, facts) for a 1 Hz run followed by a drive.

    Phases, each a block of 1 s records:
      run    on foot at ~2.8-3.1 m/s, cadence 84-86 rpm (one leg)
      walk   ~1.4 m/s, cadence 55 rpm -- the cool-down to the car
      stand  speed 0, cadence 0 -- getting in
      ramp   cadence 0, accelerating to `drive_mps`
      drive  `drive_mps`, cadence 0 -- far above any running pace

    THE WATCH SEGMENTS THE DRIVE AS rwd_run, as real watches do, so the
    car carries running-split distance until a window removes it.

    `tail` appends more phases AFTER the drive, as (kind, seconds) pairs of
    the same kinds -- ("walk", 360) is the walk from the parked car, and
    ("run", 600), ("ramp", 10), ("drive", 300) a second run and a second
    drive. `cadence=False` writes NO cadence channel at all (every cadence
    field invalid), the shape of a phone recording.

    `facts` carries what a test asserts against: the last on-foot second and
    timestamp, the odometer there, and the drive's speed.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.UTC)
    phases = [
        ("run", run_s),
        ("walk", walk_s),
        ("stand", stand_s),
        ("ramp", ramp_s),
        ("drive", drive_s),
    ] + list(tail)
    kinds = [("run", 0, 1)]  # record 0 is the start, on foot
    for kind, n in phases:
        kinds += [(kind, j + 1, n) for j in range(n)]
    n_rec = len(kinds)
    recs, d, alt = [], 0.0, 500.0
    for i, (kind, j, n) in enumerate(kinds):
        speed = {
            "run": 2.8 + (i % 7) * 0.05,
            "walk": 1.4,
            "stand": 0.0,
            "ramp": drive_mps * (j / n),
            "drive": drive_mps,
        }[kind]
        cad = {"run": 84 + (i % 3), "walk": 55}.get(kind, 0)
        hr = {"run": 140 + (i % 11), "walk": 125}.get(kind, 98)
        if i:
            d += speed
        lat = lat0 + d / 111_320.0
        recs.append(
            {
                "timestamp": start + dt.timedelta(seconds=i),
                "position_lat": int(round(lat * SEMICIRCLES_PER_DEGREE)),
                "position_long": int(round(lon0 * SEMICIRCLES_PER_DEGREE)),
                "distance": round(d, 2),
                "heart_rate": hr,
                "cadence": cad if cadence else None,
                "fractional_cadence": 0.0 if cadence else None,
                "enhanced_speed": round(speed, 3),
                "enhanced_altitude": alt + 3.0 * ((i % 120) / 120.0),
            }
        )
    odo = [r["distance"] for r in recs]
    end_i = n_rec - 1
    total_s = float(end_i)
    t = lambda s: start + dt.timedelta(seconds=s)  # noqa: E731

    def split(kind, a, b):
        return (
            "split",
            {
                "split_type": SPLIT_TYPE[kind],
                "total_elapsed_time": float(b - a),
                "total_timer_time": float(b - a),
                "total_distance": round(odo[b] - odo[a], 2),
                "start_time": t(a),
                "end_time": t(b),
            },
        )

    a_walk = run_s
    a_stand = a_walk + walk_s
    a_car = a_stand + stand_s
    a_tail = a_car + ramp_s + drive_s
    hrs = [r["heart_rate"] for r in recs]
    messages = [
        (
            "file_id",
            {
                "type": FILE_ACTIVITY,
                "manufacturer": MANUFACTURER_GARMIN,
                "product": PRODUCT_FR265_LARGE,
                "serial_number": 12345,
                "time_created": t(0),
            },
        )
    ]
    messages += [("record", r) for r in recs]
    messages += [
        split("rwd_run", 0, a_walk),
        split("rwd_walk", a_walk, a_stand),
        split("rwd_stand", a_stand, a_car),
        # THE CAR, segmented by the watch as RUNNING.
        split("rwd_run", a_car, a_tail),
    ]
    a = a_tail
    for kind, n in tail:
        messages.append(
            split(
                {"walk": "rwd_walk", "stand": "rwd_stand"}.get(kind, "rwd_run"),
                a,
                a + n,
            )
        )
        a += n
    messages += [
        (
            "lap",
            {
                "timestamp": t(end_i),
                "start_time": t(0),
                "total_elapsed_time": total_s,
                "total_timer_time": total_s,
                "total_distance": odo[-1],
                "avg_heart_rate": round(sum(hrs) / len(hrs)),
                "max_heart_rate": max(hrs),
                "lap_trigger": 0,
            },
        ),
        (
            "session",
            {
                "timestamp": t(end_i),
                "start_time": t(0),
                "sport": SPORT_RUNNING,
                "sub_sport": 0,
                "total_elapsed_time": total_s,
                "total_timer_time": total_s,
                "total_distance": odo[-1],
                "avg_heart_rate": round(sum(hrs) / len(hrs)),
                "max_heart_rate": max(hrs),
            },
        ),
        # local_timestamp = the same instant on a UTC+1 clock.
        (
            "activity",
            {
                "timestamp": t(end_i),
                "total_timer_time": total_s,
                "num_sessions": 1,
                "type": 0,
                "event": EVENT_ACTIVITY,
                "event_type": EVENT_TYPE_STOP,
                "local_timestamp": t(end_i + 3600),
            },
        ),
    ]
    last_foot = a_stand  # the last walking record
    facts = {
        "n_records": n_rec,
        "last_on_foot_s": last_foot,
        "last_on_foot_utc": t(last_foot).replace(tzinfo=None),
        "file_end_utc": t(end_i).replace(tzinfo=None),
        "drive_start_utc": t(a_car + 1).replace(tzinfo=None),
        "drive_end_utc": t(a_tail).replace(tzinfo=None),
        "odometer_at_last_on_foot_m": odo[last_foot],
        "odometer_total_m": odo[-1],
        "drive_mps": drive_mps,
        "max_on_foot_mps": max(r["enhanced_speed"] for r in recs[: last_foot + 1]),
    }
    return messages, facts


def write_run_then_drive(path: Path, start: dt.datetime, **kw) -> dict:
    """Write the run-then-drive fixture to `path`; return its facts."""
    messages, facts = run_then_drive(start, **kw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(messages))
    return facts
