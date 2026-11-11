# rtpmonIPO

Avaya IP Office async system monitor client that captures and prints decoded RTP summaries
optionally saving them to a local SQLite database file as well.

For the CLI, set `IPO_USER` and `IPO_PASSWORD` in the environment. To load
these from a `.env` file, install the optional dotenv extra:

```bash
pip install 'rtpmonipo[dotenv]'
```

Prefer verified HTTPS when available. HTTP does not encrypt monitoring data;
`--insecure` disables TLS certificate verification. Avoid passing passwords via
`--ipo-passwd` when possible: command-line arguments may appear in process
listings and shell history.

IPO addresses can be passed on the command line or read from a UTF-8 file,
one address per line. Both forms can be combined:

```bash
uv run -m rtpmonIPO --file ipo-addresses.txt 192.0.2.10
```

The file can also contain CSV rows with optional per-IPO credentials:

```csv
ip,username,password,comment
192.0.2.1,Administrator,site-one-password,#Location1
192.0.2.2,monitor,"password,with,commas",#Location2
192.0.2.3
192.0.2.4,,site-four-password,#LocationA
192.0.2.5,monitor,,#LocationB
#192.0.2.6,Administrator,password,#Disabled
```

The `ip,username,password` or `ip,username,password,comment` header is optional.
The fourth column is an optional comment and is ignored. Rows whose first field
starts with `#` (after leading whitespace) are also ignored.
Each non-empty username or
password in a row overrides the corresponding `--ipo-user` / `--ipo-passwd`
setting for that IPO. Missing or empty fields fall back to the CLI settings,
then `IPO_USER` / `IPO_PASSWORD` (including `.env` when installed), then the
built-in defaults. Command-line IP addresses use those same fallback settings.
Blank lines are ignored; CSV quoting supports commas in credentials.

Install `rtpmonipo[db]` to enable SQLite storage, then use
`--db rtpsummary.db` to save printed summaries. When a summary
arrives on a later local calendar day, existing records are moved to an archive
such as `rtpsummary_20261108.db`, and the active database starts the new day.
The `--bad-only` filter also applies to stored records.

Connections use the captured H323Evt-only filter by default to reduce unrelated
monitor traffic. SysMonitor keepalives default to `KEEPALIVE_INTERVAL` (10
seconds). Override the interval for all CLI connections with:

```bash
uv run -m rtpmonIPO --file ipos.csv --keepalive-interval 20
```

The interval must be a finite, positive number of seconds. Library callers can
set `IpoClient(..., keepalive_interval=20)`. Longer intervals reduce keepalive
traffic, but must remain within the IPO's idle timeout.

## CLI and database example

With credentials already set in the environment, run:

```bash
uv run --extra db -m rtpmonIPO --db rtpsummary.db 192.0.2.10
```

Sample output (timestamps are shown in the host's local timezone):

```text
Connected to wss://192.0.2.10:443/sysmonitor/
Date/Time            LocalIP          LPort  RemoteIP         RPort  Codec    Sent    Recv
2026-11-08@14:33:22  192.0.2.10       46750  192.0.2.20       37186  Ulaw64K  513     53
```

The SQLite database contains an `rtp_summaries` table with all fields from
each `RtpSummary`. For example:

```bash
sqlite3 -header -column rtpsummary.db \
  'SELECT timestamp, local_ip, local_rtp_port, remote_ip, recv
   FROM rtp_summaries LIMIT 1;'
```

```text
timestamp                  local_ip    local_rtp_port  remote_ip   recv
2026-11-08T21:33:22+00:00  192.0.2.10  46750           192.0.2.20  53
```

## Library usage

Connect to an IP Office and process each decoded `RtpSummary` as it arrives:

```python
import asyncio

from rtpmonIPO import IpoClient


async def main() -> None:
    client = IpoClient(
        "http://10.92.32.3",
        username="Administrator",
        password="your-password",
    )
    await client.connect()
    try:
        async for summary in client.rtp_summaries():
            print(summary)
    finally:
        await client.close()


asyncio.run(main())
```

For an IP Office using HTTPS with a self-signed certificate, use an HTTPS URL
and pass `insecure=True`:

```python
client = IpoClient(
    "https://10.92.32.3",
    username="Administrator",
    password="your-password",
    insecure=True,
)
```

## Disclaimer
`rtpmonIPO` is an independent tool and is not affiliated with, developed by, endorsed by, or supported by any vendor. It is provided "as is," without warranties of any kind, express or implied. Users are responsible for evaluating its suitability and assume all risks associated with its use.
