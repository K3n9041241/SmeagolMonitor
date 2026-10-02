#!/usr/bin/env python3

"""
Smeagol Monitor / External Server

Python 3.10+
Standard library only (pyserial is optional).

Features:
- HTTP/HTTPS packet receiver
- Raspberry Pi API-key authentication
- Vehicle state API
- KML course loading
- CSV logging
- Communication-gap logging
- Static Smeagol Monitor UI
- Render/cloud PORT environment support
"""

import argparse
import base64
import csv
import hmac
import json
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET

from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


# ============================================================
# PATH / TIME
# ============================================================

ROOT = Path(__file__).resolve().parent

NS = {
    "k": "http://www.opengis.net/kml/2.2"
}

JST = timezone(
    timedelta(hours=9)
)


# ============================================================
# ENVIRONMENT
# ============================================================

API_KEY = os.environ.get(
    "SMEAGOL_API_KEY",
    ""
).strip()


VIEW_USER = os.environ.get(
    "SMEAGOL_VIEW_USER",
    ""
).strip()


VIEW_PASSWORD = os.environ.get(
    "SMEAGOL_VIEW_PASSWORD",
    ""
).strip()


# ============================================================
# CSV
# ============================================================

CSV_FIELDS = [
    "received_at_jst",
    "base_received_at_jst",
    "vehicle_id",
    "latitude",
    "longitude",
    "speed_raw",
    "satellites",
    "g",
    "status",
    "status_raw",
    "interval_s",
    "interval_basis",
    "gap_detected",
    "source",
    "raw",
]


GAP_FIELDS = [
    "vehicle_id",
    "source",
    "previous_received_at_jst",
    "received_at_jst",
    "previous_latitude",
    "previous_longitude",
    "latitude",
    "longitude",
    "interval_s",
    "interval_basis",
]


# ============================================================
# TIME
# ============================================================

def jst_time(epoch):

    return datetime.fromtimestamp(
        epoch,
        JST
    ).isoformat(
        timespec="milliseconds"
    )


# ============================================================
# CSV WRITE
# ============================================================

def append_csv(
    path,
    fields,
    row
):

    with path.open(
        "a",
        encoding="utf-8",
        newline=""
    ) as file:

        csv.DictWriter(
            file,
            fieldnames=fields
        ).writerow(
            row
        )


# ============================================================
# VEHICLE PACKET
# ============================================================

def parse_packet(raw):

    p = [
        value.strip()
        for value
        in raw.strip().split(",")
    ]


    if (
        len(p) not in (7, 11)
        or
        not re.fullmatch(
            r"[A-Za-z0-9_-]{1,24}",
            p[0]
        )
    ):

        raise ValueError(
            "Vehicle ID or packet field count is invalid"
        )


    try:

        numbers = [
            float(value)
            for value
            in p[1:-1]
        ]

    except ValueError:

        raise ValueError(
            "Numeric packet field is invalid"
        ) from None


    if not all(
        math.isfinite(number)
        for number in numbers
    ):

        raise ValueError(
            "NaN/Infinity is not accepted"
        )


    lat, lon, speed, sats, *sensors = numbers


    if not (
        -90 <= lat <= 90
        and
        -180 <= lon <= 180
    ):

        raise ValueError(
            "Latitude / longitude is out of range"
        )


    status = {
        "O": "OK",
        "S": "SOS",
        "C": "CRASH",
    }.get(
        p[-1],
        p[-1]
    )


    if (
        speed < 0
        or
        sats < 0
        or
        not sats.is_integer()
        or
        status not in (
            "OK",
            "SOS",
            "CRASH"
        )
    ):

        raise ValueError(
            "Speed / satellites / status is invalid"
        )


    return dict(
        id=p[0],
        lat=lat,
        lon=lon,
        speed=speed,
        satellites=int(sats),
        sensors=sensors,
        status=status,
        raw=raw.strip(),
    )


# ============================================================
# KML COURSE
# ============================================================

def load_course(path):

    if not path.exists():

        print(
            f"KML not found: {path}",
            flush=True
        )

        return {
            "type": "FeatureCollection",
            "features": []
        }


    root = ET.parse(
        path
    ).getroot()


    features = []


    for pm in root.findall(
        ".//k:Placemark",
        NS
    ):

        name = pm.findtext(
            "k:name",
            default="",
            namespaces=NS
        )


        for kind in (
            "Point",
            "LineString"
        ):

            for geom in pm.findall(
                ".//k:" + kind,
                NS
            ):

                values = geom.findtext(
                    "k:coordinates",
                    default="",
                    namespaces=NS
                )


                coords = [
                    list(
                        map(
                            float,
                            token.split(",")[:2]
                        )
                    )
                    for token
                    in values.split()
                ]


                if not coords:
                    continue


                features.append(
                    dict(
                        type="Feature",
                        properties={
                            "name": name
                        },
                        geometry=dict(
                            type=kind,
                            coordinates=(
                                coords[0]
                                if kind == "Point"
                                else coords
                            )
                        )
                    )
                )


    return dict(
        type="FeatureCollection",
        features=features
    )


# ============================================================
# RECEIVER
# ============================================================

class Receiver:

    def __init__(
        self,
        log_dir,
        gap_seconds=3.0
    ):

        self.lock = (
            threading.Lock()
        )

        self.vehicles = {}

        self.total = 0
        self.invalid = 0

        self.input_status = (
            "HTTP waiting"
        )

        self.gap_seconds = (
            gap_seconds
        )


        log_dir.mkdir(
            parents=True,
            exist_ok=True
        )


        filename = (
            datetime.now()
            .strftime(
                "%Y%m%d-%H%M%S-%f"
            )
        )


        self.log_path = (
            log_dir
            / (filename + ".jsonl")
        )


        self.csv_path = (
            self.log_path
            .with_suffix(".csv")
        )


        self.gaps_path = (
            self.log_path
            .with_name(
                self.log_path.stem
                + "-gaps.csv"
            )
        )


        for path, fields in (
            (
                self.csv_path,
                CSV_FIELDS
            ),
            (
                self.gaps_path,
                GAP_FIELDS
            ),
        ):

            with path.open(
                "x",
                encoding="utf-8-sig",
                newline=""
            ) as file:

                csv.DictWriter(
                    file,
                    fieldnames=fields
                ).writeheader()


    # ========================================================
    # RECEIVE
    # ========================================================

    def receive(
        self,
        raw,
        source="http",
        base_received_epoch=None,
        base_interval_s=None
    ):

        for value in (
            base_received_epoch,
            base_interval_s
        ):

            if (
                value is not None
                and
                (
                    isinstance(
                        value,
                        bool
                    )
                    or
                    not isinstance(
                        value,
                        (int, float)
                    )
                    or
                    not math.isfinite(
                        value
                    )
                    or
                    value < 0
                )
            ):

                raise ValueError(
                    "Invalid Base timestamp / interval"
                )


        if (
            base_received_epoch
            is not None
        ):

            try:

                jst_time(
                    base_received_epoch
                )

            except (
                ValueError,
                OverflowError,
                OSError
            ):

                raise ValueError(
                    "Base timestamp is out of range"
                ) from None


        now = time.time()


        record = dict(
            received_at=(
                datetime.fromtimestamp(
                    now,
                    timezone.utc
                ).isoformat()
            ),
            received_epoch=now,
            source=source,
            raw=raw,
            base_received_epoch=(
                base_received_epoch
            ),
            base_interval_s=(
                base_interval_s
            ),
        )


        try:

            packet = parse_packet(
                raw
            )

            record[
                "packet"
            ] = packet

        except ValueError as error:

            packet = None

            record[
                "error"
            ] = str(
                error
            )


        with self.lock:

            mono = (
                time.monotonic()
            )


            #
            # JSONL raw log
            #
            with self.log_path.open(
                "a",
                encoding="utf-8"
            ) as file:

                file.write(
                    json.dumps(
                        record,
                        ensure_ascii=False
                    )
                    + "\n"
                )


            self.total += 1


            if packet is None:

                self.invalid += 1


            else:

                key = (
                    source
                    + ":"
                    + packet["id"]
                )


                old = (
                    self.vehicles.get(
                        key
                    )
                )


                interval = (
                    mono
                    - old["_mono"]
                    if old
                    else None
                )


                basis = (
                    "monitor"
                )


                event_time = (
                    now
                )


                if (
                    base_received_epoch
                    is not None
                ):

                    interval = (
                        base_interval_s
                    )

                    basis = (
                        "base"
                    )

                    event_time = (
                        base_received_epoch
                    )


                gap = (
                    interval is not None
                    and
                    interval
                    > self.gap_seconds
                )


                #
                # Current 7-field packet:
                #
                # V001,LAT,LON,SPEED,SAT,G,STATUS
                #
                g = (
                    packet["sensors"][0]
                    if
                    len(
                        raw.strip()
                        .split(",")
                    ) == 7
                    else ""
                )


                append_csv(
                    self.csv_path,
                    CSV_FIELDS,
                    dict(
                        received_at_jst=(
                            jst_time(
                                now
                            )
                        ),

                        base_received_at_jst=(
                            jst_time(
                                base_received_epoch
                            )
                            if
                            base_received_epoch
                            is not None
                            else ""
                        ),

                        vehicle_id=(
                            packet["id"]
                        ),

                        latitude=(
                            packet["lat"]
                        ),

                        longitude=(
                            packet["lon"]
                        ),

                        speed_raw=(
                            packet["speed"]
                        ),

                        satellites=(
                            packet["satellites"]
                        ),

                        g=g,

                        status=(
                            packet["status"]
                        ),

                        status_raw=(
                            raw.strip()
                            .split(",")[-1]
                            .strip()
                        ),

                        interval_s=(
                            round(
                                interval,
                                6
                            )
                            if
                            interval
                            is not None
                            else ""
                        ),

                        interval_basis=(
                            basis
                        ),

                        gap_detected=(
                            int(
                                gap
                            )
                        ),

                        source=source,

                        raw=(
                            packet["raw"]
                        )
                    )
                )


                #
                # GAP CSV
                #
                if (
                    gap
                    and
                    old
                    and
                    old["_basis"]
                    == basis
                ):

                    append_csv(
                        self.gaps_path,
                        GAP_FIELDS,
                        dict(
                            vehicle_id=(
                                packet["id"]
                            ),

                            source=source,

                            previous_received_at_jst=(
                                jst_time(
                                    old[
                                        "_event_time"
                                    ]
                                )
                            ),

                            received_at_jst=(
                                jst_time(
                                    event_time
                                )
                            ),

                            previous_latitude=(
                                old["lat"]
                            ),

                            previous_longitude=(
                                old["lon"]
                            ),

                            latitude=(
                                packet["lat"]
                            ),

                            longitude=(
                                packet["lon"]
                            ),

                            interval_s=(
                                round(
                                    interval,
                                    6
                                )
                            ),

                            interval_basis=(
                                basis
                            ),
                        )
                    )


                packet.update(
                    key=key,
                    source=source,
                    received_epoch=now,
                    _mono=mono,
                    _event_time=event_time,
                    _basis=basis,

                    count=(
                        old["count"] + 1
                        if old
                        else 1
                    ),

                    interval_s=(
                        mono
                        - old["_mono"]
                        if old
                        else None
                    )
                )


                self.vehicles[
                    key
                ] = packet


        if packet is None:

            raise ValueError(
                record["error"]
            )


        return {
            "accepted": True
        }


    # ========================================================
    # SNAPSHOT
    # ========================================================

    def snapshot(self):

        with self.lock:

            vehicles = [

                {
                    **{
                        key: value
                        for key, value
                        in packet.items()
                        if not key.startswith(
                            "_"
                        )
                    },

                    "age_s":
                        time.monotonic()
                        - packet["_mono"]

                }

                for packet
                in self.vehicles.values()
            ]


            return dict(
                vehicles=vehicles,
                total=self.total,
                invalid=self.invalid,
                input_status=(
                    self.input_status
                ),
                log=(
                    self.log_path.name
                ),
                csv_log=(
                    self.csv_path.name
                ),
                gaps_log=(
                    self.gaps_path.name
                ),
                gap_seconds=(
                    self.gap_seconds
                )
            )


# ============================================================
# OPTIONAL SERIAL INPUT
# ============================================================

def serial_lines(
    port,
    baud,
    receiver,
    stop
):

    import serial


    while not stop.is_set():

        try:

            with serial.Serial(
                port,
                baud,
                timeout=0.5
            ) as connection:

                receiver.input_status = (
                    f"Serial connected: "
                    f"{port} / {baud}"
                )


                buffer = bytearray()


                while not stop.is_set():

                    chunk = (
                        connection.read_until(
                            b"\n",
                            4096
                        )
                    )


                    buffer.extend(
                        chunk
                    )


                    if len(
                        buffer
                    ) > 8192:

                        receiver.receive(
                            "OVERSIZE:"
                            + buffer.hex(),
                            "serial"
                        )

                        buffer.clear()


                    if buffer.endswith(
                        b"\n"
                    ):

                        line = (
                            bytes(
                                buffer
                            )
                            .decode(
                                "utf-8",
                                errors="replace"
                            )
                            .strip()
                        )


                        buffer.clear()


                        if line:

                            try:

                                receiver.receive(
                                    line,
                                    "serial"
                                )

                            except ValueError:

                                pass


        except (
            serial.SerialException,
            OSError,
            ValueError
        ) as error:

            receiver.input_status = (
                f"Serial reconnecting: "
                f"{error}"
            )

            stop.wait(
                2
            )


# ============================================================
# AUTH HELPERS
# ============================================================

def valid_api_key(
    handler
):

    if not API_KEY:
        return True


    supplied = (
        handler.headers.get(
            "X-Smeagol-API-Key",
            ""
        )
    )


    return hmac.compare_digest(
        supplied,
        API_KEY
    )


def valid_view_auth(
    handler
):

    #
    # If viewer credentials are not configured,
    # Monitor UI remains public.
    #
    if (
        not VIEW_USER
        or
        not VIEW_PASSWORD
    ):

        return True


    supplied = (
        handler.headers.get(
            "Authorization",
            ""
        )
    )


    expected_raw = (
        VIEW_USER
        + ":"
        + VIEW_PASSWORD
    ).encode(
        "utf-8"
    )


    expected = (
        "Basic "
        + base64.b64encode(
            expected_raw
        ).decode(
            "ascii"
        )
    )


    return hmac.compare_digest(
        supplied,
        expected
    )


def same_origin(
    handler
):

    origin = (
        handler.headers.get(
            "Origin"
        )
    )


    #
    # Raspberry Pi server-to-server POST
    #
    if not origin:
        return False


    try:

        parsed = (
            urlparse(
                origin
            )
        )

    except ValueError:

        return False


    return (
        parsed.netloc
        ==
        handler.headers.get(
            "Host",
            ""
        )
    )


# ============================================================
# HTTP HANDLER
# ============================================================

def make_handler(
    receiver,
    course
):

    class Handler(
        SimpleHTTPRequestHandler
    ):

        def __init__(
            self,
            *args,
            **kwargs
        ):

            super().__init__(
                *args,
                directory=str(
                    ROOT
                    / "static"
                ),
                **kwargs
            )


        def log_message(
            self,
            *args
        ):

            pass


        # ----------------------------------------------------
        # JSON response
        # ----------------------------------------------------

        def send_json(
            self,
            data,
            status=200
        ):

            body = json.dumps(
                data,
                ensure_ascii=False,
                allow_nan=False
            ).encode(
                "utf-8"
            )


            self.send_response(
                status
            )

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(
                    len(
                        body
                    )
                )
            )

            self.send_header(
                "Cache-Control",
                "no-store"
            )

            self.end_headers()

            self.wfile.write(
                body
            )


        # ----------------------------------------------------
        # Basic auth challenge
        # ----------------------------------------------------

        def require_view_auth(
            self
        ):

            if valid_view_auth(
                self
            ):

                return True


            body = b"Authentication required"


            self.send_response(
                401
            )

            self.send_header(
                "WWW-Authenticate",
                'Basic realm="Smeagol"'
            )

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(
                    len(
                        body
                    )
                )
            )

            self.end_headers()

            self.wfile.write(
                body
            )

            return False


        # ----------------------------------------------------
        # GET
        # ----------------------------------------------------

        def do_GET(
            self
        ):

            #
            # Public health endpoint
            #
            if self.path == (
                "/api/health"
            ):

                return self.send_json(
                    {
                        "status": "ok",
                        "service": "smeagol"
                    }
                )


            if not self.require_view_auth():
                return


            if self.path == (
                "/api/state"
            ):

                return self.send_json(
                    receiver.snapshot()
                )


            if self.path == (
                "/api/course"
            ):

                return self.send_json(
                    course
                )


            return super().do_GET()


        # ----------------------------------------------------
        # POST
        # ----------------------------------------------------

        def do_POST(
            self
        ):

            if self.path != (
                "/api/packet"
            ):

                return self.send_json(
                    {
                        "error":
                            "Not found"
                    },
                    404
                )


            #
            # Raspberry Pi:
            # X-Smeagol-API-Key
            #
            # Browser test:
            # same-origin + viewer auth
            #
            api_authorized = (
                valid_api_key(
                    self
                )
            )


            browser_authorized = (
                same_origin(
                    self
                )
                and
                valid_view_auth(
                    self
                )
            )


            if (
                API_KEY
                and
                not api_authorized
                and
                not browser_authorized
            ):

                return self.send_json(
                    {
                        "error":
                            "Unauthorized"
                    },
                    401
                )


            try:

                length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )


                if not (
                    0
                    < length
                    <= 8192
                ):

                    raise ValueError(
                        "Request body must be 1..8192 bytes"
                    )


                data = json.loads(
                    self.rfile.read(
                        length
                    )
                )


                if (
                    not isinstance(
                        data,
                        dict
                    )
                    or
                    not isinstance(
                        data.get(
                            "packet"
                        ),
                        str
                    )
                ):

                    raise ValueError(
                        "packet string is required"
                    )


                source = (
                    "demo"
                    if
                    data.get(
                        "source"
                    )
                    == "demo"
                    else
                    "http"
                )


                return self.send_json(

                    receiver.receive(

                        data[
                            "packet"
                        ],

                        source,

                        data.get(
                            "base_received_epoch"
                        ),

                        data.get(
                            "base_interval_s"
                        )

                    )

                )


            except (
                ValueError,
                UnicodeError,
                json.JSONDecodeError
            ) as error:

                return self.send_json(
                    {
                        "error":
                            str(
                                error
                            )
                    },
                    400
                )


            except OSError as error:

                return self.send_json(
                    {
                        "error":
                            "Log write error: "
                            + str(
                                error
                            )
                    },
                    500
                )


    return Handler


# ============================================================
# HTTP SERVER
# ============================================================

class SmeagolHTTPServer(
    ThreadingHTTPServer
):

    allow_reuse_address = True


# ============================================================
# MAIN
# ============================================================

def main():

    #
    # Cloud environment defaults
    #
    env_port = int(
        os.environ.get(
            "PORT",
            "8765"
        )
    )


    env_host = (
        os.environ.get(
            "HOST",
            "0.0.0.0"
        )
    )


    env_log_dir = (
        os.environ.get(
            "SMEAGOL_LOG_DIR",
            ""
        ).strip()
    )


    parser = (
        argparse.ArgumentParser(
            description=__doc__
        )
    )


    parser.add_argument(
        "--port",
        type=int,
        default=env_port
    )


    parser.add_argument(
        "--host",
        default=env_host
    )


    parser.add_argument(
        "--kml",
        type=Path,
        default=(
            ROOT
            / "data"
            / "JRC2SR26 Leg1.kml"
        )
    )


    parser.add_argument(
        "--serial",
        help="COM3 or /dev/ttyUSB0"
    )


    parser.add_argument(
        "--baud",
        type=int,
        default=115200
    )


    parser.add_argument(
        "--log-dir",
        type=Path,
        default=(
            Path(
                env_log_dir
            )
            if env_log_dir
            else
            ROOT
            / "logs"
        )
    )


    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=3.0
    )


    args = (
        parser.parse_args()
    )


    if (
        not math.isfinite(
            args.gap_seconds
        )
        or
        args.gap_seconds
        <= 0
    ):

        parser.error(
            "--gap-seconds must be positive and finite"
        )


    course = load_course(
        args.kml
    )


    receiver = Receiver(
        args.log_dir,
        args.gap_seconds
    )


    print(
        f"CSV log: "
        f"{receiver.csv_path}"
    )

    print(
        f"Gap log: "
        f"{receiver.gaps_path}"
    )


    if API_KEY:

        print(
            "Packet API authentication: ENABLED"
        )

    else:

        print(
            "Packet API authentication: DISABLED"
        )


    if (
        VIEW_USER
        and
        VIEW_PASSWORD
    ):

        print(
            "Monitor viewer authentication: ENABLED"
        )

    else:

        print(
            "Monitor viewer authentication: DISABLED"
        )


    stop = (
        threading.Event()
    )


    #
    # Optional direct serial input
    #
    if args.serial:

        try:

            import serial  # noqa: F401

        except ImportError:

            parser.error(
                "Serial input requires pyserial"
            )


        threading.Thread(
            target=serial_lines,
            args=(
                args.serial,
                args.baud,
                receiver,
                stop
            ),
            daemon=True
        ).start()


    server = SmeagolHTTPServer(
        (
            args.host,
            args.port
        ),
        make_handler(
            receiver,
            course
        )
    )


    print(
        f"Smeagol listening on "
        f"{args.host}:{args.port}"
    )

    print(
        f"Course features: "
        f"{len(course['features'])}"
    )


    try:

        server.serve_forever()


    except KeyboardInterrupt:

        pass


    finally:

        stop.set()

        server.server_close()


if __name__ == "__main__":

    main()