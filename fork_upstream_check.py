#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出账号下所有 fork，逐个对比上游，报告「上游有、你缺」的提交数。

用法：
  python fork_upstream_check.py                 # 全量表格
  python fork_upstream_check.py --only-updates  # 只列缺上游 > 0 的
  python fork_upstream_check.py --json          # JSON
  python fork_upstream_check.py --changed-only  # 仅上游 HEAD 变过的（给定时任务用）
环境：需要已登录的 gh CLI（gh auth status 为绿）。
"""
import argparse
import json
import os
import subprocess
import sys

OWNER = os.environ.get("FORK_WATCH_OWNER", "")
STATE = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "hermes", "profiles", "checkin", "cache", "fork_watch_state.json",
)

GRAPHQL = """query($cursor: String) {
  user(login: "%s") {
    repositories(first: 50, after: $cursor, affiliations: [OWNER], isFork: true) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        defaultBranchRef { name }
        parent { nameWithOwner defaultBranchRef { name } pushedAt }
      }
    }
  }
}""" % OWNER


def gh(*args, check=True):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300])
    return p


def list_forks():
    forks, cursor = [], None
    while True:
        cmd = ["api", "graphql", "-f", "query=" + GRAPHQL]
        if cursor:
            cmd += ["-f", "cursor=" + cursor]
        out = json.loads(gh(*cmd).stdout)["data"]["user"]["repositories"]
        for node in out["nodes"]:
            branch = (node.get("defaultBranchRef") or {}).get("name") or "main"
            parent = node.get("parent") or {}
            forks.append({
                "repo": node["nameWithOwner"],
                "branch": branch,
                "upstream": parent.get("nameWithOwner") or "",
                "upstream_branch": (parent.get("defaultBranchRef") or {}).get("name") or "",
                "upstream_pushed": parent.get("pushedAt") or "",
            })
        if not out["pageInfo"]["hasNextPage"]:
            break
        cursor = out["pageInfo"]["endCursor"]
    return forks


def compare(fork):
    if not fork["upstream"]:
        fork.update(missing=None, own=None, status="无上游（已删/独立仓）", head="")
        return fork
    upstream_branch = fork["upstream_branch"] or "main"
    upstream_owner = fork["upstream"].split("/")[0]
    expr = "%s...%s:%s" % (fork["branch"], upstream_owner, upstream_branch)
    p = gh("api", "repos/%s/compare/%s" % (fork["repo"], expr),
           "--jq", "{ahead_by,behind_by,head:.commits[-1].sha,head_date:.commits[-1].commit.author.date}", check=False)
    if p.returncode != 0:
        fork.update(missing=None, own=None, status="无法比较", head="",
                    error=(p.stderr or "").strip()[:120])
        return fork
    d = json.loads(p.stdout)
    # base=你的 fork，head=上游：ahead_by = 上游独有（你缺的），behind_by = 你独有（改过的）
    fork.update(missing=d.get("ahead_by"), own=d.get("behind_by"),
                head=d.get("head") or "", head_date=d.get("head_date") or "",
                status="有更新" if (d.get("ahead_by") or 0) > 0 else "已同步")
    return fork


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only-updates", action="store_true")
    ap.add_argument("--changed-only", action="store_true")
    ap.add_argument("--owner", default="")
    args = ap.parse_args()
    global OWNER
    if args.owner:
        OWNER = args.owner
    if not OWNER:
        print("缺少账号：请设置环境变量 FORK_WATCH_OWNER=你的GitHub用户名，或加 --owner 你的用户名")
        return 2

    forks = [compare(f) for f in list_forks()]
    forks.sort(key=lambda f: (-(f["missing"] or 0), f["repo"].lower()))

    state = load_state()
    changed = []
    for f in forks:
        key = f["repo"]
        prev = state.get(key, {}).get("head", "")
        if f["head"] and f["head"] != prev:
            changed.append(f)
            state[key] = {"head": f["head"], "missing": f["missing"], "checked": f["head_date"]}
    save_state(state)

    if args.changed_only:
        if not changed:
            return 0
        print("上游有新提交的 fork：")
        for f in changed:
            print("- %s（上游 %s）：缺 %s 个提交，上游最新 %s" %
                  (f["repo"], f["upstream"], f["missing"], f["head_date"][:10]))
        return 0

    if args.json:
        print(json.dumps(forks, ensure_ascii=False, indent=2))
        return 0

    rows = [f for f in forks if (not args.only_updates or (f["missing"] or 0) > 0)]
    if not rows:
        print("没有需要跟进的 fork：所有上游都没有新提交。")
        return 0
    print("%-38s %-34s %6s %6s  %-10s %s" % ("你的仓库", "上游", "缺上游", "自己独有", "上游最后推送", "状态"))
    for f in rows:
        print("%-38s %-34s %6s %6s  %-10s %s" % (
            f["repo"], f["upstream"] or "-",
            "-" if f["missing"] is None else f["missing"],
            "-" if f["own"] is None else f["own"],
            (f["upstream_pushed"] or "")[:10] or "-",
            f["status"],
        ))
    missing_total = sum(1 for f in forks if (f["missing"] or 0) > 0)
    print("\n共 %d 个 fork，%d 个缺上游提交。" % (len(forks), missing_total))
    return 0


if __name__ == "__main__":
    sys.exit(main())