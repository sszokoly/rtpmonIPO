"""Binary-first parsing for the captured SysMonitor RTP-end layout."""

from __future__ import annotations

import ipaddress
import re
import struct
from datetime import UTC, datetime

from .models import RtpSummary

HEADER = struct.Struct("!IIII")
HEADER_SIZE = HEADER.size
H323_TEXT_OFFSET = 33
RTP_END_EVENT = 0x0233
RTP_END_MARKER = "RTP(END):".encode("utf-16-be")
SUMMARY_RE = re.compile(
    r"^!SESS (?P<sess>\S+): RTP\(END\): (?P<local>\S+) (?P<remote>\S+) (?P<metrics>.*)$"
)
REQUIRED_METRICS = {
    "codec": "codec",
    "pktsz": "pktsz",
    "rfc2833": "rfc2833",
    "age": "age",
    "sent": "sent",
    "recv": "recv",
    "rtdelay": "rtpdelay",
    "jitter": "jitter",
    "loss": "loss",
    "remotejitter": "remotejitter",
    "remoteloss": "remoteloss",
}
INTEGER_METRICS = frozenset(REQUIRED_METRICS.values()) - {"codec", "rfc2833"}


def is_rtp_end_candidate(payload: bytes) -> bool:
    """Return whether *payload* has the target header and encoded marker."""
    if (
        len(payload) < H323_TEXT_OFFSET
        or RTP_END_MARKER not in payload[H323_TEXT_OFFSET:]
    ):
        return False

    _, message_type, event_id, _ = HEADER.unpack_from(payload)
    return message_type == 2 and event_id == RTP_END_EVENT


def parse_rtp_end(payload: bytes, received_at: datetime) -> RtpSummary | None:
    """Parse a candidate RTP-end payload, rejecting malformed records safely."""
    if not is_rtp_end_candidate(payload):
        return None

    _, _, _, ipo_counter = HEADER.unpack_from(payload)
    try:
        text = _decode_h323_text(payload)
        match = SUMMARY_RE.fullmatch(text)
        if match is None:
            return None

        local_ip, local_port = _parse_endpoint(match["local"])
        remote_ip, remote_port = _parse_endpoint(match["remote"])
        metrics = _parse_metrics(match["metrics"])
        if metrics is None:
            return None
    except (UnicodeDecodeError, ValueError):
        return None

    received_utc = received_at.astimezone(UTC).replace(microsecond=0)
    return RtpSummary(
        timestamp=received_utc.isoformat(),
        ipo_timestamp=f"0x{ipo_counter:08x}",
        sess=match["sess"],
        local_ip=local_ip,
        local_rtp_port=local_port,
        remote_ip=remote_ip,
        remote_rtp_port=remote_port,
        codec=metrics["codec"],
        pktsz=metrics["pktsz"],
        rfc2833=metrics["rfc2833"],
        age=metrics["age"],
        sent=metrics["sent"],
        recv=metrics["recv"],
        rtpdelay=metrics["rtpdelay"],
        jitter=metrics["jitter"],
        loss=metrics["loss"],
        remotejitter=metrics["remotejitter"],
        remoteloss=metrics["remoteloss"],
    )


def _decode_h323_text(payload: bytes) -> str:
    return payload[H323_TEXT_OFFSET:].decode("utf-16-be").rstrip("\0\r\n")


def _parse_endpoint(value: str) -> tuple[str, int]:
    address, separator, port_text = value.rpartition("/")
    if not separator:
        raise ValueError("missing RTP port")
    ipaddress.ip_address(address)
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("invalid RTP port")
    return address, port


def _parse_metrics(text: str) -> dict[str, str | int] | None:
    metrics: dict[str, str | int] = {}
    for token in text.split():
        key, separator, value = token.partition("=")
        if not separator:
            continue
        field = REQUIRED_METRICS.get(key.lower())
        if field is None or field in metrics:
            continue
        if field == "codec":
            codec = re.fullmatch(r"(.+?)\(\d+\)", value)
            if codec is None or not codec[1]:
                return None
            metrics[field] = codec[1]
        elif field in INTEGER_METRICS:
            try:
                number = int(value)
            except ValueError:
                return None
            if number < 0:
                return None
            metrics[field] = number
        elif value:
            metrics[field] = value
        else:
            return None

    if set(metrics) != set(REQUIRED_METRICS.values()):
        return None
    return metrics
