TF ?= terraform
EVIDENCE ?= evidence.local.json
.PHONY: check init plan apply verify
check:
	$(TF) fmt -check
	$(TF) init -backend=false -input=false
	$(TF) validate
	$(TF) -chdir=../../modules/cost/aws init -backend=false -input=false
	$(TF) -chdir=../../modules/cost/aws test
init:
	@test -n "$(BACKEND_CONFIG)" || (echo 'Set BACKEND_CONFIG'; exit 1)
	$(TF) init -input=false -backend-config="$(BACKEND_CONFIG)"
plan:
	$(TF) plan -input=false -out=apply.tfplan
apply:
	$(TF) apply -input=false apply.tfplan
verify:
	python3 verify.py --email "$(EMAIL)" --output "$(EVIDENCE)"
