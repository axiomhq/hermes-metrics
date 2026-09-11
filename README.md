# hermes-metrics

Ships [Hermes](https://hermes-agent.nousresearch.com) agent telemetry to
[Axiom](https://axiom.co) as OpenTelemetry traces, metrics and logs. Spend per
model, token burn, tool and provider failures, and whether the agent is stuck in
a loop.

![Dashboard](docs/dashboard.png)

## Quickstart

Install it into the same Python that runs `hermes`, enable it, and answer one
question:

```sh
pip install 'hermes-metrics[otlp] @ git+https://github.com/axiomhq/hermes-metrics'
hermes plugins enable hermes-metrics
hermes axiom setup
```

Setup asks where telemetry should go. Choose **1** and it provisions a fresh
Axiom org in seconds, creates the three datasets, mints a token that can only
write to them, and prints a claim link. Follow that link within a day or the org
and everything in it is deleted. Choose **2** to use an Axiom org you already
have, with a token holding `datasets:create`. Either way it then creates the
datasets, the seven monitors and the dashboard pictured above, writes the
settings, and you are done. `hermes axiom status` confirms it.

Along the way it asks for the alert thresholds that depend on your workload,
each with a default you can accept by pressing enter. Every threshold can also
be changed later in the Axiom console, or with `hermes axiom alerts`.

![Alerts](docs/alerts.png)

## What you get

**Metrics.** Spend in dollars charged from published rates at call time, tokens
split across input, output, cache read, cache write and reasoning, provider and
tool latency, turn shape, session outcomes, subagent fan-out, and the installed
tool and skill inventories with per-item call counts that read zero rather than
going missing. Plus the plugin's own queue accounting, so a quiet dashboard is
never ambiguous.

**Traces.** Sessions, turns, provider calls and tool executions as one trace,
using Axiom's generative AI conventions so it lights up in the AI views.

**Logs.** Failed provider and tool calls only, so the dataset stays a failure
feed rather than a duplicate of the traces.

## Configuration

Settings live in `~/.hermes/.env`, which Hermes loads at startup. `hermes axiom
setup` writes them for you; set them by hand only if you want to.

| Variable                             | Purpose                            | Default        |
| ------------------------------------ | ---------------------------------- | -------------- |
| HERMES_AXIOM_TOKEN                   | Axiom API token with ingest rights | required       |
| HERMES_AXIOM_DOMAIN                  | Deployment host                    | `api.axiom.co` |
| HERMES_AXIOM_TRACES_DATASET          | Dataset receiving traces           | unset          |
| HERMES_AXIOM_LOGS_DATASET            | Dataset receiving logs             | unset          |
| HERMES_AXIOM_METRICS_DATASET         | Dataset receiving metrics          | unset          |
| HERMES_AXIOM_REDACTION               | `metadata` only, `tools` or `full` | `metadata`     |
| HERMES_AXIOM_SERVICE_NAME            | Service name on every signal       | `hermes`       |
| HERMES_AXIOM_METRIC_INTERVAL_SECONDS | Export cadence in seconds          | 30             |
| HERMES_AXIOM_QUEUE_CAPACITY          | Events buffered before dropping    | 2048           |
| HERMES_AXIOM_MAX_CHARS               | Cap on any captured string         | 12000          |
| HERMES_AXIOM_CONTAINER_STATS         | Sample Docker sandboxes            | off            |
| HERMES_AXIOM_DEBUG                   | Verbose plugin logging             | off            |

One signal is enough; set only the datasets you want.

### Redaction

`HERMES_AXIOM_REDACTION` decides how much captured content leaves the machine.
The three levels are cumulative, and the default is the most private one.

| Level      | What it adds                                                 |
| ---------- | ------------------------------------------------------------ |
| `metadata` | Identifiers, models, token counts, durations, error classes. |
| `tools`    | Tool call arguments, tool results, tool error messages.      |
| `full`     | Model output and provider error text.                        |

Prompts and conversation history are never shipped, at any level.

Error text is the exception, and the level does not gate it. Provider errors and
tool error messages are attached to the failing trace span as its status
description at every level, `metadata` included, and the character cap does not
reach them. The logs dataset gates both fields by level; traces does not.

Four protections apply to the content the levels gate. Keys that name a
credential are replaced with `[redacted]`, strings are capped at
`HERMES_AXIOM_MAX_CHARS`, nesting deeper than eight levels is dropped, and
collections are truncated to their first 200 entries.

Credential masking matches on the key name, never the value. A secret that
arrives inside a tool result or a model message is not detected, so treat
`tools` and `full` as able to carry anything the agent saw.

An unrecognised value falls back to `metadata`, so a typo cannot widen what
ships.

## How it behaves

Hermes dispatches its observer hooks inline on the thread running your turn, so
everything here is handed to a background worker and the hook returns
immediately. The queue is bounded and drops rather than blocks; the drop count
is itself a metric. A missing dependency, an unreachable Axiom or a change in
the hook payload schema all degrade to a warning and a working agent.

## Licence

Dual licensed under either of Apache License 2.0 or MIT, at your option.
See `LICENSE-APACHE` and `LICENSE-MIT`.
