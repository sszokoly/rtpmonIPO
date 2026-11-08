"""Public result records."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class RtpSummary:
    timestamp: str
    ipo_timestamp: str
    sess: str
    local_ip: str
    local_rtp_port: int
    remote_ip: str
    remote_rtp_port: int
    codec: str
    pktsz: int
    rfc2833: str
    age: int
    sent: int
    recv: int
    rtpdelay: int
    jitter: int
    loss: int
    remotejitter: int
    remoteloss: int

    def to_dict(self) -> dict[str, str | int]:
        """Return a fresh dictionary suitable for serialization."""
        return asdict(self)
