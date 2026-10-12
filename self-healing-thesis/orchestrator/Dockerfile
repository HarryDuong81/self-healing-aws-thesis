# =============================================================================
# Dockerfile — Flask MAPE-K Orchestrator
# Port: 9094  (Thesis Table 4.3, Listing 4.9 — do NOT change to 5000)
# Build:  docker compose build orchestrator   (runs on monitoring server)
# Python: 3.12.3 pinned in Thesis Ch4 Table 4.2
# =============================================================================

FROM python:3.12-slim

LABEL maintainer="Duong Tat Hung"
LABEL org.opencontainers.image.title="self-healing-orchestrator"
LABEL org.opencontainers.image.description="MAPE-K Analyse+Execute engine"

# ── System dependencies ────────────────────────────────────────────────────────
# curl:  healthcheck probe (/healthz endpoint — Thesis §3.7.2 Stage E3)
# git:   ansible-galaxy collection install
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ────────────────────────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && ansible-galaxy collection install community.aws community.general 2>/dev/null || true

# ── Application source ─────────────────────────────────────────────────────────
COPY app.py            .
COPY classifier.py     .
COPY plan.py           .
COPY execute.py        .
COPY mttr_logger.py    .
COPY health_checker.py .
COPY routing.yaml      .    # ConcernKnowledge artefact — Thesis §3.5.2, §4.A.4

# ── Ansible config within container ───────────────────────────────────────────
ENV ANSIBLE_HOST_KEY_CHECKING=False
ENV ANSIBLE_STDOUT_CALLBACK=yaml
ENV ANSIBLE_RETRY_FILES_ENABLED=False

# ── Flask / OpenTelemetry ──────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production
ENV OTEL_SERVICE_NAME=self-healing-orchestrator
ENV OTEL_PYTHON_LOG_CORRELATION=true

# ── Runtime directories (volume-mounted) ─────────────────────────────────────
RUN mkdir -p /app/ansible /opt/orchestrator/results

# Port 9094 — Thesis Table 4.3, Listing 4.9.
# Alertmanager routes to http://orchestrator:9094/alert.
# Prometheus scrapes http://localhost:9094/metrics.
EXPOSE 9094

# Health probe using /healthz — Thesis §3.7.2 Stage E3 fast internal probe.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:9094/healthz || exit 1

# Single Gunicorn worker — serialises MTTR timestamps (Thesis §4.4.1).
# No concurrent remediations: T_detect → T_classify → T_remediate → T_validate
# must form a contiguous timeline for each trial.
# --timeout 2700: handle_alert() dispatches _run_mape_k() SYNCHRONOUSLY inline
# in the request handler, so this is a hard ceiling on per-alert MAPE-K
# duration and must exceed the worst-case SUM of every bounded wait one
# dispatch can make — 2460s for instance_failure (raised from 2280s when
# app.py's ObservabilityChecker forwarding-check window went 120s -> 300s,
# 2026-09-14), up to 1895s for the retry-equipped Ansible classes. The
# earlier 960s was below all six.
# See app.py's module docstring for the full derivation and for why a
# breach is worse than a lost trial (orphaned children, wiped lease and
# circuit-breaker state, 5m Alertmanager re-delivery into a fresh worker).
# Keep this value equal to orchestrator.service.j2's; a unit test asserts it.
CMD ["gunicorn", "--bind", "0.0.0.0:9094", "--workers", "1", \
     "--worker-class", "sync", "--timeout", "2700", "app:app"]
