"""docs/tasks.md와 GitHub 이슈 · 프로젝트 보드를 맞춘다. 진행 상황의 기준은 보드(이슈)다.

- 진행 상황(할 일 체크, 담당, 완료)은 이슈와 보드에서 관리한다.
- 계획(작업 추가, 할 일 문장, 할 일 이동, 우선순위)은 tasks.md에서 고친다.

    python scripts/sync_tasks.py push   # tasks.md의 계획 → 이슈 (체크 상태는 이슈 것을 유지)
    python scripts/sync_tasks.py pull   # 이슈의 체크 · 담당 → tasks.md, 체크 상태 → 보드
    python scripts/sync_tasks.py tick T3 2   # T3 이슈의 2번째 할 일을 체크
    python scripts/sync_tasks.py status  # 보드 · 체크 · 선행을 읽기 전용으로 조회
    python scripts/sync_tasks.py start T1  # 완료 체크 없이 작업 시작을 보드에 표시
    python scripts/sync_tasks.py activity T30 active  # 부분 PR과 별개로 실제 작업 진행 중
    python scripts/sync_tasks.py activity T30 auto  # 진행 표시 해제 후 현재 PR 상태로 계산
    --dry-run 을 붙이면 바뀔 내용만 출력한다.

필요: gh CLI, GH_TOKEN(이슈 쓰기 + 조직 프로젝트 쓰기 권한).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

OWNER = "Softbank-Hackathon-Finals"
REPO = f"{OWNER}/platform"
PROJECT_NUMBER = 2
PR_REPOSITORIES = {REPO: "develop", f"{OWNER}/demo-chat": "main"}
DEMO_TASKS = frozenset({"T4", "T22"})
MAX_PR_PAGES = 30
TASKS = Path(__file__).resolve().parents[1] / "docs/tasks.md"
# Gitflow 전환 전에 열린 PR만 정확한 번호·작업·브랜치로 허용한다.
LEGACY_PRS = {51: ("T5", "t5-control-contract")}

LOGINS = {
    "류지환": "mangowhoiscloud",
    "김태현": "kth4778",
    "배규태": "baekyutae",
    "이상무": "sangmu1126",
    "전동훈": "Jeonsubb",
}
NAMES = {v: k for k, v in LOGINS.items()}
# 설계 문서 11.2 역할 분담의 영역. tasks.md의 **영역** 값이 이 이름으로 시작해야 라벨이 붙는다.
AREAS = {
    "데이터 · 컷오버": "area:data",
    "보안 통제 · 게이트": "area:security",
    "에이전트 · 리허설": "area:agent",
    "인프라 · CI · Edge": "area:infra",
    "플랫폼 API · 콘솔 · 데모 앱": "area:platform",
    "통합 · 문서": "area:all",
}
# 스크립트가 관리하는 라벨. 이 접두사의 라벨은 tasks.md 값으로 덮어쓴다.
MANAGED = ("P0", "P1", "P2", "stage:", "area:")
ACTIVE_WORK_LABEL = "work:active"
STEP = re.compile(r"^- \[([ x])\] (.+)$")

# 보드로 옮긴 내역. 워크플로가 Slack으로 보낸다.
moves: list[str] = []


@dataclass
class Task:
    id: str
    title: str
    stage: str
    prio: str = ""
    area: str = ""
    owner: str = ""
    why: str = ""
    make: str = ""
    goal: str = ""
    done: str = ""
    refs: str = ""
    deps: list[str] = field(default_factory=list)
    succ: list[str] = field(default_factory=list)
    # 통합 시점 의존(설계 문서의 점선): 개발은 먼저 시작하고, 완료 확인만 그 작업 뒤에 한다.
    deps_int: list[str] = field(default_factory=list)
    succ_int: list[str] = field(default_factory=list)
    steps: list[tuple[bool, str]] = field(default_factory=list)


def run(*args: str, input: str | None = None) -> str:
    r = subprocess.run(args, check=False, capture_output=True, text=True, input=input, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}: {r.stderr.strip()}")
    return r.stdout


def gql(query: str, **variables) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    return json.loads(run(*args))["data"]


def warn(msg: str) -> None:
    # GitHub Actions에서는 실행 화면에 경고로 보인다.
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"경고: {msg}")


def ids(text: str) -> list[str]:
    return re.findall(r"`(T\d+)`", text)


def split_int(text: str) -> tuple[list[str], list[str]]:
    """"`T1` (통합 시점: `T4`)" → (["T1"], ["T4"]). 괄호 밖은 시작 조건, 괄호 안은 완료 확인 조건."""
    m = re.search(r"\(통합 시점:([^)]*)\)", text)
    if not m:
        return ids(text), []
    return ids(text[: m.start()] + text[m.end() :]), ids(m.group(1))


# ---------------------------------------------------------------- tasks.md


def parse(text: str) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    stage = ""
    cur: Task | None = None
    for line in text.splitlines():
        if m := re.match(r"## (\d+)단계", line):
            stage, cur = m.group(1), None
        elif line.startswith("## "):
            stage, cur = "", None
        elif m := re.match(r"### \[(T\d+)\] (.+)", line):
            cur = Task(m.group(1), m.group(2).strip(), stage) if stage else None
            if cur:
                tasks[cur.id] = cur
        elif cur is None:
            continue
        elif line.startswith("**어디에 필요** "):
            cur.why = line.split("** ", 1)[1]
        elif line.startswith("**만들 것** "):
            cur.make = line.split("** ", 1)[1]
        elif line.startswith("**목표** "):
            cur.goal = line.split("** ", 1)[1]
        elif line.startswith("**완료 기준** "):
            cur.done = line.split("** ", 1)[1]
        elif line.startswith("- **우선순위**"):
            cur.prio = re.search(r"(P\d)", line).group(1)
            cur.area = re.search(r"\*\*영역\*\* (.+?) · \*\*담당\*\*", line).group(1).strip()
            cur.owner = re.search(r"\*\*담당\*\* (.+)$", line).group(1).strip()
        elif line.startswith("- **선행**"):
            dep, rest = line.split("**후속**", 1)
            succ, _, refs = rest.partition("**설계 문서**")
            (cur.deps, cur.deps_int), (cur.succ, cur.succ_int) = split_int(dep), split_int(succ)
            cur.refs = refs.strip()
        elif m := STEP.match(line):
            cur.steps.append((m.group(1) == "x", m.group(2)))
    return tasks


def issue_steps(body: str) -> dict[str, bool]:
    """이슈 본문의 할 일 문장 → 체크 여부."""
    return {m.group(2): m.group(1) == "x" for line in (body or "").splitlines() if (m := STEP.match(line))}


def body(t: Task, num: dict[str, int]) -> str:
    def link(ts: list[str]) -> str:
        return ", ".join(f"#{num[x]} {x}" if x in num else x for x in ts) or "없음"

    steps = "\n".join(f"- [{'x' if c else ' '}] {s}" for c, s in t.steps)
    rel = [f"- 선행: {link(t.deps)}"]
    if t.deps_int:
        rel.append(f"- 통합 시점: {link(t.deps_int)} (먼저 시작해도 된다. 완료 확인만 이 작업 뒤에)")
    rel.append(f"- 후속: {link(t.succ)}")
    if t.succ_int:
        rel.append(f"- 후속 (통합 시점): {link(t.succ_int)}")
    rel = "\n".join(rel)
    return f"""**어디에 필요** {t.why}

**만들 것** {t.make}

## 목표
{t.goal}

## 할 일
{steps}

## 완료 기준
{t.done}

## 관계
{rel}
- 단계 {t.stage} · 우선순위 {t.prio} · 설계 문서 {t.refs}

> 할 일 체크와 담당은 이 이슈에서 한다. 할 일 문장 · 작업 추가 · 이동은 `docs/tasks.md`에서 고친다.
"""


def labels(t: Task) -> set[str]:
    area = next((v for k, v in AREAS.items() if t.area.startswith(k)), None)
    return {t.prio, f"stage:{t.stage}"} | ({area} if area else set())


def owner_of(issue: dict) -> str:
    logins = [a["login"] for a in issue["assignees"]]
    return ", ".join(NAMES.get(x, x) for x in logins) or "미정"


def rewrite_tasks(text: str, issues: dict[str, dict]) -> str:
    """tasks.md의 체크박스와 담당을 이슈에 맞춘다. 계획 내용은 건드리지 않는다."""
    out, cur = [], None
    owners: dict[str, str] = {}
    for line in text.splitlines():
        if m := re.match(r"### \[(T\d+)\]", line):
            cur = issues.get(m.group(1))
        elif line.startswith("## "):
            cur = None
        if cur is not None:
            if m := STEP.match(line):
                checked = issue_steps(cur["body"]).get(m.group(2))
                if checked is not None:
                    line = f"- [{'x' if checked else ' '}] {m.group(2)}"
            elif line.startswith("- **우선순위**") and (m := re.search(r"\*\*담당\*\* (.+)$", line)):
                if m.group(1) != "전원":
                    new = owner_of(cur)
                    line = line[: m.start(1)] + new
                    tid = re.match(r"\[(T\d+)\]", cur["title"]).group(1)
                    owners[tid] = new
        out.append(line)
    return rewrite_roles("\n".join(out) + "\n", owners)


def rewrite_roles(text: str, owners: dict[str, str]) -> str:
    """역할 분담 목록에서 담당이 바뀐 작업을 새 담당 아래로 옮기고, 목록에 없는 작업을 추가하고, 개수를 다시 센다."""
    start = text.index("## 역할 분담")
    end = text.index("\n## ", start + 1)
    lines = text[start:end].split("\n")
    blocks: list[list[str]] = []  # [헤더, 항목...]
    head: list[str] = []
    for line in lines:
        if line.startswith("**"):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            head.append(line)

    def who(block: list[str]) -> str:
        return re.match(r"\*\*(.+?)\*\*", block[0]).group(1)

    entries = {}
    for b in blocks:
        for line in b[1:]:
            if m := re.match(r"- `(T\d+)`", line):
                entries[m.group(1)] = (who(b), line)
    for tid, (cur, line) in entries.items():
        new = owners.get(tid, cur).split(", ")[0]
        if new == cur or tid not in owners:
            continue
        target = next((b for b in blocks if who(b) == new), None) or next(b for b in blocks if who(b) == "미정")
        src = next(b for b in blocks if who(b) == cur)
        src.remove(line)
        items = [i for i, x in enumerate(target) if x.startswith("- `T")]
        target.insert((items[-1] + 1) if items else 1, line)

    # 작업 섹션에는 있는데 목록에 없는 작업(새로 추가한 작업)은 담당 아래에 한 줄을 넣는다.
    # 담당이 목록에 없는 이름이면 미정 아래. 목록에서 빼는 일은 하지 않는다(작업을 지울 때는 직접 지운다).
    listed = {m.group(1) for b in blocks for x in b if (m := re.match(r"- `(T\d+)`", x))}
    for t in sorted(parse(text).values(), key=lambda x: int(x.id[1:])):
        if t.id in listed:
            continue
        name = owners.get(t.id, t.owner).split(", ")[0]
        target = next((b for b in blocks if who(b) == name), None) or next(b for b in blocks if who(b) == "미정")
        items = [i for i, x in enumerate(target) if x.startswith("- `T")]
        target.insert((items[-1] + 1) if items else 1, f"- `{t.id}` {t.title} ({t.prio})")
        print(f"{t.id}: 역할 분담 목록에 추가 ({who(target)})")

    for b in blocks:
        items = [x for x in b if x.startswith("- `T")]
        p0 = sum("(P0)" in x for x in items)
        b[0] = re.sub(r"\((\d+)개, P0 (\d+)개\)", f"({len(items)}개, P0 {p0}개)", b[0])
    section = "\n".join(head + [x for b in blocks for x in b])
    return text[:start] + section + text[end:]


# ---------------------------------------------------------------- 보드


class Board:
    def __init__(self) -> None:
        d = gql(
            """query($o:String!,$n:Int!){organization(login:$o){projectV2(number:$n){id
            fields(first:100){nodes{... on ProjectV2SingleSelectField{id name options{id name}}}}}}}""",
            o=OWNER, n=PROJECT_NUMBER,
        )["organization"]["projectV2"]
        self.id = d["id"]
        status = next(f for f in d["fields"]["nodes"] if f and f.get("name") == "Status")
        self.field = status["id"]
        self.options = {o["name"]: o["id"] for o in status["options"]}
        self.items: dict[int, tuple[str, str | None]] = {}
        self.pr_states: dict[int, str] = {}
        cursor = ""
        while True:
            query = """query($p:ID!,$after:String){node(id:$p){... on ProjectV2{
              items(first:100,after:$after){nodes{id content{... on Issue{number repository{nameWithOwner}}}
                fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}
                prStatus:fieldValueByName(name:"PR 상태"){... on ProjectV2ItemFieldTextValue{text}}}
                pageInfo{hasNextPage endCursor}}}}}"""
            params = {"p": self.id}
            if cursor:
                params["after"] = cursor
            else:
                query = query.replace(",$after:String", "").replace(",after:$after", "")
            page = gql(query, **params)["node"]["items"]
            for it in page["nodes"]:
                c = it["content"] or {}
                if c.get("repository", {}).get("nameWithOwner") == REPO and "number" in c:
                    value = it["fieldValueByName"]
                    self.items[c["number"]] = (it["id"], value["name"] if value else None)
                    self.pr_states[c["number"]] = (it.get("prStatus") or {}).get("text", "")
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]

    def status(self, number: int) -> str | None:
        return self.items.get(number, (None, None))[1]

    def set(self, number: int, node_id: str, name: str, label: str, dry: bool) -> None:
        before = self.status(number)
        if before == name:
            return
        moves.append(f"{label} : {before or '보드 밖'} → {name}")
        print(f"{label}: 보드 {before or '없음'} → {name}")
        if dry:
            return
        item = self.items.get(number, (None, None))[0]
        if item is None:
            item = gql(
                "mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}",
                p=self.id,
                c=node_id,
            )["addProjectV2ItemById"]["item"]["id"]
        gql(
            """mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:
            {projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}""",
            p=self.id,
            i=item,
            f=self.field,
            o=self.options[name],
        )
        self.items[number] = (item, name)


def load_issues() -> dict[str, dict]:
    issues = json.loads(
        run("gh", "issue", "list", "-R", REPO, "--state", "all", "--limit", "500",
            "--json", "number,id,title,body,state,labels,assignees")
    )
    out: dict[str, dict] = {}
    for i in sorted(issues, key=lambda x: x["number"]):
        if m := re.match(r"\[(T\d+)\]", i["title"]):
            out.setdefault(m.group(1), i)
    return out


# ---------------------------------------------------------------- 명령


def push(dry: bool) -> None:
    """tasks.md의 계획을 이슈에 반영한다. 체크 상태는 이슈 것을 유지한다."""
    tasks = parse(TASKS.read_text(encoding="utf-8"))
    by_id = load_issues()
    board = Board()

    for t in tasks.values():
        if t.id not in by_id:
            print(f"{t.id}: 새 이슈")
            if dry:
                continue
            url = run("gh", "issue", "create", "-R", REPO, "--title", f"[{t.id}][{t.prio}] {t.title}",
                      "--body", "(tasks.md에서 생성 중)").strip()
            n = int(url.rsplit("/", 1)[1])
            i = json.loads(run("gh", "issue", "view", str(n), "-R", REPO,
                               "--json", "number,id,title,body,state,labels,assignees"))
            by_id[t.id] = i
            login = LOGINS.get(t.owner)
            if login:
                run("gh", "issue", "edit", str(n), "-R", REPO, "--add-assignee", login)
            board.set(n, i["id"], "Backlog", f"{t.id} #{n}", dry)
    num = {k: v["number"] for k, v in by_id.items()}

    for t in sorted(tasks.values(), key=lambda x: int(x.id[1:])):
        i = by_id.get(t.id)
        if i is None:
            continue
        n = i["number"]
        # 같은 문장이 이슈에 있으면 이슈의 체크를 따른다. 새로 생기거나 옮겨 온 할 일은 tasks.md 값을 쓴다.
        have = issue_steps(i["body"])
        t.steps = [(have.get(s, c), s) for c, s in t.steps]

        args = ["gh", "issue", "edit", str(n), "-R", REPO]
        changes = []
        title = f"[{t.id}][{t.prio}] {t.title}"
        if i["title"] != title:
            args += ["--title", title]
            changes.append("제목")
        new_body = body(t, num)
        if (i["body"] or "").strip() != new_body.strip():
            args += ["--body-file", "-"]
            changes.append("본문")
        have_l = {l["name"] for l in i["labels"]}
        managed = {l for l in have_l if l.startswith(MANAGED)}
        want = labels(t)
        if want - have_l:
            args += ["--add-label", ",".join(sorted(want - have_l))]
        if managed - want:
            args += ["--remove-label", ",".join(sorted(managed - want))]
        if want != managed:
            changes.append("라벨")
        if changes:
            print(f"{t.id} #{n}: {', '.join(changes)}")
            if not dry:
                run(*args, input=new_body)

    for k, i in by_id.items():
        if k not in tasks and i["state"] == "OPEN":
            warn(f"{k} #{i['number']}: tasks.md에 없음 (그대로 둠, 필요하면 직접 닫기)")

    # 새 작업을 역할 분담 목록에도 넣는다. 바뀌면 워크플로가 봇 PR로 올린다.
    text = TASKS.read_text(encoding="utf-8")
    new = rewrite_roles(text, {})
    if new != text:
        print("tasks.md: 역할 분담 목록 갱신")
        if not dry:
            TASKS.write_text(new, encoding="utf-8")


def pull(dry: bool) -> bool:
    """이슈의 체크 · 담당을 tasks.md에 반영하고, 체크 상태로 보드와 이슈 열림을 맞춘다."""
    by_id = load_issues()
    board = Board()
    tasks = parse(TASKS.read_text(encoding="utf-8"))

    for tid, i in sorted(by_id.items(), key=lambda x: int(x[0][1:])):
        if tid not in tasks:
            continue
        n, label = i["number"], f"{tid} #{i['number']}"
        steps = issue_steps(i["body"])
        total, k = len(steps), sum(steps.values())
        now = board.status(n)
        if i["state"] == "CLOSED":
            board.set(n, i["id"], "Done", label, dry)
        elif total and k == total:
            print(f"{label}: 할 일을 모두 체크해 이슈 닫기")
            if not dry:
                run("gh", "issue", "close", str(n), "-R", REPO, "--reason", "completed",
                    "--comment", "할 일이 모두 체크되어 자동으로 닫음")
            board.set(n, i["id"], "Done", label, dry)
        elif has_active_work(i) or (k > 0 and now in (None, "Backlog", "Ready", "Done")):
            board.set(n, i["id"], "In progress", label, dry)
        elif k == 0 and now in (None, "Done"):
            board.set(n, i["id"], "Backlog", label, dry)

    text = TASKS.read_text(encoding="utf-8")
    new = rewrite_tasks(text, by_id)
    changed = new != text
    if changed:
        print("tasks.md: 이슈의 체크 · 담당 반영")
        if not dry:
            TASKS.write_text(new, encoding="utf-8")
    return changed


def tick(task: str, index: int, dry: bool) -> None:
    """작업 이슈의 index번째(1부터) 할 일을 체크한다."""
    i = load_issues()[task]
    lines = i["body"].splitlines()
    pos = [n for n, line in enumerate(lines) if STEP.match(line)]
    if not 1 <= index <= len(pos):
        sys.exit(f"{task} 할 일은 1~{len(pos)}번")
    line = lines[pos[index - 1]]
    lines[pos[index - 1]] = line.replace("- [ ]", "- [x]", 1)
    print(f"{task} #{i['number']}: {lines[pos[index - 1]]}")
    if not dry:
        run("gh", "issue", "edit", str(i["number"]), "-R", REPO, "--body-file", "-", input="\n".join(lines) + "\n")


def status() -> None:
    """보드와 실제 이슈를 읽는다. 완료 체크나 로컬 계획을 수정하지 않는다."""
    issues = load_issues()
    board = Board()
    tasks = parse(TASKS.read_text(encoding="utf-8"))
    rows = []
    for tid, task in sorted(tasks.items(), key=lambda x: int(x[0][1:])):
        issue = issues.get(tid)
        if issue is None:
            rows.append({"task": tid, "error": "issue missing"})
            continue
        checks = issue_steps(issue["body"])
        rows.append({
            "task": tid, "title": task.title, "issue": issue["number"], "priority": task.prio,
            "assignees": [a["login"] for a in issue["assignees"]],
            "status": board.status(issue["number"]), "state": issue["state"],
            "pr_status": board.pr_states.get(issue["number"], ""),
            "checked": sum(checks.values()), "total": len(checks),
            "active_work": has_active_work(issue),
            "blocked_by": [dep for dep in task.deps if issues.get(dep, {}).get("state") != "CLOSED"],
            "integration_pending": [dep for dep in task.deps_int if issues.get(dep, {}).get("state") != "CLOSED"],
        })
    print(json.dumps({"project": f"https://github.com/orgs/{OWNER}/projects/{PROJECT_NUMBER}/views/2", "tasks": rows}, ensure_ascii=False, indent=2))


def start(task: str, dry: bool) -> None:
    """검증 전에도 시작 상태를 표시한다. 완료 체크는 머지 후 tick으로 한다."""
    tasks = parse(TASKS.read_text(encoding="utf-8"))
    issues = load_issues()
    if task not in tasks or task not in issues:
        raise ValueError(f"알 수 없는 작업: {task}")
    issue = issues[task]
    if issue["state"] != "OPEN":
        raise ValueError(f"{task}: 닫힌 작업은 시작으로 바꾸지 않는다")
    blocked = [dep for dep in tasks[task].deps if issues.get(dep, {}).get("state") != "CLOSED"]
    if blocked:
        raise ValueError(f"{task}: 선행 미완료: {', '.join(blocked)}")
    board = Board()
    if board.status(issue["number"]) in ("In review", "Done"):
        raise ValueError(f"{task}: 현재 상태를 In progress로 되돌리지 않는다")
    board.set(issue["number"], issue["id"], "In progress", f"{task} #{issue['number']}", dry)


def has_active_work(issue: dict) -> bool:
    return any(label["name"] == ACTIVE_WORK_LABEL for label in issue.get("labels", []))


def activity(task: str, mode: str, dry: bool) -> None:
    """Record the owner's task-level activity without changing PRs or completion."""
    if mode not in ("active", "auto"):
        raise ValueError("activity T번호 active|auto")
    tasks = parse(TASKS.read_text(encoding="utf-8"))
    issues = load_issues()
    if task not in tasks or task not in issues:
        raise ValueError(f"알 수 없는 작업: {task}")
    issue = issues[task]
    checks = issue_steps(issue["body"])
    if mode == "active" and (issue["state"] != "OPEN" or (checks and all(checks.values()))):
        raise ValueError(f"{task}: 완료한 작업에 진행 표시를 추가하지 않는다")

    # Complete both repository reads and board validation before any write.
    prs = [pr for pr in load_prs() if pr_task(pr, issues) == task]
    board = Board()
    updated = {**issue, "labels": [label for label in issue.get("labels", [])
                                  if label["name"] != ACTIVE_WORK_LABEL]}
    if mode == "active":
        updated["labels"].append({"name": ACTIVE_WORK_LABEL})
    state = pr_task_status(updated, prs)
    changed = has_active_work(issue) != has_active_work(updated)
    print(f"{task} #{issue['number']}: 작업 진행 표시 {mode} → {state}")
    if changed and not dry:
        if mode == "active":
            existing = gql("""query($o:String!,$r:String!,$name:String!){
              repository(owner:$o,name:$r){label(name:$name){id}}}""",
                o=OWNER, r=REPO.split("/")[1], name=ACTIVE_WORK_LABEL)
            if existing["repository"]["label"] is None:
                run("gh", "label", "create", ACTIVE_WORK_LABEL, "-R", REPO,
                    "--color", "1D76DB", "--description",
                    "Owner-confirmed task work continues alongside partial PR review")
        run("gh", "issue", "edit", str(issue["number"]), "-R", REPO,
            "--add-label" if mode == "active" else "--remove-label", ACTIVE_WORK_LABEL)
    board.set(issue["number"], issue["id"], state, f"{task} #{issue['number']}", dry)



def load_prs() -> list[dict]:
    """Read all configured repos before mutating any card; incomplete reads fail."""
    result = []
    for repository in PR_REPOSITORIES:
        cursor, seen = "", set()
        for _ in range(MAX_PR_PAGES):
            data = gql("""query($o:String!,$r:String!,$after:String){repository(owner:$o,name:$r){
              pullRequests(first:100,after:$after,orderBy:{field:UPDATED_AT,direction:DESC}){
                nodes{number title body state isDraft headRefName baseRefName reviewDecision mergedAt updatedAt
                  headRepository{nameWithOwner} url
                  commits(last:1){nodes{commit{statusCheckRollup{state}}}}}
                pageInfo{hasNextPage endCursor}}}}""".replace(
                    "after:$after,", "after:$after," if cursor else "").replace(
                    ",$after:String", ",$after:String" if cursor else ""),
                **{"o": OWNER, "r": repository.split("/")[1],
                   **({"after": cursor} if cursor else {})})
            page = data["repository"]["pullRequests"]
            for pr in page["nodes"]:
                result.append({**pr, "source_repository": repository})
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
            if not cursor or cursor in seen:
                raise RuntimeError("PR pagination incomplete; board unchanged")
            seen.add(cursor)
        else:
            raise RuntimeError("PR pagination limit; board unchanged")
    return result


def pr_summary(pr: dict) -> str:
    if pr["state"] == "MERGED":
        state = "병합됨"
    elif pr["state"] == "CLOSED":
        state = "병합 없이 닫힘"
    elif pr["isDraft"]:
        state = "초안"
    else:
        state = {"APPROVED": "승인 · 병합 대기", "CHANGES_REQUESTED": "수정 요청"}.get(
            pr["reviewDecision"], "리뷰 대기")
    nodes = pr.get("commits", {}).get("nodes", [])
    rollup = nodes[-1]["commit"].get("statusCheckRollup") if nodes else None
    ci = rollup["state"] if rollup else "미보고"
    repository = pr.get("source_repository", REPO).split("/")[-1]
    return f"{repository}#{pr['number']} {state} / CI {ci}"


def pr_task_status(issue: dict, prs: list[dict]) -> str:
    checks = issue_steps(issue["body"])
    if issue["state"] == "CLOSED" or (checks and all(checks.values())):
        return "Done"
    if has_active_work(issue):
        return "In progress"
    opened = [pr for pr in prs if pr["state"] == "OPEN"]
    if not opened or any(pr["isDraft"] or pr["reviewDecision"] == "CHANGES_REQUESTED" for pr in opened):
        return "In progress"
    # Approval is not merge, and a partial merge is not task completion.
    return "In review"


def pr_task(pr: dict, issues: dict | None = None) -> str | None:
    """Only same-repository heads and explicit cross-repository task references."""
    repository = pr.get("source_repository", REPO)
    if repository not in PR_REPOSITORIES:
        return None
    if (pr.get("headRepository") or {}).get("nameWithOwner") != repository:
        return None
    if pr.get("baseRefName") != PR_REPOSITORIES[repository]:
        return None
    match = re.match(r"^\[(T\d+)\] ", pr["title"])
    if not match:
        return None
    task, branch = match[1], pr["headRefName"]
    if repository != REPO:
        issue = (issues or {}).get(task)
        if task not in DEMO_TASKS or issue is None:
            return None
        # Require a standalone qualified reference line. A local #15 means a
        # demo-chat issue, not the platform task. Titles alone never bind tasks.
        reference = rf"(?im)^(?:refs|closes|fixes|resolves) +{re.escape(REPO)}#{issue['number']}[ \t]*$"
        if not re.search(reference, pr.get("body", "")):
            return None
    if re.fullmatch(rf"feature/{task}-[A-Za-z0-9][A-Za-z0-9._-]*", branch):
        return task
    if repository == REPO and LEGACY_PRS.get(pr["number"]) == (task, branch):
        return task
    return None


def pr_order(pr: dict) -> tuple[str, str, int]:
    # PR numbers are repository-local; compare update timestamps across repos.
    return (pr.get("updatedAt", ""), pr.get("source_repository", REPO), pr["number"])


def sync_prs(dry: bool) -> None:
    """Update existing task cards only. Never check tasks, close issues, or merge PRs."""
    issues, board, grouped = load_issues(), Board(), {}
    for pr in load_prs():
        task = pr_task(pr, issues)
        if task in issues:
            grouped.setdefault(task, []).append(pr)
    fields = gql("""query($p:ID!){node(id:$p){... on ProjectV2{
      fields(first:100){nodes{... on ProjectV2Field{id name dataType}}}}}}""", p=board.id)
    status_field = next((f for f in fields["node"]["fields"]["nodes"] if f.get("name") == "PR 상태"), None)
    if status_field and status_field["dataType"] != "TEXT":
        raise RuntimeError("PR 상태 must be a text field")
    if status_field is None and not dry:
        status_field = gql("""mutation($p:ID!){createProjectV2Field(input:{projectId:$p,
          dataType:TEXT,name:"PR 상태"}){projectV2Field{... on ProjectV2Field{id}}}}""", p=board.id)["createProjectV2Field"]["projectV2Field"]
    for task, prs in sorted(grouped.items(), key=lambda item: int(item[0][1:])):
        issue = issues[task]
        opened = sorted((p for p in prs if p["state"] == "OPEN"), key=pr_order)
        displayed = opened or [max(prs, key=pr_order)]
        detail = " | ".join(pr_summary(pr) for pr in displayed)
        if len(detail) > 900:
            detail = detail[:897] + "..."
        state = pr_task_status(issue, prs)
        board.set(issue["number"], issue["id"], state, f"{task} #{issue['number']}", dry)
        print(f"{task}: {detail}")
        if dry or board.pr_states.get(issue["number"]) == detail:
            continue
        gql("""mutation($p:ID!,$i:ID!,$f:ID!,$t:String!){updateProjectV2ItemFieldValue(input:{
          projectId:$p,itemId:$i,fieldId:$f,value:{text:$t}}){projectV2Item{id}}}""",
            p=board.id, i=board.items[issue["number"]][0], f=status_field["id"], t=detail)


def reconcile(dry: bool) -> None:
    """Recover coalesced events from current authoritative state, in source order."""
    push(dry)
    pull(dry)
    sync_prs(dry)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["push", "pull", "tick", "status", "start", "activity", "prs", "reconcile"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--moves-file", help="보드로 옮긴 내역을 적을 파일 (Slack 알림용)")
    a = ap.parse_args()
    if a.command == "reconcile":
        reconcile(a.dry_run)
    elif a.command == "push":
        push(a.dry_run)
    elif a.command == "pull":
        pull(a.dry_run)
    elif a.command == "prs":
        sync_prs(a.dry_run)
    elif a.command == "status":
        if a.args:
            ap.error("status는 인자를 받지 않는다")
        status()
    elif a.command == "start":
        if len(a.args) != 1:
            ap.error("start T번호")
        start(a.args[0], a.dry_run)
    elif a.command == "activity":
        if len(a.args) != 2 or a.args[1] not in ("active", "auto"):
            ap.error("activity T번호 active|auto")
        activity(a.args[0], a.args[1], a.dry_run)
    else:
        if len(a.args) != 2 or not a.args[1].isdigit():
            ap.error("tick T번호 순번")
        tick(a.args[0], int(a.args[1]), a.dry_run)
    if a.moves_file and moves:
        Path(a.moves_file).write_text("\n".join(moves) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        if "INSUFFICIENT_SCOPES" in str(exc) or "required scopes" in str(exc):
            sys.exit("GitHub Projects 권한 부족: status는 read:project, 변경은 project 권한이 필요하다. docs/project-board.md의 Actions 조회 경로를 사용할 수 있다.")
        raise
