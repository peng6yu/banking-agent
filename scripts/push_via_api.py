#!/usr/bin/env python3
"""把本地领先 origin/main 的改动，经 GitHub Git Data API 推上去。

背景：本机到 github.com:443 被掐断（gnutls recv error / 连接超时），
但 api.github.com 可达、gh 已登录（scopes: gist, read:org, repo, workflow）。
所以走 API：blob → tree（以远端 tip 的 tree 为 base）→ commit → 更新 ref。

不重写历史：新 commit 的父提交就是远端当前 tip，属于快进提交。
本地 102 个提交的完整历史仍在本地，等网络恢复可 `git push --force-with-lease` 还原。
"""
import base64
import json
import subprocess
import sys
from pathlib import Path

REPO = "peng6yu/banking-agent"
BRANCH = "main"


def gh(args, data=None):
    cmd = ["gh", "api"] + args
    if data is not None:
        cmd += ["--input", "-"]
    p = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"gh api 失败: {' '.join(cmd)}\n{p.stderr.strip()[:400]}")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def _upload(raw, path):
    try:
        text = raw.decode("utf-8")
        return gh([f"repos/{REPO}/git/blobs"], {"content": text, "encoding": "utf-8"})
    except UnicodeDecodeError:
        return gh([f"repos/{REPO}/git/blobs"],
                  {"content": base64.b64encode(raw).decode(), "encoding": "base64"})


def git(*args):
    p = subprocess.run(["git", "-c", "core.quotepath=false"] + list(args),
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {p.stderr.strip()[:300]}")
    return p.stdout


def retry(fn, tries=4, wait=5):
    import time as _t
    last = None
    for i in range(tries):
        try:
            return fn()
        except RuntimeError as e:
            last = e
            print(f"  重试 {i + 1}/{tries}（{str(e).splitlines()[-1][:70]}）", flush=True)
            _t.sleep(wait * (i + 1))
    raise last


def main():
    base_ref = gh([f"repos/{REPO}/git/ref/heads/{BRANCH}"])
    base_sha = base_ref["object"]["sha"]
    base_commit = gh([f"repos/{REPO}/git/commits/{base_sha}"])
    base_tree = base_commit["tree"]["sha"]
    print(f"远端 tip: {base_sha[:8]}  tree: {base_tree[:8]}")

    changed = git("diff", "--name-status", "--no-renames", f"{base_sha}..HEAD").strip().splitlines()
    entries, n_blob, n_del = [], 0, 0
    for line in changed:
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if status.startswith("D"):
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            n_del += 1
            continue
        f = Path(path)
        if not f.exists():
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            n_del += 1
            continue
        cache_file = Path("tmp/push_blob_cache.json")
        cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
        key = f"{path}:{f.stat().st_size}:{int(f.stat().st_mtime)}"
        raw = f.read_bytes()
        if key in cache:
            blob = {"sha": cache[key]}
        else:
            blob = _upload(raw, path)
            cache[key] = blob["sha"]
            cache_file.parent.mkdir(exist_ok=True)
            cache_file.write_text(json.dumps(cache, ensure_ascii=False))
        if False:
            pass

        mode = "100755" if (f.stat().st_mode & 0o111) else "100644"
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
        n_blob += 1
        if n_blob % 50 == 0:
            print(f"  已上传 blob {n_blob}…", flush=True)

    print(f"文件: 新增/修改 {n_blob}，删除 {n_del}")
    # 分批串联建树：每批以上一批的 tree 为 base_tree，避免一次性大 payload 被拒（422/502）
    tree_sha = base_tree
    batch = 40
    for i in range(0, len(entries), batch):
        chunk = entries[i:i + batch]
        try:
            got = retry(lambda c=chunk, b=tree_sha: gh(
                [f"repos/{REPO}/git/trees"], {"base_tree": b, "tree": c}))
        except RuntimeError:
            print(f"  第 {i // batch + 1} 批失败，逐条定位…", flush=True)
            for e in chunk:
                try:
                    got = gh([f"repos/{REPO}/git/trees"], {"base_tree": tree_sha, "tree": [e]})
                except RuntimeError as err:
                    print(f"  ✗ 坏条目: {e} {str(err)[:120]}", flush=True)
                    raise
                tree_sha = got["sha"]
            continue
        tree_sha = got["sha"]
        print(f"  建树 {min(i + batch, len(entries))}/{len(entries)}", flush=True)
    tree = {"sha": tree_sha}
    subjects = git("log", "--format=%s", f"{base_sha}..HEAD").strip().splitlines()
    msg = ("界面重构全量同步：一条对话主线 + 五块常驻裁决栏 + 三色确认卡\n\n"
           f"本地 {len(subjects)} 个提交压缩为本次快进提交（历史仍在本地仓库）。\n"
           "主要内容：四入口导航、默认助手页、确认卡三档动作与授权绑定、审计链同屏与篡改检测、\n"
           "移动端/平板断点、接管队列动作、演示台重置、评委一页纸与答辩脚本、全量验收记录。")
    commit = retry(lambda: gh([f"repos/{REPO}/git/commits"],
                              {"message": msg, "tree": tree["sha"], "parents": [base_sha]}))
    updated = gh([f"repos/{REPO}/git/refs/heads/{BRANCH}"],
                 {"sha": commit["sha"], "force": False})
    print(f"远端已更新: {updated['object']['sha'][:8]}  commit: {commit['sha']}")


if __name__ == "__main__":
    sys.exit(main())
