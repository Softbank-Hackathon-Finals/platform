#!/usr/bin/env bash
# docs/tasks.md의 작업(T번호) 할 일 목록을 GitHub 이슈 본문 "## 할 일"에 그대로 옮긴다.
# 사용법: issue-sync.sh T3 [tasks.md 경로]
set -euo pipefail

REPO=Softbank-Hackathon-Finals/platform
task="${1:?작업 번호 (예: T3)}"
file="${2:-$(git rev-parse --show-toplevel)/docs/tasks.md}"

# [T1]로 검색하면 [T10]도 걸리므로 제목 시작으로 거른다.
issue=$(gh issue list -R "$REPO" --state all --search "[$task] in:title" --limit 50 --json number,title \
  | jq -r --arg p "[$task]" 'map(select(.title|startswith($p)))|.[0].number // empty')
[ -n "$issue" ] || { echo "이슈를 못 찾음: [$task]" >&2; exit 1; }

todos=$(awk -v h="### [$task]" 'index($0,h)==1{p=1;next} /^### \[T/{p=0} p && /^- \[[ x]\]/' "$file")
[ -n "$todos" ] || { echo "tasks.md에 [$task] 할 일이 없음" >&2; exit 1; }

body=$(gh issue view "$issue" -R "$REPO" --json body -q .body)
new=$(TODOS="$todos" python3 -c '
import os, re, sys
body = sys.stdin.read()
todos = os.environ["TODOS"].strip()
pat = re.compile(r"(## 할 일\n)(?:- \[[ x]\][^\n]*\n?)+")
if not pat.search(body):
    sys.exit("이슈 본문에 \"## 할 일\" 목록이 없음")
print(pat.sub(lambda m: m.group(1) + todos + "\n", body, count=1), end="")
' <<<"$body")

gh issue edit "$issue" -R "$REPO" --body "$new" >/dev/null
echo "#$issue [$task] 할 일 $(grep -c '^- \[x\]' <<<"$todos")/$(wc -l <<<"$todos" | tr -d ' ') 반영"
