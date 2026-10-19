# Performance analysis

Capture native events with `performance_start_trace` and `performance_stop_trace`.
Every recording is saved in full. Add `analyze=true` to append the actual DevTools
Trace Engine summary; manual stop inherits that option from start when omitted.
Recording state is released before analysis so another recording can begin.

```text
performance_start_trace(reload=true, auto_stop=true, analyze=true)
performance_analyze_trace(trace_path="/path/to/trace.json.gz")
performance_analyze_insight(insight_set_id="NAVIGATION_0", insight_name="LCPBreakdown")
```

The two analysis tools read saved files without opening Chrome. Omit `trace_path`
to use the latest recording. Choose insight IDs and names from the trace summary
or the complete JSON report. Detailed reports use the native DevTools formatter
for resource timings, causes, subparts and estimated savings.

`file_path` selects the complete JSON report; otherwise reports are retained under
`~/.nodriver-mcp/artifacts/performance`. `NODRIVER_ARTIFACTS_DIR` changes the artifact
root. `max_output_chars` bounds the inline preview, with an explicit truncation
notice. `timeout` bounds preparation and analysis in milliseconds (60000 by
default, 0 to disable). Timeout/cancellation terminates the analysis child process.
The source recording remains available after analysis errors.

## Engine runtime

The Python browser server uses official nodriver 0.50.6 and MCP SDK 2.3.0. Performance
analysis additionally uses Node.js 22.12+; `NODRIVER_NODE_PATH` can select a binary.
The first analysis downloads the published `chrome-devtools-mcp@1.11.0` archive
from npm, checks its fixed SHA-512, and extracts the unmodified engine and chunked
trace parser into a local cache. The cache includes the upstream license/notices.
A fresh engine process handles each analysis.

The default cache is `~/.cache/nodriver-mcp/trace-engine` (under `XDG_CACHE_HOME`
when set). Override it with `NODRIVER_TRACE_ENGINE_CACHE_DIR`. To run without a
package download, set `NODRIVER_DEVTOOLS_PACKAGE` to an extracted published 1.11.0
package containing `package.json` and `build/src`. Node is needed when requesting
analysis; ordinary browser tools and raw exports use the Python runtime.

## Observed metrics

Reports contain a `web_vitals` entry per native insight set/navigation. Times are
milliseconds; CLS is a dimensionless score. `lcp_ms` comes from the engine's LCP
model, `inp_ms` uses its high-percentile interaction, and `worst_interaction_ms`
retains the longest interaction used by the detailed native breakdown. `cls`
uses the largest native layout-shift session cluster. `fcp_ms` comes from native
page-load metrics. A missing observation is `null`, including INP when there was
no recorded interaction. These values describe activity captured in this trace.

The report also includes engine version, trace path/bounds, event count,
analysis duration, insight IDs, model states/savings and any model diagnostics.
JSON and gzip inputs are supported by the official chunked parser. The regression
fixture retains a known observed LCP of 129.211 ms and produces 19 native models.

## Verification

```sh
NODRIVER_MCP_INSTALL_TESTS=1 NODRIVER_MCP_LIVE_TESTS=1 \
NODRIVER_MCP_PERFORMANCE_TESTS=1 \
uv run --locked python -B -m unittest discover -s tests -v
```

The performance opt-in runs the actual Node engine and may prepare the pinned
cache. Coverage includes a portable upstream fixture, real Chrome navigation and
trusted input, MCP stdio calls, isolated installation of the packaged bridge,
output preservation, unavailable runtime and process cleanup.
