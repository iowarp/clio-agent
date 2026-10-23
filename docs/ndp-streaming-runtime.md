# NDP Kafka streams through streaming_v2

CLIO uses the NDP `streaming_v2` Python SDK for cataloged Kafka sources. HTTP
catalog and service requests continue to use the Endpoint's advertised API.
No custom HTTP consumer interface is required on the Endpoint.

The SDK is a separate consuming-application dependency. Install it in a dedicated
Python environment on the connected CLIO computer or the Endpoint connection's
SSH target. It is not installed automatically by setting up an NDP Endpoint.
The qualified upstream revision is
`sci-ndp/streaming_v2@2ba8b56baf82ee25e4947a705e044a9b0eed8001` (0.1.0),
with Python 3.13, `ndp-ep` 0.9.0, `kafka-python` 3.0.11 and `aiokafka` 0.14.0.
The SDK is not currently published as `scidx-streaming-v2` on PyPI. Keep its
installation explicit rather than making all CLIO installations depend on a
private or unpublished package. Source access must be available to the operator.

## Private runtime configuration

Create a private JSON profile on the runtime computer and restrict it to its
operator. It binds the credentials to the reviewed Endpoint URL:

```json
{
  "endpoint_url": "http://127.0.0.1:8002",
  "kafka": {
    "host": "broker.example.org",
    "port": 9093,
    "username": "YOUR_BROKER_USER",
    "password": "YOUR_BROKER_PASSWORD",
    "security_protocol": "SASL_SSL",
    "sasl_mechanism": "SCRAM-SHA-512"
  }
}
```

NDP account authentication and Kafka authentication are separate. CLIO sends the
saved NDP account token over private process stdin; it never puts credentials in
operation arguments, shell command arguments or capture receipts. Broker secrets
stay in the private profile on the selected computer. The profile's Endpoint URL
must match the approved connection exactly. Optional `kafka.source_aliases`
entries, each with `host` and `port`, can identify additional approved addresses
for the same broker. They are operator configuration, not agent-supplied aliases.

In **Attach → NDP → Endpoints & streams**, edit the connection's **Streaming
runtime** and select that environment's Python executable and the private
profile's path. These paths are on the SSH target when the connection uses SSH.
The agent connector accepts the same `streaming_runtime` object with `python`
and `profile_path`. An existing same-owner configuration for the exact same
Endpoint/SSH/authentication destination can be reused in a new workspace.

## Reader operations and retained evidence

The ordinary operation interface advertises these calls with method `SDK`,
separately from observed HTTP routes:

| Operation | Behavior |
| --- | --- |
| `SDK streaming_v2.open` | Resolve the selected catalog resource ID and record the earliest or latest retained Kafka positions. |
| `SDK streaming_v2.read` | Capture a finite batch, retain the actual records in workspace resource custody and advance CLIO's checkpoint. |
| `SDK streaming_v2.list` | List this workspace's readers and their state. |
| `SDK streaming_v2.stop` | Stop future captures while preserving positions. |
| `SDK streaming_v2.resume` | Allow captures from CLIO's saved positions. |

The selected catalog resource must identify a Kafka source on the configured
broker. This prevents forwarding broker credentials to a catalog-selected,
unapproved server. Reader IDs are bound to the workspace, principal, Endpoint,
runtime configuration and source topic. Editing the destination or runtime
requires opening a new reader. Recorded captures remain available.

Each read is bounded by the ordinary request budget, at most 1,000 records and a
2 MiB capture. Polling runs in the SDK worker, outside the API event loop and
outside ledger write transactions. Captures retain topic, partition, offset,
Kafka timestamp, exact source-byte hash and decoded source payload. Non-JSON
values retain their bytes as base64; tombstones remain distinguishable.

CLIO buffers messages returned by SDK initialization polls instead of losing
them. It uses explicit Kafka positions rather than the SDK's replaying
history-mode `resume()` method. Retrying the same cursor returns the same saved
capture, including empty polls. A later read without a cursor uses the reader's
current checkpoint. Successful captures commit their receipt and position
together; stopping/resuming during an in-flight read prevents that read from
overwriting the newer control state. No broker offsets are committed on behalf
of another consumer group.

Expired retention, reset offsets, unavailable partitions and changed topic
identity are errors, not invitations to silently restart. Topic-identity
validation uses the identity exposed by the broker's topic metadata; offset
bounds are also checked. This cannot recover records the broker no longer retains.
The current capture validator requires contiguous partition offsets. Compacted
topics or other broker histories with intentional offset gaps require a separate
continuation policy; CLIO currently rejects those gaps without advancing its
checkpoint.

Stopping a reader never stops its independent source producer. A catalog resource
does not start a producer, and a consumer's memory buffer is not a historical
archive. Analyze the captured provider timestamps, provenance and units before
describing the records as scientific measurements.
