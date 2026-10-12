# observability/

Configuration for the monitoring stack.  **Not everything here is deployed.**

The monitoring host is provisioned by `ansible/roles/monitoring_stack`.  Some
files in this directory are copied to the host verbatim; others have been
superseded by Jinja templates in that role and survive only as reference or as
lint targets.  The two sets have drifted before -- the deployed cAdvisor image
was pinned in one place and `latest` in the other -- so the split is recorded
here explicitly.

| File | Status | Deployed from |
|---|---|---|
| `prometheus/rules/fault_classes.yml` | **live** | copied verbatim to `prometheus/rules/alert_rules.yml` |
| `kibana/kibana.yml` | **live** | copied verbatim |
| `otelcol/config.yaml` | **live** | copied verbatim |
| `grafana/provisioning/dashboards/self-healing-dashboard.json` | **live** | copied verbatim |
| `docker-compose.yml` | reference / lint target | `templates/docker-compose.yml.j2` |
| `alertmanager/alertmanager.yml` | superseded | `templates/alertmanager.yml.j2` |
| `prometheus/prometheus.yml` | superseded | `templates/prometheus.yml.j2` |
| `logstash/pipeline/logstash.conf` | superseded | `templates/logstash.conf.j2` |
| `grafana/provisioning/datasources/prometheus.yml` | superseded | role task |
| `grafana/provisioning/dashboards/dashboards.yml` | superseded | role task |

Two consequences follow.

`make compose-lint` and CI validate `docker-compose.yml`, not the rendered
template, because `docker compose config` cannot parse Jinja.  A change to the
template is therefore not covered by that check.

`make verify-pins` compares the container-image entries in `.toolchain`
against `ansible/roles/monitoring_stack/defaults/main.yml`, which is where the
deployed versions actually come from.  It does not read this directory.
