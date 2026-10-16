# Interrupted model streams

An HTTP peer closing a chunked response without its terminal chunk produces
`connection closed mid-chunk`. That proves an interrupted transport, not the
reason for the close. A gateway idle timeout, upstream failure, proxy or network
disconnect require gateway logs to distinguish. A UI heartbeat does not keep
the gateway's model connection alive.

DSPy retries typed transient failures before forwarding any stream chunk.
After that boundary it refuses automatic replay. CLIO can recover more narrowly
for stateless OpenAI-compatible chat requests that propose only local function
tools, because the agent loop executes tools only after accepting the complete
model response.

- Before public answer text appears, a typed transport failure can restart the
  current generation. Previously completed tool calls and results remain in ARC
  and in the new request. They are not executed again by recovery.
- Interrupted reasoning remains in the transcript. A visible service notice
  closes that passage before the new attempt. Partial reasoning and incomplete
  tool proposals are not accepted as new context or executed.
- Retry backoff is 1s, 2s, then capped at 8s, bounded by
  `limits.lm_transient_retries` / `CLIO_LM_TRANSIENT_RETRIES` (default 2; 0 disables
  CLIO recovery). This budget is per interrupted generation; DSPy still owns its
  pre-stream retry budget. Cancellation is checked at most every 250ms during
  backoff, and newly arrived steers are included before the new request.
- After public answer text appears, the turn reports a clear provider error and
  preserves the partial transcript. It does not silently restart and duplicate
  an answer. Stateful SDK engines and provider-native tools keep their own
  recovery policies. Authentication, configuration, policy and arbitrary tool
  failures do not enter this recovery path.

Local qualification uses a real HTTP/SSE endpoint that closes chunked streams,
CLIO's LM factory, DSPy's native transport, the actual agent loop and ARC, and
real filesystem writes. A separate live transcript test checks ordering and
reload projection. These tests do not establish MAG's production timeout or
exercise inference on the protected Argonne computer.
