TF ?= terraform
PYTHON ?= python3
EVIDENCE ?= evidence.local.json

.PHONY: check init plan apply verify destroy-plan destroy

check:
	$(TF) fmt -check
	$(TF) init -backend=false -input=false
	$(TF) validate
	$(TF) test

init:
	@test -n "$(BACKEND_CONFIG)" || (echo 'Set BACKEND_CONFIG to an S3 backend JSON file'; exit 1)
	$(TF) init -input=false -backend-config="$(BACKEND_CONFIG)"

plan:
	$(TF) plan -input=false -out=apply.tfplan

apply:
	$(TF) apply -input=false apply.tfplan

verify:
	$(PYTHON) verify.py "$(EVIDENCE)"

destroy-plan:
	$(TF) plan -destroy -input=false -out=destroy.tfplan

destroy:
	$(TF) apply -input=false destroy.tfplan
