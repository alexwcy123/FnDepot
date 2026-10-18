#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_apps.py — 自动检查上游 GitHub Releases 并更新 fnpack.json

用法:
    python scripts/update_apps.py                     # 检查全部跟踪应用并写入 fnpack.json
    python scripts/update_apps.py --dry-run           # 只检查不写入（本地测试用）
    python scripts/update_apps.py --app fnmedia-ext   # 只处理指定应用（可重复传入）
    python scripts/update_apps.py --max-releases 50   # 每个仓库扫描的 Release 数量上限

工作机制:
    1. 读取 scripts/tracked_apps.json 中登记的上游仓库;
    2. 调用 GitHub Releases API 获取正式版 Release（自动跳过 draft；
       include_prerelease 为 true 时才纳入预发布）;
    3. 与 fnpack.json 中该应用已登记的最高版本比较，仅添加严格更新的版本，
       已存在的版本键一律跳过（不静默替换已发布版本）;
    4. 新版本的 download_url / size / sha256 全部取自 GitHub Release 资产:
       sha256 优先使用 API 返回的资产 digest（GitHub 上传时官方计算），
       其次读取随附的 .sha256 资产，最后下载 FPK 自行计算;
    5. 只重写 fnpack.json 中对应应用的 releases 节点并按版本降序排列，
       应用级元数据（display_name / desc / icon_url 等）不受影响。

依赖: 仅 Python 标准库（3.8+），可直接在 GitHub Actions 运行。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FNPACK = ROOT / "fnpack.json"
CONFIG = Path(__file__).resolve().parent / "tracked_apps.json"
API = "https://api.github.com"
CST = timezone(timedelta(hours=8))  # 东八区，与 fnpack.json 的 updated_at 惯例一致
CHANGELOG_MAX = 600
USER_AGENT = "FnDepot-auto-updater"


# ---------------------------------------------------------------- 基础工具

def gh_get(url, token=None):
    """请求 GitHub API 并返回解析后的 JSON。"""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError("GitHub API 请求失败 HTTP %s: %s %s" % (e.code, url, detail))


def fetch_text(url, token=None):
    """下载文本内容（用于随附的 .sha256 资产）。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def sha256_of_url(url, token=None):
    """流式下载并计算 SHA256，返回 (hex, bytes)。"""
    h = hashlib.sha256()
    total = 0
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=600) as resp:
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


# ---------------------------------------------------------------- 版本处理

def _suffix_key(suffix):
    """预发布后缀按 SemVer 规则逐段比较：数字段按数值，字母段按字符串。"""
    ids = []
    for part in suffix.split("."):
        if part.isdigit():
            ids.append((0, int(part), ""))
        else:
            ids.append((1, 0, part))
    return tuple(ids)


def version_key(raw):
    """把版本号解析为可比较元组；无法解析时返回 None。

    例: 'v1.2.17.10' -> ((1,2,17,10), 1, ())
        '1.0.0-beta.1' -> ((1,0,0,0), 0, ((1,0,'beta'), (0,1,'')))  # 预发布排在正式版之前
    """
    v = (raw or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    m = re.match(r"^(\d+(?:\.\d+)*)(?:[-+]([0-9A-Za-z.+-]+))?$", v)
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = (nums + (0,) * 4)[:4] if len(nums) <= 4 else nums
    suffix = m.group(2) or ""
    return (nums, 0 if suffix else 1, _suffix_key(suffix))


def version_str(raw):
    """tag_name -> fnpack.json 版本键（去掉前缀 v）。"""
    return re.sub(r"^[vV]", "", (raw or "").strip())


def detect_arch(filename):
    """从资产文件名识别架构：x86 / arm / all。"""
    if re.search(r"[-_.]x86([-_.]|$)", filename, re.I) or \
       re.search(r"[-_.]amd64([-_.]|$)", filename, re.I):
        return "x86"
    if re.search(r"[-_.](arm64|aarch64|arm)([-_.]|$)", filename, re.I):
        return "arm"
    return "all"


# ---------------------------------------------------------------- 字段加工

def sha256_from_digest(asset):
    """优先使用 GitHub 上传时官方计算的资产 digest。"""
    d = (asset.get("digest") or "").strip()
    m = re.match(r"^sha256:([0-9a-fA-F]{64})$", d)
    return m.group(1).lower() if m else None


def parse_sha256_text(text):
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    return m.group(1).lower() if m else None


def clean_changelog(body, name):
    """Release body -> 纯文本 changelog（去 markdown 记号，限长）。"""
    text = (body or "").strip()
    if text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for line in text.split("\n"):
            line = re.sub(r"^#{1,6}\s*", "", line)
            line = re.sub(r"^>\s?", "", line)
            if line.lstrip().startswith("|"):
                # markdown 表格 -> "键：值" 文本行，丢弃分隔行
                cells = [re.sub(r"[*_`]{1,2}", "", c.strip())
                         for c in line.strip().strip("|").split("|")]
                if cells and not all(re.match(r"^:?-+:?$", c) for c in cells if c):
                    lines.append("：".join(c for c in cells if c))
                continue
            line = re.sub(r"^[-*+]\s+", "", line)
            line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
            line = re.sub(r"[*_`]{1,2}", "", line)
            lines.append(line.strip())
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        text = (name or "").strip()
    if len(text) > CHANGELOG_MAX:
        text = text[:CHANGELOG_MAX].rstrip() + "……"
    return text


def to_cst(iso_utc):
    """GitHub UTC 时间 -> 东八区 ISO 8601（与 fnpack.json 惯例一致）。"""
    dt = datetime.fromisoformat((iso_utc or "").replace("Z", "+00:00"))
    return dt.astimezone(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


# ---------------------------------------------------------------- 主流程

def process_app(app_cfg, fnpack, token, max_releases):
    """检查单个应用，返回 (新增版本列表, 跳过信息列表)。"""
    appname = app_cfg["appname"]
    repo = app_cfg["repo"]
    apps = fnpack.get("apps", {})
    if appname not in apps:
        raise RuntimeError(
            "fnpack.json 的 apps 中不存在 %r，请先人工补齐应用级元数据再纳入自动更新" % appname)

    releases = apps[appname].setdefault("releases", {})
    keys = [version_key(k) for k in releases]
    cur_max = max((k for k in keys if k), default=None)

    data = gh_get("%s/repos/%s/releases?per_page=%d" % (API, repo, max_releases), token)
    added, skipped = [], []

    for rel in data:  # API 按发布时间倒序返回
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not app_cfg.get("include_prerelease", False):
            continue
        tag = rel.get("tag_name", "")
        vkey = version_key(tag)
        if not vkey:
            skipped.append("%s（版本号无法解析）" % tag)
            continue
        if cur_max is not None and vkey <= cur_max:
            continue  # 已登记或更旧的版本一律跳过，不静默替换

        fpks = [a for a in rel.get("assets", []) if a["name"].lower().endswith(".fpk")]
        if not fpks:
            skipped.append("%s（无 FPK 资产）" % tag)
            continue

        packages = {}
        for asset in fpks:
            arch = detect_arch(asset["name"])
            if arch in packages:
                skipped.append("%s 存在多个 %s 包，已忽略 %s" % (tag, arch, asset["name"]))
                continue
            sha = sha256_from_digest(asset)
            if not sha:
                side = next((s for s in rel.get("assets", [])
                             if s["name"] == asset["name"] + ".sha256"), None)
                if side:
                    sha = parse_sha256_text(fetch_text(side["browser_download_url"], token))
            if not sha:
                sha, _ = sha256_of_url(asset["browser_download_url"], token)
            packages[arch] = {
                "download_url": asset["browser_download_url"],
                "sha256": sha,
                "size": asset.get("size"),
            }

        entry = {
            "changelog": clean_changelog(rel.get("body"), rel.get("name")),
            "updated_at": to_cst(rel.get("published_at")),
            "packages": packages,
        }
        if app_cfg.get("os_min_version"):
            entry["os_min_version"] = app_cfg["os_min_version"]

        releases[version_str(tag)] = entry
        added.append(version_str(tag))
        cur_max = vkey  # 同一次运行中只收录最新版本，避免补录旧版

    # 版本键按降序排列，便于阅读；无法解析的版本排在最后
    apps[appname]["releases"] = dict(sorted(
        releases.items(),
        key=lambda kv: (version_key(kv[0]) is not None,
                        version_key(kv[0]) or ((0, 0, 0, 0), 0, "")),
        reverse=True,
    ))
    return added, skipped


def main():
    ap = argparse.ArgumentParser(description="自动检查上游 GitHub Releases 并更新 fnpack.json")
    ap.add_argument("--dry-run", action="store_true", help="只检查不写入文件")
    ap.add_argument("--app", action="append", dest="only",
                    help="只处理指定 appname，可重复传入")
    ap.add_argument("--max-releases", type=int, default=30,
                    help="每个仓库扫描的 Release 数量上限（默认 30）")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("提示: 未设置 GITHUB_TOKEN，使用匿名访问 GitHub API（每小时限额较低）")

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    fnpack = json.loads(FNPACK.read_text(encoding="utf-8"))

    targets = [a for a in cfg.get("apps", []) if not args.only or a["appname"] in args.only]
    if not targets:
        print("没有匹配的跟踪应用（--app 过滤可能写错了）", file=sys.stderr)
        sys.exit(2)

    changed = False
    failures = 0
    for app_cfg in targets:
        name = app_cfg["appname"]
        try:
            added, skipped = process_app(app_cfg, fnpack, token, args.max_releases)
        except Exception as e:  # 单个应用失败不影响其他应用
            failures += 1
            print("[%s] 检查失败: %s" % (name, e), file=sys.stderr)
            continue
        if added:
            changed = True
            print("[%s] 新增版本: %s" % (name, ", ".join(added)))
        else:
            print("[%s] 已是最新，无新版本" % name)
        for s in skipped:
            print("[%s] 跳过: %s" % (name, s))

    if changed and not args.dry_run:
        FNPACK.write_text(json.dumps(fnpack, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        print("fnpack.json 已更新")
    elif args.dry_run:
        print("dry-run 模式：未写入任何文件")

    if failures and failures == len(targets):
        sys.exit(1)  # 全部失败时以非零码退出，便于工作流发现问题


if __name__ == "__main__":
    main()
