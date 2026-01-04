# 🌐 远程串口 & TCP 被控端 GUI 工具（v1.2）

> 无需公网 IP，轻松将本地串口或内网 TCP 服务（如 SSH、RDP）通过 FRP 映射到公网！

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)

![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## ✨ 功能亮点

- **串口远程透传**：将本地串口（如 COM3）映射为公网 TCP 端口，支持 SecureCRT、PuTTY 等终端以 **RAW 模式**连接。
- **TCP 内网穿透**：一键映射内网任意 TCP 服务（SSH、RDP、Winbox 等）到公网。
- **内置串口-TCP 桥接**：无需额外工具（如 socat），程序自动完成串口 ↔ TCP 转发。
- **外部配置支持**：可通过同目录下的 `config.ini` 文件自定义 FRP 服务器地址、端口和 Token。
- **图形化操作界面**：简洁易用的 Tkinter GUI，支持多连接管理、日志实时显示、状态交互等。
- **安全临时配置**：FRP 配置文件仅在运行时生成，退出后自动清理。

---

## 📦 使用说明

### 1. 前置要求

- Windows 系统（已打包为 `.exe`）
- 已部署 FRP 服务端（frps） 并获取：
    - 服务器 IP 地址
    - 通信端口（默认 `7000`）
    - 安全 Token

### 2. 首次运行

- （可选）创建 `config.ini` 文件来自定义 FRP 服务器信息（格式见下文）。
- 双击运行即可。

> 💡 **注意**：
> 
> - 远程串口连接时，请确保终端软件设置为 **RAW 模式**。
> - PuTTY 用户还需在 **Terminal → Local echo / Local line editing** 中设为 **Force Off**。

---

## ⚙️ 配置文件 `config.ini`（可选）

若未提供此文件，程序将使用内置默认值（需自行修改源码或替换）。

Ini编辑

```
1[frp_server]
2frps_addr = your_frp_server_ip
3frps_port = 7000
4frps_token = your_secure_token_here
```

> 🔒 该文件**不会被程序自动创建**，仅读取已有配置，保障安全性。

---

## 🖥️ 界面功能

|标签页|功能|
|---|---|
|**串口映射**|选择 COM 口、波特率，指定公网端口，一键启动串口穿透|
|**TCP 映射**|输入内网 IP:端口（如 `192.168.1.100:22`），自动生成建议公网端口，支持手动调整|

- **活动连接列表**：实时显示所有映射任务，支持 **启动/停止/删除**。
- **运行中状态**：点击 `[运行中]` 标签可**一键复制公网地址**（如 `www.esun21.com:3000`）。
- **日志面板**：显示串口/TCP 数据流向、FRP 启动日志、错误提示等。

---

## 🛠️ 开发与打包

本项目基于 Python 3.8+ 开发，使用以下技术栈：

- `tkinter`：GUI 界面
- `pyserial`：串口通信
- `socketserver`：串口-TCP 桥接
- `subprocess`：调用 `frpc.exe`
- `PyInstaller`：打包为文件 EXE


---
---
---
# 🌐 Remote Serial & TCP Controller GUI Tool (v1.2)

> Expose local serial ports or internal TCP services (e.g., SSH, RDP) to the public internet—no public IP required! Powered by FRP.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)

![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## ✨ Key Features

- **Remote Serial Port Access**: Map a local COM port (e.g., `COM3`) to a public TCP port. Connect via terminal tools like SecureCRT or PuTTY in **RAW mode**.
- **TCP Tunneling**: One-click expose any internal TCP service (SSH, RDP, Winbox, etc.) through FRP.
- **Built-in Serial-to-TCP Bridge**: No external tools (like `socat`) needed—bidirectional forwarding handled internally.
- **External Configuration Support**: Customize your FRP server settings via an optional `config.ini` file.
- **Graphical User Interface**: Clean, intuitive Tkinter-based UI with multi-connection management and real-time logging.
- **Secure Temporary Configs**: FRP config files are auto-generated at runtime and cleaned up on exit.

---

## 📦 Getting Started

### 1. Prerequisites

- Windows OS (distributed as a standalone `.exe`)
- A running FRP server (`frps`) with:
    - Server IP address
    - Communication port (default: `7000`)
    - Authentication token

### 2. First Run

- (Optional) Create a `config.ini` file to specify your FRP server details (see format below).
- Double-click to launch!

> 💡 **Important Notes**:
> 
> - For serial connections, **set your terminal to RAW mode**.
> - **PuTTY users**: Go to _Connection → Telnet → Local echo_ and _Local line editing_, and set both to **Force Off**.

---

## ⚙️ Configuration File: `config.ini` (Optional)

If this file is missing, the program falls back to built-in defaults (which you can customize in source code).

Ini Edit
```
[frp_server]
frps_addr = your_frp_server_ip
frps_port = 7000
frps_token = your_secure_token_here
```

> 🔒 The program **never creates** this file automatically—it only reads it if present, ensuring security.

---

## 🖥️ UI Overview

|Tab|Function|
|---|---|
|**Serial Mapping**|Select COM port & baud rate, assign a public port, and start remote serial access instantly|
|**TCP Mapping**|Enter internal `IP:Port` (e.g., `192.168.1.100:22`); the tool suggests a public port (editable)|

- **Active Connections Panel**: View all mappings with **Start / Stop / Remove** controls.
- **Live Status Indicator**: Click the `[Running]` label to **copy the public address** (e.g., `www.esun21.com:3000`) to clipboard.
- **Log Console**: Real-time display of data flow (serial ↔ TCP), FRP logs, and error messages.

---

## 🛠️ Development & Packaging

Built with Python 3.8+ and the following stack:

- `tkinter`: GUI framework
- `pyserial`: Serial communication
- `socketserver`: Built-in serial-TCP bridge
- `subprocess`: Launches embedded `frpc.exe`
- `PyInstaller`: Bundled into Windows executable
