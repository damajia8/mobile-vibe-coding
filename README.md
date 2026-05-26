# Mobile-Vibe-Monitor

手机端远程监控与审批 Claude Code vibecoding 变更的实时协作系统。

## 环境准备

### 1. 电脑端依赖

确保已安装 Python 3.8+，然后安装依赖：

```bash
pip install fastapi uvicorn psutil
```

### 2. 前置条件

- Claude Code 窗口必须**获得焦点**（SendKeys 依赖活动窗口）
- VS Code 需**打开目标项目文件夹**（用于工作区名称自动检测）
- 电脑**不可锁屏或休眠**

## 启动服务

### 第 1 步：启动后端

将 `vibe_server.py` 和 `index.html` 放入目标项目目录，运行：

```bash
cd 目标项目目录
python vibe_server.py
```

启动后服务监听 `http://localhost:8000`。

### 第 2 步：公网穿透

安装 [cpolar](https://www.cpolar.com/)，在新终端中执行：

```bash
cpolar http 8000
```

cpolar 会分配一个公网 HTTPS 地址（如 `https://xxxx.r30.cpolar.top`）。

### 第 3 步：手机访问

手机浏览器打开 cpolar 分配的公网地址。页面加载后右上角绿色圆点亮起表示 WebSocket 已连接。

## 日常操作流程

### 发送提示词

1. 确认页面顶部 **AI 状态** 显示 `Claude Code 持续守护中`（蓝色）
2. 在底部输入框输入提示词，点击右侧箭头发送
3. 提示词会自动粘贴到电脑端 Claude Code 对话框并按回车发送

### 审批代码变更

当 Claude Code 产生文件变更时，手机端会自动拦截：

1. 上下文大厅弹出红色警告 + 代码 Diff 预览
2. 决策栏变为红色 `⚠️ 待决策` 状态
3. 点击按钮做出决策：
   - **Yes** — 单步放行，允许本次变更写入
   - **Allow** — 彻底放权，允许 Claude Code 自主执行后续操作
   - **No** — 拒绝修改，驳回本次变更

4. 手机端自动接收执行结果，上下文大厅即时更新

## 界面说明

| 区域 | 功能 |
|------|------|
| 工作区标识 | 显示当前项目名 + WebSocket 连接状态（绿点在线/红点离线） |
| AI 状态栏 | 实时显示检测到的 AI 插件（Claude Code / Kimi / 断联） |
| 决策栏 | 左侧显示就绪/待决策状态，右侧三个决策按钮 |
| 操作反馈 | 粘贴进度、决策结果等状态提示 |
| 上下文大厅 | AI 代码申请、Diff 预览、操作历史流 |
| 底部输入框 | 输入提示词并发送 |

## 状态流转

```
IDLE ──发送提示词──→ RUNNING ──检测变更──→ WAITING_CONFIRM ──干预按键──→ IDLE
                        │                                                   │
                        └── 无变更 ──────────────────────────────────────────┘
```

- **IDLE** — 空闲，可发送新提示词
- **RUNNING** — Claude Code 正在思考/执行中
- **WAITING_CONFIRM** — 检测到代码变更，等待手机端审批

## 文件说明

| 文件 | 说明 |
|------|------|
| `vibe_server.py` | FastAPI + WebSocket 后端（进程监控、Git 轮询、SendKeys 干预） |
| `index.html` | 手机端 SPA 前端（深色主题、WebSocket 实时通信） |

## 许可

MIT

