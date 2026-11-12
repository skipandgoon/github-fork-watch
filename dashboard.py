#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 项目面板（本机）

- 列出账号下所有仓库，区分：纯 fork / 有自有提交 / 非 fork
- 上游有更新的仓库置顶并标红
- 纯 fork：点「更新 fork」直接调用 GitHub 官方 merge-upstream（等同网页 Sync fork）
- 有自有提交：点「去处理」跳转 compare 页面，人工合并
- 数据缓存 5 分钟，右上角可手动刷新

用法：
  python dashboard.py            # http://127.0.0.1:8770
  python dashboard.py --port 9000
依赖：已登录的 gh CLI（gh auth status 为绿）
"""
import argparse
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OWNER = os.environ.get("FORK_WATCH_OWNER", "")
CACHE_TTL = 300
LOCK = threading.Lock()
CACHE = {"at": 0, "data": None}
UPSTREAM_CACHE = {}
UPSTREAM_TTL = 3600
STOP_WORDS = (
    "停止维护", "不再维护", "已停止维护", "停止更新", "停止开发", "停止修复", "停更", "弃坑",
    "项目已归档", "仓库已归档", "已归档", "no longer maintained", "unmaintained",
    "deprecated", "end of life", "archived by owner",
)
SELF_WORDS = (
    "本项目", "本仓库", "这个项目", "该项目", "此项目", "此仓库", "本插件", "本扩展",
    "this project", "this repository", "this repo",
)

GRAPHQL = """query($cursor: String) {
  user(login: "%s") {
    repositories(first: 100, after: $cursor, affiliations: [OWNER], ownerAffiliations: [OWNER]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        isFork
        isArchived
        description
        pushedAt
        defaultBranchRef { name }
        parent { nameWithOwner description isArchived defaultBranchRef { name } pushedAt }
      }
    }
  }
}"""


def graphql_query():
    return GRAPHQL % OWNER


def log(msg):
    """pythonw 没有控制台，print 可能抛异常；日志失败不影响服务。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def gh(args, check=True):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300])
    return p


def list_repos():
    repos, cursor = [], None
    while True:
        cmd = ["api", "graphql", "-f", "query=" + graphql_query()]
        if cursor:
            cmd += ["-f", "cursor=" + cursor]
        page = json.loads(gh(cmd).stdout)["data"]["user"]["repositories"]
        for n in page["nodes"]:
            parent = n.get("parent") or {}
            repos.append({
                "repo": n["nameWithOwner"],
                "fork": bool(n.get("isFork")),
                "archived": bool(n.get("isArchived")),
                "desc": (n.get("description") or "").strip(),
                "pushed": n.get("pushedAt") or "",
                "branch": (n.get("defaultBranchRef") or {}).get("name") or "main",
                "upstream": parent.get("nameWithOwner") or "",
                "upstream_branch": (parent.get("defaultBranchRef") or {}).get("name") or "",
                "upstream_pushed": parent.get("pushedAt") or "",
                "upstream_desc": (parent.get("description") or "").strip(),
                "upstream_archived": bool(parent.get("isArchived")),
            })
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return repos


def compare(repo):
    if not repo["fork"] or not repo["upstream"]:
        repo.update(missing=None, own=None, upstream_head="", state="own")
        return repo
    up_owner = repo["upstream"].split("/")[0]
    expr = "%s...%s:%s" % (repo["branch"], up_owner, repo["upstream_branch"] or "main")
    p = None
    for attempt in range(3):
        p = gh(["api", "repos/%s/compare/%s" % (repo["repo"], expr),
                "--jq", "{ahead_by,behind_by,head:.commits[-1].sha,date:.commits[-1].commit.author.date}"],
               check=False)
        if p.returncode == 0:
            break
        err = (p.stderr or "") + (p.stdout or "")
        if any(k in err for k in ("EOF", "timeout", "connection reset", "TLS handshake", "502", "503")):
            time.sleep(1.2 * (attempt + 1))
            continue
        break
    if p.returncode != 0:
        repo.update(missing=None, own=None, upstream_head="", state="error",
                    error=(p.stderr or "").strip().splitlines()[-1][:120] if (p.stderr or "").strip() else "compare failed")
        return repo
    d = json.loads(p.stdout)
    repo.update(
        missing=d.get("ahead_by"),           # 上游有、你没有
        own=d.get("behind_by"),              # 你有、上游没有
        upstream_head=d.get("head") or "",
        upstream_head_date=d.get("date") or "",
    )
    if (repo["own"] or 0) == 0:
        repo["state"] = "pure"
    else:
        repo["state"] = "modified"
    return repo


def upstream_maintenance(repo):
    """上游是否停止维护：归档标记 + 描述/README 关键词。结果按上游缓存 1 小时。"""
    up = repo.get("upstream")
    if not up:
        repo["upstream_stopped"] = False
        repo["upstream_stop_reason"] = ""
        return repo
    now = time.time()
    hit = UPSTREAM_CACHE.get(up)
    if hit and now - hit[0] < UPSTREAM_TTL:
        stopped, reason = hit[1], hit[2]
    else:
        stopped, reason = False, ""
        if repo.get("upstream_archived"):
            stopped, reason = True, "上游仓库已归档"
        desc = (repo.get("upstream_desc") or "").strip()
        if not stopped:
            for word in STOP_WORDS:
                if word.lower() in desc.lower():
                    stopped, reason = True, "上游描述含「%s」" % word
                    break
        if not stopped:
            p = gh(["api", "repos/%s/readme" % up,
                    "-H", "Accept: application/vnd.github.raw"], check=False)
            if p.returncode == 0:
                text = (p.stdout or "")[:8000]
                low = text.lower()
                # 自指 + 停止词（60 字窗口），避免把「原作者停止维护后，我继续…」当成本项目停维护
                for self_word in SELF_WORDS:
                    pos = 0
                    while True:
                        i = low.find(self_word, pos)
                        if i < 0:
                            break
                        window = low[i:i + 60]
                        hit_word = next((w for w in STOP_WORDS if w.lower() in window), None)
                        if hit_word:
                            stopped, reason = True, "上游 README：%s…%s" % (self_word, hit_word)
                            break
                        pos = i + len(self_word)
                    if stopped:
                        break
                if not stopped:
                    for line in text.splitlines()[:80]:
                        t = line.strip().strip("*# ").strip().lower()
                        if t and any(t.startswith(w.lower()) for w in STOP_WORDS[:6]):
                            stopped, reason = True, "上游 README 开头声明停止维护"
                            break
        UPSTREAM_CACHE[up] = (now, stopped, reason)
    repo["upstream_stopped"] = stopped
    repo["upstream_stop_reason"] = reason
    return repo


def build_status():
    # 已归档的仓库不监控：不参与比对、不占统计
    repos = [r for r in list_repos() if not r["archived"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        repos = list(pool.map(compare, repos))
        repos = list(pool.map(upstream_maintenance, repos))
    for r in repos:
        r["has_update"] = (r.get("missing") or 0) > 0
        if r["fork"] and r["upstream"]:
            up_owner = r["upstream"].split("/")[0]
            r["compare_url"] = "https://github.com/%s/compare/%s...%s:%s" % (
                r["repo"], r["branch"], up_owner, r["upstream_branch"] or "main")
        else:
            r["compare_url"] = ""
        r["repo_url"] = "https://github.com/" + r["repo"]
    repos.sort(key=lambda r: (not r["has_update"], -(r.get("missing") or 0), r["repo"].lower()))
    return {
        "owner": OWNER,
        "fetched_at": int(time.time()),
        "repos": repos,
        "summary": {
            "total": len(repos),
            "fork": sum(1 for r in repos if r["fork"]),
            "pure": sum(1 for r in repos if r.get("state") == "pure"),
            "modified": sum(1 for r in repos if r.get("state") == "modified"),
            "own": sum(1 for r in repos if r.get("state") == "own"),
            "updates": sum(1 for r in repos if r["has_update"]),
            "stopped": sum(1 for r in repos if r.get("upstream_stopped")),
        },
    }


def get_status(force=False):
    now = time.time()
    with LOCK:
        if not force and CACHE["data"] and now - CACHE["at"] < CACHE_TTL:
            return CACHE["data"]
    data = build_status()
    with LOCK:
        CACHE.update(at=time.time(), data=data)
    return data


def sync_fork(repo_name):
    data = get_status()
    row = next((r for r in data["repos"] if r["repo"] == repo_name), None)
    if not row:
        return {"ok": False, "error": "仓库不在列表里"}
    if not row["fork"] or not row["upstream"]:
        return {"ok": False, "error": "不是 fork，没有上游可同步"}
    if row.get("state") == "modified":
        return {"ok": False, "error": "该 fork 有自有提交，请走 compare 页面手动合并", "compare_url": row["compare_url"]}
    if not row["has_update"]:
        return {"ok": False, "error": "已经是最新，无需更新"}
    p = gh(["api", "-X", "POST", "repos/%s/merge-upstream" % repo_name,
            "-f", "branch=%s" % row["branch"]], check=False)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip().replace("\n", " ")[:200]
        return {"ok": False, "error": msg}
    try:
        out = json.loads(p.stdout)
    except Exception:
        out = {"raw": p.stdout.strip()[:200]}
    CACHE["at"] = 0
    return {"ok": True, "result": out}


INDEX = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitHub 项目面板</title>
<style>
  :root { --bg:#f4f7fb; --card:#fff; --line:#e3e9f2; --ink:#1c2534; --sub:#6b7687;
          --blue:#2f6fed; --green:#1f9d55; --red:#e0464f; --amber:#b7791f; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--ink);
         font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  header { display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin-bottom:18px; }
  h1 { font-size:20px; margin:0; }
  .muted { color:var(--sub); font-size:12px; }
  .spacer { flex:1; }
  button { font:inherit; cursor:pointer; border-radius:8px; border:1px solid var(--line);
           background:#fff; padding:7px 14px; color:var(--ink); }
  button:hover { border-color:#c7d4ea; }
  button.primary { background:var(--blue); color:#fff; border-color:var(--blue); }
  button.primary:hover { background:#255fd4; }
  button:disabled { opacity:.55; cursor:default; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  .card b { display:block; font-size:22px; line-height:1.2; }
  .card span { color:var(--sub); font-size:12px; }
  .section { background:var(--card); border:1px solid var(--line); border-radius:12px; margin-bottom:18px; overflow:hidden; }
  .section > h2 { margin:0; padding:12px 16px; font-size:15px; border-bottom:1px solid var(--line);
                  display:flex; gap:8px; align-items:center; }
  .section > h2 em { font-style:normal; color:var(--sub); font-weight:400; font-size:12px; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px 14px; text-align:left; border-bottom:1px solid var(--line); vertical-align:middle; }
  th { font-size:12px; color:var(--sub); font-weight:500; background:#fbfcfe; }
  tr:last-child td { border-bottom:0; }
  .name a { color:var(--ink); font-weight:600; text-decoration:none; }
  .name a:hover { color:var(--blue); }
  .desc { color:var(--sub); font-size:12px; margin-top:2px; max-width:520px; overflow:hidden;
          text-overflow:ellipsis; white-space:nowrap; }
  .badge { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; border:1px solid; }
  .b-up { color:var(--red); border-color:#f3c4c6; background:#fdf0f1; }
  .b-ok { color:var(--green); border-color:#c8e8d4; background:#f0fbf4; }
  .b-own { color:var(--amber); border-color:#f0dfba; background:#fdf8ec; }
  .b-gray { color:var(--sub); border-color:var(--line); background:#f7f9fc; }
  .b-stop { color:#8a5a00; border-color:#ead9a8; background:#fdf4df; }
  .up { color:var(--sub); font-size:12px; }
  .actions { text-align:right; white-space:nowrap; }
  .actions a { color:var(--blue); text-decoration:none; font-size:13px; }
  .hide { display:none; }
  #toast { position:fixed; right:20px; bottom:20px; background:#1c2534; color:#fff; padding:10px 16px;
           border-radius:8px; opacity:0; transform:translateY(8px); transition:.2s; max-width:420px; }
  #toast.show { opacity:1; transform:none; }
  #toast.err { background:#a5282f; }
  @media (max-width:1100px){ .cards{grid-template-columns:repeat(2,1fr);} .desc{max-width:240px;} }
</style>
</head>
<body>
<header>
  <h1>GitHub 项目面板</h1>
  <span class="muted" id="meta">加载中…</span>
  <span class="spacer"></span>
  <label class="muted"><input type="checkbox" id="onlyUpdates"> 只看有更新</label>
  <button id="refresh">刷新</button>
</header>

<div class="cards">
  <div class="card"><b id="s-total">-</b><span>全部仓库</span></div>
  <div class="card"><b id="s-updates" style="color:var(--red)">-</b><span>上游有更新</span></div>
  <div class="card"><b id="s-pure" style="color:var(--green)">-</b><span>纯 fork（可一键更新）</span></div>
  <div class="card"><b id="s-modified" style="color:var(--amber)">-</b><span>有自有提交</span></div>
  <div class="card"><b id="s-stopped" style="color:var(--amber)">-</b><span>上游停维护</span></div>
  <div class="card"><b id="s-own">-</b><span>非 fork / 无上游</span></div>
</div>

<div class="section" id="sec-modified">
  <h2>有自有提交 <em>上游有更新时打开项目主页，由你自己合并</em></h2>
  <table><thead><tr><th>仓库</th><th>上游</th><th>缺上游</th><th>自己独有</th><th>上游最后推送</th><th>上游状态</th><th>状态</th><th></th></tr></thead>
  <tbody id="tb-modified"></tbody></table>
</div>

<div class="section" id="sec-pure">
  <h2>纯 fork <em>没有自有提交，点「更新 fork」直接同步</em></h2>
  <table><thead><tr><th>仓库</th><th>上游</th><th>缺上游</th><th>上游最后推送</th><th>上游状态</th><th>状态</th><th></th></tr></thead>
  <tbody id="tb-pure"></tbody></table>
</div>

<div class="section" id="sec-own">
  <h2>非 fork / 无上游 <em>自有项目，不参与上游比对</em></h2>
  <table><thead><tr><th>仓库</th><th>说明</th><th>最后推送</th><th></th></tr></thead>
  <tbody id="tb-own"></tbody></table>
</div>

<div id="toast"></div>

<script>
const $ = (s) => document.querySelector(s);
let DATA = null, busy = new Set();

function esc(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function days(iso){ if(!iso) return ""; const d = Math.floor((Date.now() - new Date(iso).getTime())/86400000); return d <= 0 ? "今天" : d + " 天前"; }

function toast(msg, isErr){
  const t = $("#toast"); t.textContent = msg; t.className = "show" + (isErr ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.className = "", 5000);
}

function rowModified(r){
  return `<tr>
    <td class="name"><a href="${r.repo_url}" target="_blank">${esc(r.repo)}</a><div class="desc">${esc(r.desc)}</div></td>
    <td class="up">${esc(r.upstream)}</td>
    <td>${r.missing > 0 ? `<span class="badge b-up">↓ ${r.missing}</span>` : `<span class="badge b-ok">已同步</span>`}</td>
    <td><span class="badge b-own">${r.own}</span></td>
    <td class="up">${esc((r.upstream_pushed||"").slice(0,10))} · ${days(r.upstream_pushed)}</td>
    <td>${r.upstream_stopped ? `<span class="badge b-stop" title="${esc(r.upstream_stop_reason)}">停维护</span>` : '<span class="badge b-gray">正常</span>'}</td>
    <td>${r.has_update ? '<span class="badge b-up">上游有更新</span>' : '<span class="badge b-gray">无更新</span>'}</td>
    <td class="actions">${r.has_update ? `<a href="${r.repo_url}" target="_blank">打开项目 →</a>` : ""}</td>
  </tr>`;
}

function rowPure(r){
  const btn = r.has_update
    ? `<button class="primary" data-sync="${esc(r.repo)}" ${busy.has(r.repo) ? "disabled" : ""}>${busy.has(r.repo) ? "更新中…" : "更新 fork"}</button>`
    : `<button disabled>已同步</button>`;
  return `<tr>
    <td class="name"><a href="${r.repo_url}" target="_blank">${esc(r.repo)}</a><div class="desc">${esc(r.desc)}</div></td>
    <td class="up">${esc(r.upstream)}</td>
    <td>${r.missing > 0 ? `<span class="badge b-up">↓ ${r.missing}</span>` : `<span class="badge b-ok">已同步</span>`}</td>
    <td class="up">${esc((r.upstream_pushed||"").slice(0,10))} · ${days(r.upstream_pushed)}</td>
    <td>${r.upstream_stopped ? `<span class="badge b-stop" title="${esc(r.upstream_stop_reason)}">停维护</span>` : '<span class="badge b-gray">正常</span>'}</td>
    <td>${r.has_update ? '<span class="badge b-up">上游有更新</span>' : '<span class="badge b-gray">无更新</span>'}</td>
    <td class="actions">${btn}</td>
  </tr>`;
}

function rowOwn(r){
  return `<tr>
    <td class="name"><a href="${r.repo_url}" target="_blank">${esc(r.repo)}</a><div class="desc">${esc(r.desc)}</div></td>
    <td class="up">${r.upstream ? "上游：" + esc(r.upstream) : (r.fork ? "上游已删除/不可用" : "自有仓库")}</td>
    <td class="up">${esc((r.pushed||"").slice(0,10))} · ${days(r.pushed)}</td>
    <td class="actions"><a href="${r.repo_url}" target="_blank">打开 →</a></td>
  </tr>`;
}

function render(){
  if(!DATA) return;
  const only = $("#onlyUpdates").checked;
  const repos = DATA.repos;
  const mod = repos.filter(r => r.state === "modified" && (!only || r.has_update));
  const pure = repos.filter(r => r.state === "pure" && (!only || r.has_update));
  const own = repos.filter(r => r.state === "own" && !only);
  const err = repos.filter(r => r.state === "error");

  $("#tb-modified").innerHTML = mod.map(rowModified).join("") || `<tr><td colspan="7" class="muted">没有符合条件的仓库</td></tr>`;
  $("#tb-pure").innerHTML = pure.map(rowPure).join("") || `<tr><td colspan="6" class="muted">没有符合条件的仓库</td></tr>`;
  $("#tb-own").innerHTML = own.map(rowOwn).join("") || `<tr><td colspan="4" class="muted">没有符合条件的仓库</td></tr>`;
  if (err.length) {
    $("#tb-own").innerHTML += err.map(r => `<tr><td class="name">${esc(r.repo)}</td><td class="up">无法比较：${esc(r.error||"")}</td><td></td><td></td></tr>`).join("");
  }

  $("#s-total").textContent = DATA.summary.total;
  $("#s-updates").textContent = DATA.summary.updates;
  $("#s-pure").textContent = DATA.summary.pure;
  $("#s-modified").textContent = DATA.summary.modified;
  $("#s-own").textContent = DATA.summary.own;
  $("#s-stopped").textContent = DATA.summary.stopped;
  $("#meta").textContent = `@${DATA.owner} · ${new Date(DATA.fetched_at*1000).toLocaleTimeString()}`;
}

async function load(force){
  $("#refresh").disabled = true; $("#meta").textContent = "读取中…";
  try {
    const r = await fetch("/api/status" + (force ? "?force=1" : ""));
    DATA = await r.json();
    render();
  } catch(e) { toast("读取失败：" + e, true); }
  finally { $("#refresh").disabled = false; }
}

async function sync(repo){
  busy.add(repo); render();
  try {
    const r = await fetch("/api/sync", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({repo})});
    const d = await r.json();
    if (d.ok) { toast(`${repo} 已更新：${(d.result && d.result.merge_type) || "成功"}`); await load(true); }
    else toast(`${repo} 未更新：${d.error}`, true);
  } catch(e) { toast("请求失败：" + e, true); }
  finally { busy.delete(repo); render(); }
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-sync]");
  if (b) sync(b.getAttribute("data-sync"));
});
$("#refresh").onclick = () => load(true);
$("#onlyUpdates").onchange = render;
load(false);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        try:
            if self.path.startswith("/api/status"):
                force = "force=1" in self.path
                self._send(200, json.dumps(get_status(force), ensure_ascii=False))
            elif self.path in ("/", "/index.html"):
                self._send(200, INDEX, "text/html; charset=utf-8")
            else:
                self._send(404, '{"error":"not found"}')
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)[:300]}, ensure_ascii=False))

    def do_POST(self):
        if not self.path.startswith("/api/sync"):
            self._send(404, '{"error":"not found"}')
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            repo = str(payload.get("repo") or "")
            self._send(200, json.dumps(sync_fork(repo), ensure_ascii=False))
        except Exception as e:
            self._send(500, json.dumps({"ok": False, "error": str(e)[:300]}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--owner", default="", help="GitHub 用户名（也可用环境变量 FORK_WATCH_OWNER）")
    args = ap.parse_args()
    global OWNER
    if args.owner:
        OWNER = args.owner
    if not OWNER:
        log("缺少账号：请设置环境变量 FORK_WATCH_OWNER=你的GitHub用户名，或加 --owner 你的用户名")
        raise SystemExit(2)
    # 后台预热：端口先起来，页面不用干等 20 秒
    def warm():
        try:
            get_status(force=True)
        except Exception as e:
            log("预热失败: %s" % e)
    threading.Thread(target=warm, daemon=True).start()
    pidfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dashboard.pid")
    try:
        open(pidfile, "w").write(str(os.getpid()))
    except OSError:
        pass
    import atexit
    atexit.register(lambda: os.path.exists(pidfile) and os.remove(pidfile))
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log("GitHub 项目面板: http://%s:%d" % (args.host, args.port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
