# ──────────────────────────────────────────────────────────────────────────────
# Digilux Honeywell — Account Deletion Feature
# Infrastructure deployment via Terraform
#
# Usage:
#   make deploy ENV=digilux          # full init + plan + apply
#   make deploy ENV=honeywell
#
#   make init    ENV=digilux         # initialise backend only
#   make plan    ENV=digilux         # plan and save to .builds/<env>.tfplan
#   make apply   ENV=digilux         # apply the saved plan
#   make output  ENV=digilux         # print all outputs (API URL, key, etc.)
#   make destroy ENV=digilux         # ⚠️  destroy (deletion_audit + archive bucket are protected)
#
#   make fmt                         # format all .tf files in place
#   make validate ENV=digilux        # validate HCL syntax
#
# ENV defaults to digilux if not specified.
# ──────────────────────────────────────────────────────────────────────────────

ENV      ?= digilux
TF       := terraform
INFRA    := infra
BUILDS   := $(INFRA)/.builds

.PHONY: init plan apply deploy destroy output fmt validate _builds

# ── Ensure .builds/ directory exists ──────────────────────────────────────────
_builds:
	@mkdir -p $(BUILDS)

# ── init ──────────────────────────────────────────────────────────────────────
init: _builds
	@echo ""
	@echo "==> [$(ENV)] Initialising Terraform backend"
	@echo ""
	$(TF) -chdir=$(INFRA) init \
	    -reconfigure \
	    -backend-config=envs/$(ENV)-backend.hcl

# ── plan ──────────────────────────────────────────────────────────────────────
plan: init
	@echo ""
	@echo "==> [$(ENV)] Planning"
	@echo ""
	$(TF) -chdir=$(INFRA) plan \
	    -var-file=envs/$(ENV).tfvars \
	    -out=.builds/$(ENV).tfplan

# ── apply ─────────────────────────────────────────────────────────────────────
apply:
	@echo ""
	@echo "==> [$(ENV)] Applying"
	@echo ""
	$(TF) -chdir=$(INFRA) apply .builds/$(ENV).tfplan

# ── deploy = init + plan + apply ──────────────────────────────────────────────
deploy: plan apply
	@echo ""
	@echo "==> [$(ENV)] Deploy complete"
	@echo ""
	@$(MAKE) output ENV=$(ENV)

# ── output ────────────────────────────────────────────────────────────────────
output:
	@echo ""
	@echo "==> [$(ENV)] Outputs"
	@echo ""
	$(TF) -chdir=$(INFRA) output

# ── destroy ───────────────────────────────────────────────────────────────────
# Note: deletion_audit DynamoDB table and archive S3 bucket have prevent_destroy=true.
# They will NOT be deleted even if you run this target.
destroy:
	@echo ""
	@echo "==> [$(ENV)] WARNING: destroying resources"
	@echo ""
	$(TF) -chdir=$(INFRA) destroy \
	    -var-file=envs/$(ENV).tfvars

# ── fmt ───────────────────────────────────────────────────────────────────────
fmt:
	$(TF) -chdir=$(INFRA) fmt -recursive

# ── validate ──────────────────────────────────────────────────────────────────
validate: init
	$(TF) -chdir=$(INFRA) validate
