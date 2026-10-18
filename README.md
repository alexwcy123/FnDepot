# FnDepot 外部应用源

fnOS（飞牛）外部应用源，遵循 [EWEDLCM/FnDepot](https://github.com/EWEDLCM/FnDepot) V2 编写规范，收录以下应用，并通过 GitHub Actions **每日自动检查上游 Release 更新版本**。

## 收录应用

| 应用 | 显示名 | 架构 | 当前版本 | 说明 |
| --- | --- | --- | --- | --- |
| [fnmedia-ext](fnmedia-ext/README.md) | 飞牛影视在线扩展 | all | 0.1.11 | 飞牛影视在线资源扩展：原生搜索聚合、豆瓣榜单四库、资源站海量分类四库、多线路播放、播放记录与续播 |
| [lemon-music](lemon-music/README.md) | 柠檬音乐 | x86 / arm | 1.2.17.10 | Web 音乐工具：多平台搜歌下载、本地音乐库管理、标签整理、逐字歌词播放，兼容落雪自定义音源 |

安装包与图标均直链上游仓库官方资源（GitHub Releases / raw），本仓库不保存二进制文件。

## 在 fnOS 中添加

应用中心 → 设置 → 外部源（应用源）→ 添加：

```text
https://github.com/alexwcy123/FnDepot
```

> 客户端需 v0.0.7 以上才能解析 V2 源。需要 fntermx / picoclaw / flatcms / flatnas 等应用的用户，请同时添加官方源 <https://github.com/EWEDLCM/FnDepot>。

## 自动更新机制

`.github/workflows/auto-update-apps.yml` 每天 UTC 18:00（北京时间 02:00）运行 `scripts/update_apps.py`：检查 `scripts/tracked_apps.json` 中登记的上游仓库 Release，发现新版本即自动写入 `fnpack.json` 并提交推送。已发布版本不会被覆盖，SHA256 来自 GitHub 官方计算值。

详细设计、验证记录与维护方法见 [新增应用与自动更新说明.md](新增应用与自动更新说明.md)。

## 目录结构

```text
├── fnpack.json                       # 源索引（V2）
├── scripts/
│   ├── update_apps.py                # 自动更新脚本（仅标准库）
│   └── tracked_apps.json             # 跟踪的上游仓库清单
├── .github/workflows/
│   └── auto-update-apps.yml          # 每日自动检查工作流
├── fnmedia-ext/README.md             # 应用说明
└── lemon-music/README.md             # 应用说明
```
