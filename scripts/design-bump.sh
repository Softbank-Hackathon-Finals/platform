#!/usr/bin/env bash
# 설계 문서 버전 올리기: 지금 버전을 design-history/로 옮기고, design.html의 버전 표기를 새 버전으로 바꾼다.
#   scripts/design-bump.sh v0.2
# main에서 실행하면 design-bump-<새 버전> 브랜치를 만들고, 작업 브랜치에서 실행하면 그 브랜치에서 그대로 한다
# (design.html을 이미 고치고 있어도 된다. 사본은 origin/main의 지금 버전으로 만든다).
# 하는 일
#   1. origin/main의 docs/design.html을 docs/design-history/design-<지금 버전>.html로 복사 (이후 수정하지 않는다)
#   2. 그 커밋에 design-<지금 버전> 태그를 달고 push (GitHub 비교 화면: compare/design-v0.1...main)
#   3. design-history/README.md 버전 목록에 줄 추가 ("바뀐 것" 칸은 사람이 채운다)
#   4. design.html의 버전 표기(머리말 상태 · 꼬리말)를 새 버전과 오늘 날짜로 바꾼다
# 그다음 design.html 본문을 고치고 PR로 올린다. 오타 · 실측값 같은 작은 수정에는 쓰지 않는다.
set -euo pipefail

new="${1:?새 버전 (예: v0.2)}"
[[ "$new" =~ ^v[0-9]+(\.[0-9]+)*$ ]] || { echo "버전 형식: v숫자[.숫자] (예: v1, v0.2)" >&2; exit 1; }
cd "$(git rev-parse --show-toplevel)"

doc=docs/design.html
hist=docs/design-history
git fetch -q origin main --tags
base=$(git rev-parse origin/main)
old=$(git show "$base:$doc" | grep -oP '<dt>상태</dt><dd>[^<]*\K v[0-9.]+(?=</dd>)' | tr -d ' ')
[ -n "$old" ] || { echo "$doc 머리말에서 버전(<dt>상태</dt><dd>… vX</dd>)을 찾지 못했다." >&2; exit 1; }
[ "$old" != "$new" ] || { echo "지금 버전이 이미 $new 다." >&2; exit 1; }
[ ! -e "$hist/design-$old.html" ] || { echo "$hist/design-$old.html 이 이미 있다." >&2; exit 1; }

branch=$(git branch --show-current)
if [ "$branch" = main ] || [ -z "$branch" ]; then
  git switch -q -c "design-bump-$new" "$base"
  branch="design-bump-$new"
fi
git show "$base:$doc" > "$hist/design-$old.html"

today=$(TZ=Asia/Seoul date +%F)
repo_url=${REPO_URL:-$(gh repo view --json url -q .url)}
REPO_URL="$repo_url" OLD="$old" NEW="$new" TODAY="$today" python3 - "$doc" "$hist/README.md" <<'PY'
import os, re, sys
doc, hist = sys.argv[1], sys.argv[2]
old, new, today, repo = os.environ["OLD"], os.environ["NEW"], os.environ["TODAY"], os.environ["REPO_URL"]
s = open(doc, encoding="utf-8").read()
s, n1 = re.subn(rf"(<dt>상태</dt><dd>[^<]*) {re.escape(old)}(</dd>)", rf"\1 {new}\2", s, count=1)
s, n2 = re.subn(rf" {re.escape(old)} · \d{{4}}-\d{{2}}-\d{{2}} · ", f" {new} · {today} · ", s, count=1)
if n1 != 1:
    sys.exit("머리말 버전 표기를 바꾸지 못했다")
if n2 != 1:
    print("경고: 꼬리말 버전 · 날짜 표기를 찾지 못해 그대로 둔다", file=sys.stderr)
open(doc, "w", encoding="utf-8").write(s)

h = open(hist, encoding="utf-8").read()
# 지금 버전 줄을 이전 버전 줄로 바꾼다. "무엇이 바뀌었나" · "관련 ADR" 칸은 그대로 옮긴다.
m = re.search(rf"^\|\s*\*\*{re.escape(old)} \(현재\)\*\*\s*\|\s*(.*?)\s*~\s*\|[^|\n]*\|\s*(.*?)\s*\|\s*(.*?)\s*\|[ \t]*\n", h, flags=re.M)
since, what, adr = (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")
if m:
    h = h[: m.start()] + h[m.end() :]
row_new = f"| **{new} (현재)** | {today} ~ | [`docs/design.html`](../design.html) | (작성: 무엇이 바뀌었나) | (관련 ADR) |\n"
row_old = f"| {old} | {since} ~ {today} | [`design-{old}.html`](design-{old}.html) · [태그]({repo}/tree/design-{old}) | {what} | {adr} |\n"
marker = "|---|---|---|---|---|\n"
h = h.replace(marker, marker + row_new + row_old, 1)
open(hist, "w", encoding="utf-8").write(h)
PY

git tag "design-$old" "$base"
git push -q origin "refs/tags/design-$old"
git add "$doc" "$hist"
cat <<MSG
준비 완료: 브랜치 $branch
- $hist/design-$old.html (이전 버전 사본), 태그 design-$old (push함)
- $doc 버전 표기 $old → $new
다음: $doc 본문을 고치고, $hist/README.md의 "무엇이 바뀌었나" · "관련 ADR" 칸을 채운 뒤
  git commit -m "[T29] 설계 문서 $new: …" && gh pr create
MSG
