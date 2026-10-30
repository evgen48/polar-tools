#!/usr/bin/env python3
"""
Recover truncated Polar ROUTE.GZB / SAMPLES.GZB files and export a GPX track.

No third-party packages and no Ruby/Nokogiri required.

Why it is needed
----------------
When a recording was not finalised on the watch (no TSESS.BPB / BASE.BPB, only
TEMP.BPB / SAMPLES.BIN present) the *.GZB files are cut off in the middle of
the gzip stream, so gzip/Ruby's Zlib::GzipReader fail with "unexpected end of
file".  The deflate data that *is* there is perfectly readable, so we inflate
it as a raw deflate stream (ignoring the gzip header/trailer) and decode the
protobuf messages up to the last complete value.

Usage
-----
  python polar_recover_gpx.py <activity_dir> [-o out.gpx] [--dump-dir DIR] [--inspect]

  <activity_dir> is e.g. C:\\Users\\me\\Polar\\watch1\\U\\0\\20261029\\E\\110031
  (the folder that contains the 00\\ subfolder).  Several folders may be given;
  with several folders -o must be a directory.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import struct
import sys
import xml.etree.ElementTree as ET
import zlib

# --------------------------------------------------------------------------
# gzip recovery
# --------------------------------------------------------------------------


def inflate_gzip_lenient(data: bytes) -> tuple[bytes, bool]:
    """Inflate a gzip file, returning whatever could be decoded.

    Returns (decompressed_bytes, complete) where complete tells whether the
    deflate stream ended properly.
    """
    if len(data) < 10 or data[0] != 0x1F or data[1] != 0x8B:
        # Header is wrong -> treat the whole file as a raw deflate stream
        start = 0
    else:
        flags = data[3]
        start = 10
        if flags & 0x04:  # FEXTRA
            start += 2 + int.from_bytes(data[start:start + 2], "little")
        if flags & 0x08:  # FNAME
            start = data.index(b"\0", start) + 1
        if flags & 0x10:  # FCOMMENT
            start = data.index(b"\0", start) + 1
        if flags & 0x02:  # FHCRC
            start += 2

    d = zlib.decompressobj(-zlib.MAX_WBITS)
    out = bytearray()
    pos = start
    complete = False
    try:
        # Feed in small pieces so that everything before a corrupt block is kept
        while pos < len(data) and not d.eof:
            out += d.decompress(data[pos:pos + 1024])
            pos += 1024
        complete = d.eof
        if not complete:
            out += d.flush()
    except zlib.error:
        pass
    return bytes(out), complete


# --------------------------------------------------------------------------
# minimal protobuf reader (tolerant to truncation)
# --------------------------------------------------------------------------


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    r = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        r |= (b & 0x7F) << shift
        if not b & 0x80:
            return r, i
        shift += 7


def parse_fields(buf: bytes) -> dict[int, list]:
    """Top level fields -> {field_number: [values]}.

    Varints are returned as int, length-delimited as bytes.  A final
    length-delimited field that is cut short is returned with the bytes that
    are available (the caller decides what to do with the partial value).
    """
    fields: dict[int, list] = {}
    i = 0
    n = len(buf)
    while i < n:
        try:
            tag, i = _varint(buf, i)
            num, wt = tag >> 3, tag & 7
            if wt == 0:
                v, i = _varint(buf, i)
            elif wt == 2:
                ln, i = _varint(buf, i)
                v = bytes(buf[i:i + ln])
                i += ln
            elif wt == 1:
                v = bytes(buf[i:i + 8])
                i += 8
            elif wt == 5:
                v = bytes(buf[i:i + 4])
                i += 4
            else:
                break
        except IndexError:
            break
        fields.setdefault(num, []).append(v)
    return fields


def packed_varints(chunks: list[bytes], signed_zigzag: bool = False) -> list[int]:
    out: list[int] = []
    for c in chunks:
        i = 0
        while i < len(c):
            try:
                v, j = _varint(c, i)
            except IndexError:  # truncated last varint
                break
            i = j
            if signed_zigzag:
                v = (v >> 1) ^ -(v & 1)
            out.append(v)
    return out


def packed_fixed(chunks: list[bytes], fmt: str) -> list[float]:
    size = struct.calcsize(fmt)
    out: list[float] = []
    for c in chunks:
        usable = len(c) - len(c) % size  # drop a partially written value
        out.extend(struct.unpack("<%d%s" % (usable // size, fmt), c[:usable]))
    return out


def decode_datetime(blob: bytes) -> dt.datetime | None:
    """PbSystemDateTime {date{y,m,d}, time{h,m,s,ms}} (UTC)."""
    f = parse_fields(blob)
    try:
        date = parse_fields(f[1][0])
        time = parse_fields(f[2][0])
        return dt.datetime(
            date[1][0], date[2][0], date[3][0],
            time.get(1, [0])[0], time.get(2, [0])[0], time.get(3, [0])[0],
            time.get(4, [0])[0] * 1000, tzinfo=dt.timezone.utc,
        )
    except (KeyError, IndexError, ValueError):
        return None


def decode_duration(blob: bytes) -> float:
    f = parse_fields(blob)
    g = lambda k: f.get(k, [0])[0]
    return g(1) * 3600 + g(2) * 60 + g(3) + g(4) / 1000.0


# --------------------------------------------------------------------------
# Polar messages
# --------------------------------------------------------------------------


def load_route(path: str) -> tuple[dict, dict]:
    raw, complete = inflate_gzip_lenient(open(path, "rb").read())
    f = parse_fields(raw)
    route = {
        "duration": packed_varints(f.get(1, [])),                 # ms
        "lat": packed_fixed(f.get(2, []), "d"),
        "lon": packed_fixed(f.get(3, []), "d"),
        "alt": packed_varints(f.get(4, []), signed_zigzag=True),  # m
        "sats": packed_varints(f.get(5, [])),
        "first_time": decode_datetime(f[9][0]) if 9 in f else None,
    }
    info = {"complete": complete, "raw_len": len(raw)}
    return route, info


def load_samples(path: str) -> tuple[dict, dict]:
    raw, complete = inflate_gzip_lenient(open(path, "rb").read())
    f = parse_fields(raw)
    samples = {
        "interval": decode_duration(f[1][0]) if 1 in f else 1.0,
        "hr": packed_varints(f.get(2, [])),
        "cad": packed_varints(f.get(4, [])),
        "alt": packed_fixed(f.get(6, []), "f"),
        "temp": packed_fixed(f.get(8, []), "f"),
        "speed": packed_fixed(f.get(9, []), "f"),
        "dist": packed_fixed(f.get(11, []), "f"),
        "pauses": decode_pauses(f.get(30, [])),
    }
    info = {"complete": complete, "raw_len": len(raw)}
    return samples, info


def decode_pauses(chunks: list[bytes]) -> list[tuple[float, float]]:
    """PbPauseTime {start: PbDuration, duration: PbDuration} -> [(start_s, length_s)].

    ``start`` is measured on the recording clock (active time, i.e. without
    earlier pauses), the same clock as the route ``duration`` values;
    ``duration`` is the real length of the pause.
    """
    out = []
    for c in chunks:
        f = parse_fields(c)
        if 1 in f and 2 in f:
            out.append((decode_duration(f[1][0]), decode_duration(f[2][0])))
    return sorted(out)


def folder_start_time(activity_dir: str) -> dt.datetime | None:
    """Fallback: <...>\\<YYYYMMDD>\\E\\<HHMMSS> is the *local* start time."""
    p = os.path.normpath(activity_dir).split(os.sep)
    try:
        day, _, hms = p[-3], p[-2], p[-1]
        local = dt.datetime.strptime(day + hms, "%Y%m%d%H%M%S")
        return local.astimezone().astimezone(dt.timezone.utc)
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------
# GPX
# --------------------------------------------------------------------------

NS_GPX = "http://www.topografix.com/GPX/1/1"
NS_GPXTPX = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * \
        math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def build_gpx(route: dict, samples: dict | None, start: dt.datetime) -> tuple[ET.ElementTree, dict]:
    """Build the GPX.

    The watch's recording clock stops while the exercise is paused, so the
    route ``duration`` values only count *active* time, and the straight line
    between the last fix before a pause and the first one after it is the
    distance travelled while paused (car, bus...).  The pauses are stored in
    SAMPLES.GZB as ``pause_times`` (start = active time at which the pause began,
    duration = real length of the pause).  They are preserved as:
      * a new <trkseg> at every pause -- GPS tools therefore do not draw or
        count a line across the pause;
      * real wall-clock time stamps: every point after a pause is shifted by the
        length of all earlier pauses, so the pause is visible in the timeline;
      * two <wpt> per pause ("Pause N start" / "Pause N end") with the pause
        length and the straight-line distance that was skipped.
    """
    ET.register_namespace("", NS_GPX)
    ET.register_namespace("gpxtpx", NS_GPXTPX)
    ET.register_namespace("xsi", NS_XSI)

    # Only use points where lat AND lon (and the time offset, if present) are complete.
    # The last element of a truncated file is usually the partially written one.
    n = min(len(route["lat"]), len(route["lon"]))
    durations = route["duration"]
    if durations:
        n = min(n, len(durations))
    use_dur = len(durations) >= n and n > 0

    interval = (samples or {}).get("interval") or 1.0
    pauses = (samples or {}).get("pauses", [])     # [(active_start_s, length_s)]
    first = durations[0] if use_dur else 0

    def ex_time(i: int) -> float:        # active seconds since exercise start
        return durations[i] / 1000.0 if use_dur else float(i)

    def pauses_before(sec: float) -> int:   # number of pauses that started at/before sec
        return sum(1 for a, _ in pauses if a <= sec)

    def wall(sec: float) -> dt.datetime:
        shift = sum(length for a, length in pauses if a <= sec)
        return start + dt.timedelta(seconds=sec - first / 1000.0 + shift)

    def fmt(t: dt.datetime) -> str:
        # round (not truncate) to whole seconds so ~1 s fixes never share a stamp
        return (t + dt.timedelta(milliseconds=500)).strftime("%Y-%m-%dT%H:%M:%SZ")

    root = ET.Element("{%s}gpx" % NS_GPX, {
        "version": "1.1",
        "creator": "polar_recover_gpx.py (based on https://github.com/cmaion/polar)",
    })
    meta = ET.SubElement(root, "{%s}metadata" % NS_GPX)
    ET.SubElement(ET.SubElement(meta, "{%s}author" % NS_GPX), "{%s}name" % NS_GPX).text = \
        "https://github.com/cmaion/polar"
    ET.SubElement(meta, "{%s}time" % NS_GPX).text = fmt(start)

    # --- split into segments at pauses -----------------------------------------
    segments: list[list[int]] = []
    boundaries: list[tuple[int, int]] = []     # (last index before, first index after) a pause
    moving_m = 0.0
    skipped_m = 0.0
    prev_k = 0
    for i in range(n):
        k = pauses_before(ex_time(i))
        d = 0.0
        if i > 0:
            d = haversine(route["lat"][i - 1], route["lon"][i - 1], route["lat"][i], route["lon"][i])
        if i == 0 or k != prev_k:
            segments.append([])
            if i > 0:
                boundaries.append((i - 1, i))
                skipped_m += d
        else:
            moving_m += d
        segments[-1].append(i)
        prev_k = k

    # --- pause waypoints ---------------------------------------------------
    gaps = []
    for a, b in boundaries:
        before, after = ex_time(a), ex_time(b)
        # pauses that began between the two fixes
        group = [(pa, pl) for pa, pl in pauses if before < pa <= after]
        length = sum(pl for _, pl in group)
        jump = haversine(route["lat"][a], route["lon"][a], route["lat"][b], route["lon"][b])
        gaps.append((length, jump))
        for label, idx in (("start", a), ("end", b)):
            w = ET.SubElement(root, "{%s}wpt" % NS_GPX, {"lat": "%.8f" % route["lat"][idx],
                                                          "lon": "%.8f" % route["lon"][idx]})
            ET.SubElement(w, "{%s}time" % NS_GPX).text = fmt(wall(ex_time(idx)))
            ET.SubElement(w, "{%s}name" % NS_GPX).text = "Pause %d %s" % (len(gaps), label)
            ET.SubElement(w, "{%s}desc" % NS_GPX).text = \
                "paused %s, %.0f m straight-line skipped (not counted)" % (_hms(length), jump)

    # --- track points ------------------------------------------------------
    def add_point(seg, i: int) -> None:
        sec = ex_time(i)
        pt = ET.SubElement(seg, "{%s}trkpt" % NS_GPX,
                           {"lat": "%.8f" % route["lat"][i], "lon": "%.8f" % route["lon"][i]})
        if i < len(route["alt"]):
            ET.SubElement(pt, "{%s}ele" % NS_GPX).text = "%.1f" % route["alt"][i]
        ET.SubElement(pt, "{%s}time" % NS_GPX).text = fmt(wall(sec))
        if samples:
            si = int(round(sec / interval))
            ext_vals = []
            if si < len(samples["temp"]):
                ext_vals.append(("atemp", "%.1f" % samples["temp"][si]))
            if si < len(samples["hr"]):
                ext_vals.append(("hr", str(samples["hr"][si])))
            if si < len(samples["cad"]):
                ext_vals.append(("cad", str(samples["cad"][si])))
            if ext_vals:
                ext = ET.SubElement(pt, "{%s}extensions" % NS_GPX)
                tpe = ET.SubElement(ext, "{%s}TrackPointExtension" % NS_GPXTPX)
                for key, v in ext_vals:
                    ET.SubElement(tpe, "{%s}%s" % (NS_GPXTPX, key)).text = v

    trk = ET.SubElement(root, "{%s}trk" % NS_GPX)
    ET.SubElement(trk, "{%s}name" % NS_GPX).text = "Track"
    for seg_idx in segments:
        seg = ET.SubElement(trk, "{%s}trkseg" % NS_GPX)
        for i in seg_idx:
            add_point(seg, i)

    ET.indent(root, space="  ")
    stats = {
        "points": n,
        "segments": len(segments),
        "gaps": gaps,
        "pauses_total": len(pauses),
        "pause_seconds": sum(pl for _, pl in pauses),
        "moving_m": moving_m,
        "skipped_m": skipped_m,
        "end": wall(ex_time(n - 1)) if n else start,
    }
    return ET.ElementTree(root), stats


def _hms(sec: float) -> str:
    sec = int(round(sec))
    return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def process(activity_dir: str, out_path: str, dump_dir: str | None, inspect: bool) -> int:
    d00 = os.path.join(activity_dir, "00")
    route_file = os.path.join(d00, "ROUTE.GZB")
    samples_file = os.path.join(d00, "SAMPLES.GZB")
    if not os.path.isfile(route_file):
        print("%s: no 00\\ROUTE.GZB, skipping" % activity_dir)
        return 1

    route, rinfo = load_route(route_file)
    samples, sinfo = (None, None)
    if os.path.isfile(samples_file):
        samples, sinfo = load_samples(samples_file)

    print("== %s" % activity_dir)
    print("ROUTE.GZB   : %s, %d bytes inflated, points lat=%d lon=%d alt=%d dur=%d" % (
        "complete" if rinfo["complete"] else "TRUNCATED (recovered)", rinfo["raw_len"],
        len(route["lat"]), len(route["lon"]), len(route["alt"]), len(route["duration"])))
    if samples:
        print("SAMPLES.GZB : %s, %d bytes inflated, interval=%.1fs hr=%d cad=%d temp=%d speed=%d" % (
            "complete" if sinfo["complete"] else "TRUNCATED (recovered)", sinfo["raw_len"],
            samples["interval"], len(samples["hr"]), len(samples["cad"]),
            len(samples["temp"]), len(samples["speed"])))

    start = route["first_time"] or folder_start_time(activity_dir)
    if start is None:
        print("Cannot determine start time", file=sys.stderr)
        return 2
    print("Start (UTC) : %s  [%s]" % (start.isoformat(),
                                      "route first_location_time" if route["first_time"] else "folder name"))

    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)
        for name in ("ROUTE", "SAMPLES"):
            src = os.path.join(d00, name + ".GZB")
            if os.path.isfile(src):
                raw, _ = inflate_gzip_lenient(open(src, "rb").read())
                with open(os.path.join(dump_dir, name + ".pb"), "wb") as fh:
                    fh.write(raw)
        print("Raw protobuf dumped to %s" % dump_dir)

    if inspect:
        d = route["duration"]
        print("duration[0..5]=%s ... last=%s ; lat[0]=%s lon[0]=%s alt[0]=%s" % (
            d[:5], d[-1:], route["lat"][:1], route["lon"][:1], route["alt"][:1]))
        if samples and samples["pauses"]:
            print("pause_times (active-clock start -> real length):")
            for a_, l_ in samples["pauses"]:
                print("   %s  paused %s" % (_hms(a_), _hms(l_)))

    tree, st = build_gpx(route, samples, start)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    print("Pauses      : %d stored, %d with route points on both sides, %s paused in total" % (
        st["pauses_total"], len(st["gaps"]), _hms(st["pause_seconds"])))
    print("Track       : %d points in %d segment(s); GPS distance %.2f km, "
          "%.2f km straight-line skipped over pauses (not counted)" % (
              st["points"], st["segments"], st["moving_m"] / 1000, st["skipped_m"] / 1000))
    print("Wall clock  : %s -> %s UTC" % (start.strftime("%H:%M:%S"), st["end"].strftime("%H:%M:%S")))
    print("GPX         : -> %s" % out_path)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("activity_dirs", nargs="+")
    ap.add_argument("-o", "--output", help="GPX file (one folder) or directory (several folders)")
    ap.add_argument("--dump-dir", help="also write the recovered raw protobuf (ROUTE.pb / SAMPLES.pb)")
    ap.add_argument("--inspect", action="store_true", help="print extra diagnostics")
    a = ap.parse_args()

    rc = 0
    for ad in a.activity_dirs:
        ad = os.path.normpath(ad)
        name = "%s_%s.gpx" % (os.path.basename(os.path.dirname(os.path.dirname(ad))), os.path.basename(ad))
        if a.output and len(a.activity_dirs) == 1 and not os.path.isdir(a.output):
            out = a.output
        else:
            out = os.path.join(a.output or ad, name)
        rc |= process(ad, out, a.dump_dir, a.inspect)
    return rc


if __name__ == "__main__":
    sys.exit(main())
