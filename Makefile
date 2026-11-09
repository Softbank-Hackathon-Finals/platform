PYTHON ?= python3

.PHONY: tasks-status tasks-status-remote tasks-preview tasks-start tasks-tick tasks-test

tasks-status:
	$(PYTHON) scripts/sync_tasks.py status

tasks-status-remote:
	gh workflow run sync-tasks.yml -R Softbank-Hackathon-Finals/platform --ref develop -f command=status

tasks-preview:
	$(PYTHON) scripts/sync_tasks.py pull --dry-run

tasks-start:
	@test -n "$(TASK)" || (echo 'Set TASK=T1'; exit 1)
	$(PYTHON) scripts/sync_tasks.py start "$(TASK)"

tasks-tick:
	@test -n "$(TASK)" -a -n "$(STEP)" || (echo 'Set TASK=T1 STEP=2 after merge and verification'; exit 1)
	$(PYTHON) scripts/sync_tasks.py tick "$(TASK)" "$(STEP)"

tasks-test:
	$(PYTHON) -m unittest discover -s scripts/tests -v
