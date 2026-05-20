#!/usr/bin/env python3
"""
py2exe-github — 把 .py 上传 GitHub，自动编译成 .exe 并下载回来

用法:
  py2exe /path/to/script.py              # 构建单个文件
  py2exe /path/to/script.py -o ./dist    # 指定输出目录
  py2exe /path/to/script.py --gui        # GUI 模式 (无控制台窗口)
  py2exe -i script.py                    # 同上，快捷用法
  py2exe --token "新token"               # 更新 GitHub Token
  py2exe --reinit                        # 重新创建仓库

首次运行自动:
  1. 提示输入 GitHub Token (保存至 ~/.py2exe/config.json)
  2. 创建 GitHub 仓库 py2exe-builder (含 Actions workflow)
  3. 推送文件并等待构建完成
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import requests

# ── Constants ──
CONFIG_DIR = Path.home() / ".py2exe"
CONFIG_FILE = CONFIG_DIR / "config.json"
WORKFLOW_FILE = Path.home() / "tools" / "py2exe-github" / "build.yml"
REPO_NAME = "py2exe-builder"
API = "https://api.github.com"
POLL_INTERVAL = 15
TIMEOUT = 600


# ══════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════

def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}

def save_config(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    CONFIG_FILE.chmod(0o600)

def get_token():
    cfg = load_config()
    token = cfg.get("token")
    if not token:
        print("=" * 56)
        print("  首次使用，请输入 GitHub Personal Access Token")
        print("  (需勾选 repo + workflow 权限)")
        print("=" * 56)
        token = input("  Token: ").strip()
        if not token.startswith(("github_pat_", "ghp_")):
            print("  ⚠ Token 格式异常，但仍将保存（预期以 ghp_ 或 github_pat_ 开头）")
        cfg["token"] = token
        save_config(cfg)
        print("  ✅ Token 已保存至 ~/.py2exe/config.json\n")
    return token


# ══════════════════════════════════════════════════
# GitHub API helpers
# ══════════════════════════════════════════════════

def gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

def _req(method, url, token, **kw):
    r = requests.request(method, url, headers=gh_headers(token), **kw)
    if r.status_code >= 400:
        print(f"  ⚠ {method} {url.rsplit('/', 1)[-1]} → {r.status_code}", flush=True)
    return r

def gh_get(url, token):        return _req("GET", url, token)
def gh_post(url, token, **kw): return _req("POST", url, token, json=kw.get("json") or {})
def gh_put(url, token, **kw):  return _req("PUT", url, token, json=kw.get("json"))
def gh_patch(url, token, **kw):return _req("PATCH", url, token, json=kw.get("json"))
def gh_del(url, token):        return _req("DELETE", url, token)


# ══════════════════════════════════════════════════
# Core
# ══════════════════════════════════════════════════

def get_user(token):
    r = gh_get(f"{API}/user", token)
    return r.json() if r.status_code == 200 else None

def ensure_repo(token, username):
    r = gh_get(f"{API}/repos/{username}/{REPO_NAME}", token)
    if r.status_code == 200:
        print(f"  ✅ 仓库就绪: {username}/{REPO_NAME}")
        return r.json()
    if r.status_code != 404:
        print(f"  ❌ 检查仓库失败: {r.text[:200]}"); sys.exit(1)
    print(f"  📦 创建仓库 {username}/{REPO_NAME} ...")
    r2 = gh_post(f"{API}/user/repos", token, json={
        "name": REPO_NAME, "private": False, "auto_init": True,
        "description": "Auto-build .py → .exe via GitHub Actions",
    })
    if r2.status_code in (200, 201):
        print("  ✅ 仓库创建成功"); return r2.json()
    print(f"  ❌ 创建失败: {r2.text[:300]}"); sys.exit(1)

def get_file_sha(token, username, path, branch="main"):
    r = gh_get(f"{API}/repos/{username}/{REPO_NAME}/contents/{path}?ref={branch}", token)
    if r.status_code == 200:
        return r.json()["sha"]
    return None

def push_files(token, username, branch, files_dict, commit_msg):
    """Push multiple files atomically using the Contents API."""
    print(f"  📤 推送 {len(files_dict)} 个文件到 {branch} ...", flush=True)
    for file_path, content in files_dict.items():
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        url = f"{API}/repos/{username}/{REPO_NAME}/contents/{file_path}"
        data = {"message": commit_msg, "content": encoded, "branch": branch}
        sha = get_file_sha(token, username, file_path, branch)
        if sha:
            data["sha"] = sha
        r = gh_put(url, token, json=data)
        if r.status_code not in (200, 201):
            print(f"  ❌ 推送 {file_path} 失败: {r.text[:200]}"); return False
        print(f"    ✓ {file_path}", flush=True)
    return True

def wait_for_build(token, username, timeout=TIMEOUT):
    """Poll Actions runs until latest completed or timeout."""
    url = f"{API}/repos/{username}/{REPO_NAME}/actions/runs?per_page=5&branch=main"
    start = time.time()
    last_id = None
    dots = 0
    print(f"  ⏳ 等待 GitHub Actions 构建 ...", end="", flush=True)

    while time.time() - start < timeout:
        r = gh_get(url, token)
        if r.status_code == 200:
            runs = r.json().get("workflow_runs", [])
            if runs:
                run = runs[0]  # latest
                run_id = run["id"]
                status = run["status"]
                conclusion = run.get("conclusion")

                if run_id != last_id:
                    last_id = run_id
                    dots = 0
                    print(f"\r  ⏳ Run #{run_id}: {status:15}", end="", flush=True)
                else:
                    dots += 1
                    if dots % 4 == 0:
                        print(".", end="", flush=True)

                if status == "completed":
                    print(f"\r  {'✅' if conclusion == 'success' else '❌'} Actions 构建: {conclusion}", flush=True)
                    if conclusion == "success":
                        art_url = f"{API}/repos/{username}/{REPO_NAME}/actions/runs/{run_id}/artifacts"
                        ra = gh_get(art_url, token)
                        if ra.status_code == 200:
                            arts = ra.json().get("artifacts", [])
                            if arts:
                                return arts[0]["archive_download_url"], run["html_url"]
                        print("  ⚠ 未找到构建产物")
                        return None, run["html_url"]
                    else:
                        print(f"  📋 详情: {run['html_url']}")
                        return None, run["html_url"]
        time.sleep(POLL_INTERVAL)

    print("\n  ⏰ 等待超时 (10分钟)")
    return None, None

def download_exe(token, dl_url, output_dir, source_name):
    """Download artifact zip and extract .exe files."""
    print(f"  📥 下载构建产物 ...", flush=True)
    r = requests.get(dl_url, headers=gh_headers(token))
    if r.status_code != 200:
        print(f"  ❌ 下载失败: {r.status_code}"); return []

    zip_path = output_dir / f"{source_name}_artifact.zip"
    zip_path.write_bytes(r.content)

    exe_files = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith(".exe"):
                dest = output_dir / Path(name).name
                with zf.open(name) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                os.chmod(dest, 0o755)
                exe_files.append(dest)
                print(f"    ✅ {dest}", flush=True)

    zip_path.unlink()
    return exe_files

def check_token_scopes(token):
    r = requests.get(f"{API}/", headers=gh_headers(token))
    scopes = r.headers.get("X-OAuth-Scopes", "")
    return scopes, "repo" in scopes, "workflow" in scopes


# ══════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="把 .py 上传 GitHub Actions 自动编译成 .exe",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  py2exe my_script.py                    # 构建
  py2exe my_script.py -o ./dist          # 指定输出目录
  py2exe my_script.py --gui              # GUI 模式 (无控制台窗口)
  py2exe my_script.py --name myapp       # 指定 exe 文件名
  py2exe --token "ghp_xxx"               # 更新 Token
  py2exe --reinit                        # 重建仓库
        """,
    )
    parser.add_argument("file", nargs="?", help="要构建的 .py 文件")
    parser.add_argument("-o", "--output", default="./dist", help="exe 输出目录 (默认: ./dist)")
    parser.add_argument("-i", dest="file", help="指定输入文件 (快捷)")
    parser.add_argument("--gui", action="store_true", help="GUI 模式 (--noconsole)")
    parser.add_argument("--name", help="指定 exe 文件名 (不含 .exe)")
    parser.add_argument("--reinit", action="store_true", help="重建 GitHub 仓库")
    parser.add_argument("--token", help="更新 GitHub Token")
    args = parser.parse_args()

    # ――― Token update ―――
    if args.token:
        cfg = load_config()
        cfg["token"] = args.token
        save_config(cfg)
        print("✅ Token 已更新")
        return

    # ――― Reinit ―――
    if args.reinit:
        token = get_token()
        user = get_user(token)
        if not user: print("❌ Token 无效"); sys.exit(1)
        gh_del(f"{API}/repos/{user['login']}/{REPO_NAME}", token)
        print("  🗑 已删除旧仓库")
        ensure_repo(token, user["login"])
        return

    # ――― Validate file ―――
    py_path = Path(args.file).resolve() if args.file else None
    if not py_path or not py_path.exists() or py_path.suffix != ".py":
        print("❌ 请指定有效的 .py 文件")
        parser.print_help()
        sys.exit(1)

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_name = py_path.stem
    exe_name = args.name or source_name

    print(f"\n🔨 py2exe — 自动编译 .exe")
    print(f"  {'─' * 48}")
    print(f"  源文件: {py_path}")
    print(f"  输出:   {output_dir}/")
    print()

    # ――― Auth ―――
    token = get_token()
    scopes, has_repo, has_wf = check_token_scopes(token)
    if not has_repo or not has_wf:
        print(f"  ⚠ Token 权限: {scopes}")
        print(f"  ⚠ 缺少 repo 或 workflow 权限，请重新生成 token")
        if input("  继续? (Y/n): ").strip().lower() == "n":
            sys.exit(1)

    user = get_user(token)
    if not user: print("❌ Token 无效"); sys.exit(1)
    username = user["login"]
    print(f"  👤 {username}")

    # ――― Repo ―――
    ensure_repo(token, username)

    # ――― Prepare files ―――
    source_code = py_path.read_text(encoding="utf-8")
    req_file = py_path.parent / "requirements.txt"
    requirements = req_file.read_text(encoding="utf-8") if req_file.exists() else ""
    workflow_content = WORKFLOW_FILE.read_text(encoding="utf-8")

    files = {f"{exe_name}.py": source_code, ".github/workflows/build.yml": workflow_content}
    if requirements:
        files["requirements.txt"] = requirements

    # If GUI mode, adjust the workflow on the fly
    if args.gui:
        files[".github/workflows/build.yml"] = workflow_content.replace("--console", "--noconsole")

    # ――― Push ―――
    branch = "main"
    commit_msg = f"build: {exe_name}.py → {exe_name}.exe"
    if not push_files(token, username, branch, files, commit_msg):
        print("❌ 推送失败"); sys.exit(1)

    # ――― Wait & download ―――
    print()
    dl_url, run_url = wait_for_build(token, username)

    if dl_url:
        exes = download_exe(token, dl_url, output_dir, source_name)
        if exes:
            print(f"\n{'=' * 50}")
            print(f"  ✅ 构建成功!")
            for exe in exes:
                print(f"     📦 {exe}  ({exe.stat().st_size / 1024:.0f} KB)")
            print(f"{'=' * 50}")
        else:
            print("  ⚠ 未提取到 .exe 文件")
    elif run_url:
        print(f"\n  📋 Actions 详情: {run_url}")

if __name__ == "__main__":
    main()
