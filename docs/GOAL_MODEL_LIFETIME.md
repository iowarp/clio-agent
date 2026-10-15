# Goal iteration and model connections

The goal sequence is work, then an awaited completion check, then the next work
turn. These stages do not intentionally run as three simultaneous model requests.
An idle conversation also does not itself issue a model request.

The dynamic agent builders create a fresh LM for each forward so rotated
credentials take effect. The goal checker can create its own caller/Fleet LM.
These operation-owned clients now close their HTTP pools explicitly when the
forward or check ends, including failures, timeout and cancellation. Connections
can still be reused between model steps within one forward. History and usage
remain available after pool cleanup.

Borrowed session and Fleet clients retain their established lifetime. The cleanup
scope owns only factory instances constructed within that operation; a late
credential resolver returning after cancellation closes its newly created client
without dispatching work.

The regression tests use real local HTTP connections and count both active
requests and open sockets. Twenty-one sequential checks previously retained
twenty-one idle sockets when references were kept to remove garbage-collection
timing from the test. The corrected path closes owned pools deterministically.
An inherited client can retain one reusable idle socket while its active request
count is zero. This distinction matters: an open keep-alive connection does not
by itself prove an inference request remains active.

The reported AMSC `max_parallel_requests` 429 does not expose the gateway's
individual reservations. This fix addresses the confirmed client-lifetime gap;
its relationship to that protected gateway's count requires gateway-side evidence
or a fresh run on the affected machine. No concurrency cap or retry policy changes
are part of this correction.
