# rtpmonIPO

Asynchronous Avaya IP Office SysMonitor client that captures decoded H323 RTP
end summaries.

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

Install `rtpmonipo[db]` to enable SQLite storage, then use
`--db rtpsummary.db` to save printed summaries. When a summary
arrives on a later local calendar day, existing records are moved to an archive
such as `rtpsummary_20261108.db`, and the active database starts the new day.
The `--bad-only` filter also applies to stored records.

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
