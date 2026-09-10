# hermess-metrics

A Hermes plugin that ships agent-loop telemetry to Axiom as OpenTelemetry
traces, metrics, and logs.

Hermes observer hooks run inline on the agent loop, so everything this plugin
collects is handed off to a background exporter rather than sent from the hook
itself.

## Install

Either copy `hermess_metrics/` into `~/.hermes/plugins/hermess-metrics/`, or
`pip install hermess-metrics` and let the `hermes_agent.plugins` entry point
find it. Then enable it:

```sh
hermes plugins enable hermess-metrics
```

## Configure

| Variable                     | Purpose                                  |
| ---------------------------- | ---------------------------------------- |
| HERMES_AXIOM_TOKEN           | Axiom API token with ingest rights       |
| HERMES_AXIOM_DOMAIN          | Deployment host, default api.axiom.co    |
| HERMES_AXIOM_TRACES_DATASET  | Dataset receiving traces                 |
| HERMES_AXIOM_LOGS_DATASET    | Dataset receiving logs                   |
| HERMES_AXIOM_METRICS_DATASET | Dataset receiving metrics                |
| HERMES_AXIOM_DEBUG           | Verbose plugin logging                   |

Axiom requires a dedicated dataset per signal, and a metrics dataset must be
created with the OpenTelemetry metrics kind.
