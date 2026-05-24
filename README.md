# Mobile-Vibe-Monitor

手机端远程监控与审批 Claude Code vibecoding 变更的实时协作系统。

## 工作原理

```
手机浏览器 ──HTTPS── 公网隧道 ── localhost:8000 ── vibe_server.py
                                                      │
                                           ┌──────────┼──────────┐
                                           │          │          │
                                       进程扫描    Git 轮询   SendKeys
                                           │          │          │
                                      Claude Code   文件变更   键盘干预
```

服务端运行在电脑端，通过进程扫描感知 Claude Code 存活状态，轮询 git diff 检测文件变更。手机端通过 WebSocket 实时接收状态推送，以键盘模拟（SendKeys）向 Claude Code 对话框发送提示词和干预按键。

## 快速开始

### 依赖

```bash
pip install fastapi uvicorn psutil
```

### 启动服务

```bash
python vibe_server.py
```

服务监听 `http://localhost:8000`。

### 内网穿透

使用 cpolar / ngrok 等工具将本地 8000 端口暴露到公网：

```bash
cpolar http 8000
```

手机浏览器访问公网地址即可。

## 功能

- **AI 状态感知** — 实时检测 Claude Code / Kimi 进程是否在线
- **提示词远程发送** — 手机输入提示词，剪贴板粘贴到电脑端 Claude Code 对话框
- **变更审批** — Git diff 轮询检测文件变更，推送至手机端等待审批
- **键盘干预** — 手机端 Yes/Allow/No 按钮映射键盘 1/2/3 键，回答 Claude Code 选择问题
- **工作区检测** — 从 VS Code 窗口标题自动提取当前项目名
- **2/3 选项模式** — 点击图标切换，适配 Claude Code 的二选一/三选一场景

## 前置条件

- 电脑端 Claude Code 窗口必须获得焦点（SendKeys 依赖活动窗口）
- VS Code 需打开项目文件夹（用于工作区名称检测）
- 电脑不可锁屏或休眠

## 文件说明

| 文件 | 说明 |
|------|------|
| `vibe_server.py` | FastAPI + WebSocket 后端 |
| `index.html` | 手机端 SPA 前端 |

## 许可

MIT
