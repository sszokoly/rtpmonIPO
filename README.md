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

`--rotate COUNT` controls how many dated database archives are retained
(default: `7`), excluding the current database. Immediately after each rotation,
the newest `COUNT` archives are kept and older archives for that database are
deleted. With daily traffic, this retains the previous seven days plus today.
`--rotate 0` removes all dated archives after rotation. For example:

```bash
uv run --extra db -m rtpmonIPO --db rtpsummary.db --rotate 14 192.0.2.10
```

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

### Automatic connection recovery

The CLI monitors each configured IPO independently. It retries initial
connection/startup failures and reconnects after a remote close or detected
stream failure, while healthy IPO connections continue uninterrupted.
Retry delays are **1, 2, 4, 8, 16, 32, then 60 seconds**, capped at 60 seconds
indefinitely. The delay resets to 1 second after complete successful startup,
even when the IPO has no calls. Each delay starts after the previous attempt's
resources have been closed.

Every attempt uses the same resolved credentials, protocol/port, TLS policy,
startup timeout, and keepalive interval, and reapplies the H323Evt-only startup
filter. The CLI stays running and retrying even if **all IPOs are offline**;
it no longer exits merely because no IPO is connected. Direct `IpoClient`
library calls retain single-session behavior and do not reconnect automatically.

Connection-success messages and summaries go to standard output. Disconnects
and failed attempts identify the affected endpoint and next retry delay on
standard error. The summary header prints only once per invocation. Recovered
sessions retain verbose/bad-only filtering and the same database writer,
including daily rotation and archive retention.

Calls missed during an outage are not replayed or synthesized. Local invalid
configuration, formatting, output, or database errors are fatal: the CLI stops
all monitoring, cleans up, and exits nonzero rather than retrying a connection
to hide a processing failure.

Remote authentication and TLS failures are retried under the same policy;
certificate verification is never relaxed automatically. Correcting credentials
or connection settings requires restarting the CLI. Reconnection starts when
the transport or existing keepalive handling detects a failure: there is no new
keepalive-acknowledgement timeout, so a silent network blackhole may take time
to detect.

Press **Ctrl+C** to stop. Connecting, streaming, and retry waits are cancelled
promptly, and sessions, keepalive tasks, and the shared database are closed
before exiting with status 130.

### Example

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
