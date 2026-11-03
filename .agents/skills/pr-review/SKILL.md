---
name: pr-review
description: Collect revision-bound review packets for one or more platform PRs, assess team contracts and validation evidence, and address findings without substituting thread resolution for human approval.
---

# Platform PR review

Use this skill when reviewing platform PRs, responding to review findings, or checking whether a PR may enter GitHub's native auto-merge flow. Read [the review playbook](../../../docs/review-playbook.md) first. The relative link is from the skill directory; the repository path is `docs/review-playbook.md`.

1. Read the current task, `AGENTS.md`, `docs/design.html`, `docs/tasks.md`, and relevant development/governance documents. Compare the base contract with proposed changes. Identify affected task IDs and both primary and secondary owners from the current role table. Slack/huddle comments supply context; they do not silently replace a later authoritative assignment.
2. Collect a packet for all requested PRs:

   ```sh
   python3 scripts/review_packet.py 73 74 --output-dir /absolute/path/to/private-review-packets
   ```

   The command only reads GitHub. Exit 0 means collection completed; it does **not** mean approved, CI passed, ready to merge, or reviewed. An API failure, unsupported/partial pagination, or head/base change produces `UNKNOWN` and exit 1. Read `packet.json` as well as `packet.md`.
3. Read every linked original comment and the exact base/head diff. The packet deliberately omits comment bodies, patches, logs, and secrets. Create `assessment.md` beside the packet. Record each finding's disposition, exact assessed head, rationale, evidence, and remaining owner/action. Do not overwrite older packets or claim a checklist was performed merely because it exists.
4. Review correctness and failure paths, authoritative contracts, simple ownership/module boundaries, CI selection/dependencies, security/guardrails, costs, and applicable local/cloud evidence. Use the playbook checklist. Run relevant verification; do not run every cloud procedure for a prose-only change.
5. Fix authorized findings on the PR's feature branch. A new head invalidates the previous assessment for changed behavior. Recollect the packet and rerun affected verification. Review requests go to affected primary and secondary owners; merging requires at least one qualifying human approval, not every invited reviewer's approval.
6. A fixed thread may be resolved when the current head matches the assessed head and the ledger contains sufficient evidence. This is a separate, explicit GitHub action outside this script. An unresolved issue remains open; an unknown or disputed finding goes to its owner. Never fabricate a human approval, dismiss a change request to make a gate pass, or treat a bot review as human approval.
7. Before any merge action, refresh GitHub's review decision, head/base, active rules, checks, and outstanding change requests. Follow GitHub's native rules and auto-merge. Thread resolution is **not** reviewer approval. The packet is not a merge engine and never grants merge permission.

Keep packet output outside the repository unless a scrubbed evidence artifact is intentionally selected for commit. Do not put credential values, raw CI logs, private comment bodies, or cloud account exports into the packet or assessment. Refer to bounded commands, test results, immutable artifacts, and cloud receipt identifiers instead.
