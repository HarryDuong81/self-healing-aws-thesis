# Makefile — Self-Healing Infrastructure Thesis
# Thesis reference: §4.1 Pre-flight, §8 Testing
#
# Targets:
#   make pre-flight      All pre-campaign checks (§4.1)
#   make unit            pytest tests/unit/
#   make integration     pytest tests/integration/ (needs INTEGRATION_TESTS=1 or live AWS)
#   make verify-pins     Diff installed toolchain versions against .toolchain
#   make lint            HCL + Ansible + Python lint only
#   make clean           Remove generated artefacts

.DEFAULT_GOAL := help
SHELL         := /bin/bash
PYTHON        := python3

# ── Directories ───────────────────────────────────────────────────────────────

TF_BOOTSTRAP  := terraform/bootstrap
TF_TARGETS    := terraform/targets
TF_BASELINE   := terraform/baseline
ANSIBLE_DIR   := ansible
PLAYBOOKS     := $(ANSIBLE_DIR)/playbooks
OBS_COMPOSE   := observability/docker-compose.yml
RESULTS_DIR   := results

# ── Help ──────────────────────────────────────────────────────────────────────

.PHONY: help
help:
	@echo "Self-Healing Thesis — available make targets:"
	@echo ""
	@echo "  pre-flight       All pre-campaign checks (HCL lint, Ansible lint, pytest unit, compose config)"
	@echo "  unit             Run orchestrator unit tests"
	@echo "  integration      Run end-to-end integration tests (requires live AWS; set INTEGRATION_TESTS=1)"
	@echo "  verify-pins      Check installed toolchain versions against .toolchain"
	@echo "  lint             HCL + Ansible + Python lint only"
	@echo "  clean            Remove generated artefacts (results/, __pycache__, .pytest_cache)"
	@echo ""

# ── Pre-flight (§4.1) ─────────────────────────────────────────────────────────

.PHONY: pre-flight
pre-flight: verify-pins lint unit compose-lint
	@echo ""
	@echo "Pre-flight PASSED. Safe to launch campaign."

# ── Lint ──────────────────────────────────────────────────────────────────────

.PHONY: lint
lint: tf-lint ansible-lint py-lint

.PHONY: tf-lint
tf-lint:
	@echo "==> Terraform fmt check (bootstrap)"
	terraform -chdir=$(TF_BOOTSTRAP) fmt -check -recursive
	@echo "==> Terraform validate (bootstrap)"
	terraform -chdir=$(TF_BOOTSTRAP) validate
	@echo "==> Terraform fmt check (targets)"
	terraform -chdir=$(TF_TARGETS) fmt -check -recursive
	@echo "==> Terraform validate (targets)"
	terraform -chdir=$(TF_TARGETS) validate
	@echo "==> Terraform fmt check (baseline)"
	terraform -chdir=$(TF_BASELINE) fmt -check -recursive
	@echo "==> Terraform validate (baseline)"
	terraform -chdir=$(TF_BASELINE) validate

.PHONY: ansible-lint
ansible-lint:
	@echo "==> Ansible lint"
	ANSIBLE_CONFIG=$(ANSIBLE_DIR)/ansible.cfg ansible-lint $(ANSIBLE_DIR)/
	@echo "==> Ansible syntax check (all playbooks)"
	ANSIBLE_CONFIG=$(ANSIBLE_DIR)/ansible.cfg ansible-playbook $(PLAYBOOKS)/*.yml --syntax-check

.PHONY: py-lint
py-lint:
	@echo "==> Ruff lint (orchestrator)"
	ruff check orchestrator/

# ── Unit tests ────────────────────────────────────────────────────────────────

.PHONY: unit
unit:
	@echo "==> pytest unit tests"
	$(PYTHON) -m pytest tests/unit/ -v

# ── Integration tests ─────────────────────────────────────────────────────────

.PHONY: integration
integration:
	@echo "==> pytest integration tests (INTEGRATION_TESTS=1)"
	INTEGRATION_TESTS=1 $(PYTHON) -m pytest tests/integration/ -v --timeout=600

# ── Docker Compose lint ───────────────────────────────────────────────────────

.PHONY: compose-lint
compose-lint:
	@echo "==> Docker Compose config lint"
	docker compose -f $(OBS_COMPOSE) config -q

# ── Verify pins (§3) ────────────────────────────────────────────────

.PHONY: verify-pins
verify-pins:
	@echo "==> Verifying toolchain pins against .toolchain"
	@$(PYTHON) scripts/verify_pins.py .toolchain

# ── Campaign helpers ──────────────────────────────────────────────────────────

.PHONY: steady-state
steady-state:
	$(PYTHON) experiments/trials/wait_steady_state.py --window 60

.PHONY: host-baseline
host-baseline:
	$(PYTHON) experiments/trials/capture_host_baseline.py \
		--duration 600 --interval 15 \
		--out s3://its-thesis-data/baseline/host_baseline.json

# ── Analysis ──────────────────────────────────────────────────────────────────

CAMPAIGN ?= results/mttr_log.jsonl

.PHONY: analysis
analysis: analysis-descriptive analysis-wilcoxon analysis-h3 analysis-overhead

.PHONY: analysis-descriptive
analysis-descriptive:
	$(PYTHON) analysis/01_descriptive.py --campaign $(CAMPAIGN) --out results/01_descriptive.json

.PHONY: analysis-wilcoxon
analysis-wilcoxon:
	$(PYTHON) analysis/02_wilcoxon.py --campaign $(CAMPAIGN) --out results/02_wilcoxon.json

.PHONY: analysis-h3
analysis-h3:
	$(PYTHON) analysis/h3_upper_bound.py --campaign $(CAMPAIGN) --system S1 --out results/03_h3_upper_bound.json

.PHONY: analysis-overhead
analysis-overhead:
	@echo "Running RQ3 overhead analysis — requires --baseline and --cadvisor args."
	@echo "Use: make analysis-overhead BASELINE=s3://... CADVISOR=http://..."
	$(PYTHON) analysis/04_overhead.py \
		--baseline $${BASELINE:-results/host_baseline.json} \
		--cadvisor $${CADVISOR:-http://localhost:8080} \
		--out results/04_overhead.json

# ── Clean ─────────────────────────────────────────────────────────────────────

.PHONY: clean
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache   -exec rm -rf {} + 2>/dev/null || true
	rm -f $(RESULTS_DIR)/01_descriptive.json \
	      $(RESULTS_DIR)/02_wilcoxon.json \
	      $(RESULTS_DIR)/03_h3_upper_bound.json \
	      $(RESULTS_DIR)/04_overhead.json
	@echo "Cleaned."
