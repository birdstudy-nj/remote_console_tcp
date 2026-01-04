# -*- coding: utf-8 -*-
"""
远程串口 & TCP 被控端 GUI 工具 (v1.2)
功能：
  - 将本地串口通过 FRP 映射到公网（支持 RAW 模式终端）
  - 将本地 TCP 服务（如 SSH、RDP）通过 FRP 映射到公网
  - 支持从外部 config.ini 加载 FRP 服务器配置
  - 内置串口-TCP 桥接（无需额外工具）

作者：Frank.Ni
官网：www.esun21.com
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk
import serial
import serial.tools.list_ports
import threading
import socketserver
import subprocess
import configparser
import os
import sys
import socket
import time
import tempfile


# --- 资源路径处理 ---
# 用于 PyInstaller 打包后正确访问资源文件（如 frpc.exe、logo256.ico）
def resource_path(relative_path):
    """
    获取资源文件的绝对路径。
    在 PyInstaller 打包后，资源会被解压到 _MEIPASS 目录；
    开发时则使用当前工作目录。
    """
    try:
        # 如果是 PyInstaller 打包后的环境，sys._MEIPASS 指向临时资源目录
        base_path = sys._MEIPASS
    except Exception:
        # 否则使用当前脚本所在目录
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# --- 程序元信息 ---
APP_VERSION = "v1.2"
UPDATE_NOTES = [
    "• 新增：关于说明",
    "• 新增：外部配置文件config.ini",
    "• 改进：界面信息细节",
]


# FRPC_EXEC 指向打包后的 frpc.exe 路径（内部资源）
FRPC_EXEC = resource_path('frpc.exe')

# --- 默认 FRP 服务器配置（内置，不暴露到文件）---
# 当 config.ini 不存在或读取失败时使用
DEFAULT_FRP_CONFIG = {
    "addr": "your_frp_server_ip_address",   # FRP 服务器地址
    "port": 7000,                          # FRP 服务器端口
    "token": "your_frp_server_secure_token"  # 认证令牌
}


# --- 安全加载 config.ini：仅读取，绝不创建 ---
def load_frp_config():
    """
    从程序运行目录加载 config.ini 中的 FRP 服务器配置。
    优先级：外部 config.ini > 内部默认值。
    注意：此函数不会创建 config.ini，仅读取已有文件。
    返回：(addr, port, token)
    """
    # 1. 获取程序运行的根目录 (即 EXE 所在的物理目录)
    if getattr(sys, 'frozen', False):
        # 如果是打包后的环境（PyInstaller）
        base_dir = os.path.dirname(sys.executable)
    else:
        # 如果是源代码运行环境
        base_dir = os.path.dirname(os.path.abspath(__file__))

    # 2. 定义外部配置文件路径（与 exe 同级）
    external_config = os.path.join(base_dir, "config.ini")

    # 3. 优先级判断：优先外部，其次内部
    if os.path.exists(external_config):
        config_path = external_config
    else:
        # 都没有则直接返回默认值
        return DEFAULT_FRP_CONFIG["addr"], DEFAULT_FRP_CONFIG["port"], DEFAULT_FRP_CONFIG["token"]

    # 4. 执行读取
    cfg = configparser.ConfigParser()
    try:
        # 指定 utf-8 编码，防止用户手动编辑 ini 后出现乱码
        cfg.read(config_path, encoding='utf-8')
        frps_addr = cfg.get("frp_server", "frps_addr", fallback=DEFAULT_FRP_CONFIG["addr"])
        frps_port = cfg.getint("frp_server", "frps_port", fallback=DEFAULT_FRP_CONFIG["port"])
        frps_token = cfg.get("frp_server", "frps_token", fallback=DEFAULT_FRP_CONFIG["token"])
        return frps_addr, frps_port, frps_token
    except Exception:
        # 如果文件损坏或读取失败，返回默认值
        return DEFAULT_FRP_CONFIG["addr"], DEFAULT_FRP_CONFIG["port"], DEFAULT_FRP_CONFIG["token"]


# 全局变量：加载 FRP 服务器配置（只在启动时加载一次）
SERVER_ADDR, SERVER_PORT, SERVER_TOKEN = load_frp_config()


# ---------------- 串口 TCP 桥 ----------------
class SerialBridge:
    """
    实现串口 ↔ TCP 的双向桥接。
    使用 socketserver 创建 TCP 服务，将串口数据转发给 TCP 客户端，反之亦然。
    """

    def __init__(self, serial_port, baudrate, local_port, log_func):
        self.serial_port = serial_port  # 串口号，如 "COM3"
        self.baudrate = baudrate        # 波特率
        self.local_port = local_port    # 本地监听的 TCP 端口（用于 FRP 连接）
        self.log = log_func             # 日志回调函数
        self.ser = None                 # serial.Serial 对象
        self.server = None              # socketserver.TCPServer 对象
        self.thread = None              # 服务器线程
        self.running = False            # 运行状态标志

    def start(self):
        """启动串口桥"""
        try:
            # 打开串口
            self.ser = serial.Serial(
                self.serial_port,
                self.baudrate,
                timeout=0.1,            # 读超时
                write_timeout=1.0,      # 写超时
                inter_byte_timeout=0.05 # 字节间超时
            )
        except Exception as e:
            self.log(f"[ERROR] 打开串口失败 {self.serial_port}: {e}")
            return False

        # 动态创建 TCP 请求处理器（闭包捕获 ser 和 log）
        handler = self.make_handler()
        # 创建多线程 TCP 服务器（监听所有接口）
        self.server = socketserver.ThreadingTCPServer(('0.0.0.0', self.local_port), handler)
        self.server.allow_reuse_address = True  # 允许端口复用

        # 启动服务器线程
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True
        self.log(f"[INFO] 串口桥启动: {self.serial_port}@{self.baudrate} → TCP {self.local_port}")
        return True

    def stop(self):
        """停止串口桥"""
        if not self.running:
            return

        self.running = False
        self.log(f"[INFO] 正在停止串口桥: {self.serial_port}")

        # 安全关闭串口
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception as e:
            self.log(f"[WARN] 关闭串口时出错: {e}")

        # 关闭 TCP 服务器
        try:
            if self.server:
                self.server.shutdown()      # 停止 serve_forever
                self.server.server_close()  # 关闭 socket
        except Exception as e:
            self.log(f"[WARN] server shutdown 出错: {e}")

        # 等待线程结束（最多 2 秒）
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

        self.log(f"[INFO] 串口桥已停止: {self.serial_port}")

    def make_handler(self):
        """
        返回一个动态生成的 TCP 请求处理器类。
        使用闭包捕获当前实例的 ser 和 log。
        """
        ser = self.ser
        log = self.log

        class Handler(socketserver.BaseRequestHandler):
            """处理每个 TCP 客户端连接"""

            def handle(self):
                self.request.settimeout(0.5)  # 设置 socket 超时
                client_addr = self.client_address
                log(f"[INFO] 新客户端连接: {client_addr}")

                stop_event = threading.Event()  # 用于协调两个方向的线程退出

                def serial_to_tcp():
                    """串口 → TCP 方向"""
                    try:
                        while not stop_event.is_set():
                            data = ser.read(1024)  # 从串口读取
                            if data:
                                try:
                                    self.request.sendall(data)  # 发送给 TCP 客户端
                                    # 尝试解码并记录日志（忽略无法解码的字节）
                                    decoded = data.decode('utf-8', errors='replace')
                                    for line in decoded.splitlines(keepends=True):
                                        log(f"[SERIAL → TCP] {line.rstrip()}")
                                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                                    log(f"[WARN] 客户端 {client_addr} 断开 (串口→TCP): {e}")
                                    break
                    except Exception as e:
                        log(f"[ERROR] 串口读取异常: {e}")
                    finally:
                        stop_event.set()

                def tcp_to_serial():
                    """TCP → 串口方向"""
                    try:
                        while not stop_event.is_set():
                            try:
                                data = self.request.recv(1024)  # 从 TCP 客户端接收
                                if not data:
                                    log(f"[INFO] 客户端 {client_addr} 正常关闭连接")
                                    break
                                try:
                                    ser.write(data)  # 写入串口
                                    decoded = data.decode('utf-8', errors='replace')
                                    for line in decoded.splitlines(keepends=True):
                                        log(f"[TCP → SERIAL] {line.rstrip()}")
                                except serial.SerialException as e:
                                    log(f"[ERROR] 串口写入失败: {e}")
                                    break
                            except socket.timeout:
                                continue  # 超时继续循环
                            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                                log(f"[WARN] 客户端 {client_addr} 断开: {e}")
                                break
                    except Exception as e:
                        log(f"[ERROR] TCP接收异常: {e}")
                    finally:
                        stop_event.set()

                # 启动两个方向的线程
                t1 = threading.Thread(target=serial_to_tcp, daemon=True)
                t2 = threading.Thread(target=tcp_to_serial, daemon=True)
                t1.start()
                t2.start()

                # 等待任一线程结束
                try:
                    while t1.is_alive() or t2.is_alive():
                        t1.join(timeout=0.1)
                        t2.join(timeout=0.1)
                except KeyboardInterrupt:
                    pass

                stop_event.set()
                log(f"[INFO] 客户端 {client_addr} 会话结束")

        return Handler


# ---------------- FRP 连接管理 ----------------
class FRPConnection:
    """
    表示一个 FRP 映射连接（串口或 TCP）。
    包含配置、子进程、UI 引用等。
    """

    def __init__(self, local_ip, local_port, remote_port, ini_path, conn_type="tcp", serial_port=None):
        self.local_ip = local_ip          # 本地 IP（串口时为 127.0.0.1）
        self.local_port = local_port      # 本地端口（串口时为桥接端口）
        self.remote_port = remote_port    # 公网映射端口
        self.ini_path = ini_path          # 临时 FRP 配置文件路径
        self.conn_type = conn_type        # "serial" 或 "tcp"
        self.serial_port = serial_port    # 仅串口连接有效
        self.process = None               # subprocess.Popen 对象
        self.status = "停止"              # "停止" / "运行中"
        self.frame = None                 # UI Frame 引用 (frame, prefix_lbl, status_lbl)
        self.buttons = {}                 # UI 按钮引用
        self.bridge = None                # SerialBridge 实例（仅串口）
        self.process_lock = threading.Lock()  # 保护 process 访问
        self.is_temp_ini = True           # 标记是否为临时配置文件（用于清理）
        self.baudrate = None              # 串口波特率（仅串口）


# ---------------- GUI 主窗口 ----------------
class FRPGUI:
    """主 GUI 界面类"""

    def __init__(self, root):
        self.root = root
        self.root.title("远程串口 & TCP被控端")
        self.root.geometry("400x600")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)  # 绑定关闭事件

        self.connections = []   # 存储所有 FRPConnection 实例
        self.closing = False    # 正在关闭标志
        self.close_start_time = None  # 关闭开始时间（用于超时）

        # ====== 顶部：Notebook + “关于”按钮 ======
        top_bar = tk.Frame(root)
        top_bar.pack(fill=tk.X, padx=10, pady=(10, 0))

        # 创建 Notebook（标签页）
        notebook = ttk.Notebook(top_bar)
        notebook.pack(fill=tk.X)

        # “关于”按钮：使用 place 布局固定在右上角
        about_btn = tk.Button(
            top_bar,
            text="关于",
            command=self.show_about,
            relief=tk.FLAT,
            fg="gray",
            font=("TkDefaultFont", 9),
            cursor="hand2",
            bd=0,
            activeforeground="black",
            bg=root.cget("bg")  # 背景色与窗口一致
        )
        about_btn.place(relx=1.0, y=2, x=0, anchor="ne")  # 右上角对齐

        # 悬停效果
        about_btn.bind("<Enter>", lambda e: about_btn.config(fg="black"))
        about_btn.bind("<Leave>", lambda e: about_btn.config(fg="gray"))

        # 创建两个标签页
        serial_frame = ttk.Frame(notebook)
        notebook.add(serial_frame, text="串口映射")
        tcp_frame = ttk.Frame(notebook)
        notebook.add(tcp_frame, text="TCP映射")

        # 初始化页面内容
        self.setup_serial_page(serial_frame)
        self.setup_tcp_page(tcp_frame)

        # ====== 活动连接列表（带滚动条）======
        conn_frame = tk.Frame(root, height=150)
        conn_frame.pack(fill=tk.X, pady=5, padx=10)
        conn_frame.pack_propagate(False)  # 固定高度
        tk.Label(conn_frame, text="活动连接列表：").pack(anchor=tk.W)

        # 使用 Canvas + Scrollbar 实现可滚动区域
        self.canvas = tk.Canvas(conn_frame, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(conn_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_conn_frame = ttk.Frame(self.canvas)

        # 绑定滚动区域大小变化
        self.scrollable_conn_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        # 将 scrollable_conn_frame 放入 canvas
        self.canvas.create_window((0, 0), window=self.scrollable_conn_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.conn_container = self.scrollable_conn_frame

        # 绑定鼠标滚轮（Windows）
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # ====== 日志区域 ======
        log_frame = tk.Frame(root)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5, padx=10)
        tk.Label(log_frame, text="信息：").pack(anchor=tk.W)
        self.log = scrolledtext.ScrolledText(log_frame, width=50, height=8, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)

        # ====== 启动提示 ======
        self.write_log("使用远程串口功能\nSecureCRT、PuTTY等终端需设置为RAW模式")
        self.write_log("\nPuTTY还需在Terminal中的Local echo和Local line editing设为Force Off")
        self.write_log("")


    def _on_mousewheel(self, event):
        """处理鼠标滚轮事件（仅 Windows）"""
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def setup_serial_page(self, parent):
        """初始化串口映射页面"""
        serial_group = tk.LabelFrame(parent, text="串口配置", padx=10, pady=10)
        serial_group.pack(fill=tk.X, padx=10, pady=5)

        # 串口选择
        tk.Label(serial_group, text="串口:").grid(row=0, column=0, sticky=tk.W)
        self.com_var = tk.StringVar()
        self.com_combo = ttk.Combobox(serial_group, textvariable=self.com_var, width=20, state="readonly")
        self.com_combo.grid(row=0, column=1, padx=5, sticky=tk.W)
        self.refresh_com_ports()  # 初始刷新串口列表
        tk.Button(serial_group, text="刷新串口", command=self.refresh_com_ports).grid(row=0, column=2, padx=5)

        # 波特率选择
        tk.Label(serial_group, text="波特率:").grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
        self.baud_var = tk.StringVar(value="9600")
        self.baud_combo = ttk.Combobox(serial_group, textvariable=self.baud_var, width=20, state="readonly")
        self.baud_combo['values'] = [9600, 19200, 38400, 57600, 115200, 230400]
        self.baud_combo.grid(row=1, column=1, padx=5, pady=(10, 0), sticky=tk.W)

        # 外网端口输入
        remote_group = tk.LabelFrame(parent, text="外网端口配置 (1-65535)", padx=10, pady=10)
        remote_group.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(remote_group, text="外网端口:").grid(row=0, column=0, sticky=tk.W)
        self.serial_remote_entry = tk.Entry(remote_group, width=20)
        self.serial_remote_entry.grid(row=0, column=1, padx=5, sticky=tk.W)
        self.serial_remote_entry.insert(0, "3000")  # 默认值

        # 添加按钮
        tk.Button(parent, text="添加并启动串口映射", command=self.add_serial_connection, bg="#4CAF50", fg="white").pack(
            pady=10, padx=10, fill=tk.X)

    def setup_tcp_page(self, parent):
        """初始化 TCP 映射页面"""
        ip_group = tk.LabelFrame(parent, text="内网IP:端口配置", padx=10, pady=10)
        ip_group.pack(fill=tk.X, padx=10, pady=5)

        # IP 输入
        tk.Label(ip_group, text="IP地址:").grid(row=0, column=0, sticky=tk.W)
        self.ip_var = tk.StringVar()
        self.ip_entry = tk.Entry(ip_group, textvariable=self.ip_var, width=20)
        self.ip_entry.grid(row=0, column=1, padx=5, sticky=tk.W)
        # 绑定事件：输入时自动计算外网端口
        self.ip_entry.bind('<KeyRelease>', self.update_tcp_remote_port)
        self.ip_entry.bind('<FocusOut>', self.update_tcp_remote_port)

        # 端口输入
        tk.Label(ip_group, text="端口:").grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
        self.port_var = tk.StringVar()
        self.port_entry = tk.Entry(ip_group, textvariable=self.port_var, width=20)
        self.port_entry.grid(row=1, column=1, padx=5, pady=(10, 0), sticky=tk.W)
        self.port_entry.insert(0, "22")  # 默认 SSH 端口
        self.port_entry.bind('<KeyRelease>', self.update_tcp_remote_port)
        self.port_entry.bind('<FocusOut>', self.update_tcp_remote_port)

        # 常用端口提示
        tip_label = tk.Label(
            ip_group,
            text="SSH 22, Telnet 23, RDP 3389, Winbox 8291",
            fg="gray",
            font=("TkDefaultFont", 8)
        )
        tip_label.grid(row=2, column=1, sticky=tk.W, pady=(2, 0))

        # 外网端口输入（可手动修改）
        remote_group = tk.LabelFrame(parent, text="外网端口配置 (1-65535)", padx=10, pady=10)
        remote_group.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(remote_group, text="外网端口:").grid(row=0, column=0, sticky=tk.W)
        self.tcp_remote_entry = tk.Entry(remote_group, width=20)
        self.tcp_remote_entry.grid(row=0, column=1, padx=5, sticky=tk.W)

        # 添加按钮
        tk.Button(parent, text="添加并启动TCP映射", command=self.add_tcp_connection, bg="#2196F3", fg="white").pack(
            pady=10, padx=10, fill=tk.X)

    def update_tcp_remote_port(self, event=None):
        """
        根据 IP 最后一段 + 本地端口 自动生成外网端口建议值。
        规则：IP最后段 + 本地端口 → 若 >65535 则截断至5位或4位。
        """
        ip_address = self.ip_var.get().strip()
        local_port_str = self.port_var.get().strip()
        self.tcp_remote_entry.delete(0, tk.END)
        if not ip_address or not local_port_str:
            return
        parts = ip_address.split('.')
        if len(parts) != 4:
            return
        try:
            local_port_int = int(local_port_str)
            if not (1 <= local_port_int <= 65535):
                return
            last_octet_str = parts[-1]
            combined_port_str = last_octet_str + local_port_str
            calculated_port = int(combined_port_str)
            if calculated_port > 65535:
                combined_len = len(combined_port_str)
                if combined_len > 5:
                    truncated_5_str = combined_port_str[:5]
                    truncated_5_int = int(truncated_5_str)
                    if 1 <= truncated_5_int <= 65535:
                        calculated_port = truncated_5_int
                    else:
                        truncated_4_str = combined_port_str[:4]
                        truncated_4_int = int(truncated_4_str) if truncated_4_str else 0
                        if 1 <= truncated_4_int <= 65535:
                            calculated_port = truncated_4_int
                        else:
                            return
                elif combined_len == 5:
                    truncated_4_str = combined_port_str[:4]
                    truncated_4_int = int(truncated_4_str) if truncated_4_str else 0
                    if 1 <= truncated_4_int <= 65535:
                        calculated_port = truncated_4_int
                    else:
                        return
            if 1 <= calculated_port <= 65535:
                self.tcp_remote_entry.insert(0, str(calculated_port))
        except ValueError:
            pass

    def write_log(self, text):
        """线程安全的日志写入（使用 root.after 切回主线程）"""
        self.root.after(0, lambda: self._do_write_log(text))

    def _do_write_log(self, text):
        """实际写入日志（必须在主线程）"""
        if not self.root.winfo_exists():
            return
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)  # 自动滚动到底部

    def refresh_com_ports(self):
        """刷新可用串口列表"""
        ports = serial.tools.list_ports.comports()
        com_list = [p.device for p in ports]
        self.com_combo['values'] = com_list
        if com_list:
            self.com_combo.current(0)  # 选中第一个

    def add_serial_connection(self):
        """处理“添加串口映射”按钮点击"""
        local_port = self.com_var.get().strip()
        remote = self.serial_remote_entry.get().strip()
        if not local_port:
            messagebox.showerror("错误", "请选择串口")
            return
        if not remote.isdigit():
            messagebox.showerror("错误", "外网端口必须是数字")
            return
        remote_port = int(remote)
        if not (1 <= remote_port <= 65535):
            messagebox.showwarning("警告", f"外网端口 {remote_port} 不在合规范围内 (1-65535)。")
            self.write_log(f"[警告] 尝试添加串口映射失败：外网端口 {remote_port} 不合规。")
            return
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            messagebox.showerror("错误", "波特率必须是数字")
            return

        # 为串口桥分配本地端口（避免冲突）
        local_port_num = 20000 + (remote_port % 1000)
        bridge = SerialBridge(local_port, baud, local_port_num, self.write_log)
        if not bridge.start():
            return

        # 生成 FRP 配置
        cfg = configparser.ConfigParser()
        cfg["common"] = {
            "server_addr": SERVER_ADDR,
            "server_port": SERVER_PORT,
            "authentication_method": "token",
            "token": SERVER_TOKEN
        }
        cfg[f"proxy_{remote_port}"] = {
            "type": "tcp",
            "local_ip": "127.0.0.1",
            "local_port": local_port_num,
            "remote_port": remote_port,
        }

        # 创建临时配置文件
        with tempfile.NamedTemporaryFile(prefix=f"frpc_{remote_port}_", suffix=".ini", delete=False, mode='w',
                                         encoding='utf-8') as tmpf:
            cfg.write(tmpf)
            ini_path = tmpf.name

        # 创建连接对象
        conn = FRPConnection("127.0.0.1", local_port_num, remote_port, ini_path, "serial", local_port)
        conn.baudrate = baud
        conn.bridge = bridge
        self.connections.append(conn)
        self.add_connection_ui(conn)
        self.start_connection(conn)

    def add_tcp_connection(self):
        """处理“添加TCP映射”按钮点击"""
        ip = self.ip_var.get().strip()
        port = self.port_var.get().strip()
        remote = self.tcp_remote_entry.get().strip()
        if not ip:
            messagebox.showerror("错误", "请输入IP地址")
            return
        if not port or not port.isdigit():
            messagebox.showerror("错误", "请输入有效的端口号")
            return
        if not remote or not remote.isdigit():
            messagebox.showerror("错误", "外网端口必须是数字")
            return

        port = int(port)
        remote_port = int(remote)
        if not (1 <= remote_port <= 65535):
            messagebox.showwarning("警告", f"外网端口 {remote_port} 不在合规范围内 (1-65535)。")
            self.write_log(f"[警告] 尝试添加TCP映射失败：外网端口 {remote_port} 不合规。")
            return

        # 生成 FRP 配置
        cfg = configparser.ConfigParser()
        cfg["common"] = {
            "server_addr": SERVER_ADDR,
            "server_port": SERVER_PORT,
            "authentication_method": "token",
            "token": SERVER_TOKEN
        }
        cfg[f"proxy_{remote_port}"] = {
            "type": "tcp",
            "local_ip": ip,
            "local_port": port,
            "remote_port": remote_port,
        }

        # 创建临时配置文件
        with tempfile.NamedTemporaryFile(prefix=f"frpc_{remote_port}_", suffix=".ini", delete=False, mode='w',
                                         encoding='utf-8') as tmpf:
            cfg.write(tmpf)
            ini_path = tmpf.name

        # 创建连接对象
        conn = FRPConnection(ip, port, remote_port, ini_path, "tcp")
        self.connections.append(conn)
        self.add_connection_ui(conn)
        self.start_connection(conn)

    def bind_status_label(self, label, remote_port):
        """
        为“[运行中]”标签绑定交互行为：
          - 悬停：下划线
          - 点击：复制公网地址到剪贴板，并显示“[已复制!]”
        """
        # 获取当前字体
        current_font = label.cget("font")
        import tkinter.font as tkFont
        if isinstance(current_font, str):
            font_obj = tkFont.Font(font=current_font)
        else:
            font_obj = tkFont.Font(font=current_font)

        normal_font = font_obj.copy()
        underline_font = font_obj.copy()
        underline_font.configure(underline=True)

        def on_enter(e):
            label.config(font=underline_font)

        def on_leave(e):
            label.config(font=normal_font)

        def on_click(e):
            # 决定公网地址的主机名
            host = "www.esun21.com" if SERVER_ADDR == DEFAULT_FRP_CONFIG['addr'] else SERVER_ADDR
            address = f"{host}:{remote_port}"
            self.root.clipboard_clear()
            self.root.clipboard_append(address)
            self.write_log(f"[INFO] 已复制地址: {address}")
            # 临时显示绿色“已复制!”
            label.config(text="[已复制!]", fg="green", font=normal_font)
            label.after(1000, lambda: label.config(text="[运行中]", fg="blue", font=normal_font))

        label.bind("<Enter>", on_enter)
        label.bind("<Leave>", on_leave)
        label.bind("<Button-1>", on_click)

    def add_connection_ui(self, conn):
        """在连接列表中添加一行 UI"""
        frame = tk.Frame(self.conn_container)
        frame.pack(fill=tk.X, pady=2, padx=5)

        # 构建描述文本
        if conn.conn_type == "serial":
            prefix_text = f"串口 {conn.serial_port} → 外网 {conn.remote_port} "
        else:
            prefix_text = f"TCP {conn.local_ip}:{conn.local_port} → 外网 {conn.remote_port} "

        prefix_lbl = tk.Label(frame, text=prefix_text, anchor="w")
        prefix_lbl.pack(side=tk.LEFT)

        status_text = f"[{conn.status}]"
        status_lbl = tk.Label(
            frame,
            text=status_text,
            anchor="w",
            fg="blue" if conn.status == "运行中" else "gray",
            cursor="hand2" if conn.status == "运行中" else ""
        )
        status_lbl.pack(side=tk.LEFT)

        # 绑定交互（仅运行中）
        if conn.status == "运行中":
            self.bind_status_label(status_lbl, conn.remote_port)

        # 按钮区域
        button_frame = tk.Frame(frame)
        button_frame.pack(side=tk.RIGHT, fill=tk.Y)

        btn_toggle = tk.Button(button_frame, text="停止" if conn.status == "运行中" else "启动", width=5,
                               command=lambda c=conn: self.toggle_connection(c))
        btn_remove = tk.Button(button_frame, text="删除", width=5,
                               command=lambda c=conn: self.remove_connection(c))

        conn.buttons = {'toggle': btn_toggle, 'remove': btn_remove}
        btn_remove.pack(side=tk.RIGHT, padx=1)
        btn_toggle.pack(side=tk.RIGHT, padx=1)

        # 保存 UI 引用
        conn.frame = (frame, prefix_lbl, status_lbl)

    def refresh_status(self, conn):
        """刷新连接的 UI 状态（线程安全）"""
        if not hasattr(conn, 'frame') or not conn.frame:
            return

        frame, prefix_lbl, status_lbl = conn.frame
        if not (frame.winfo_exists() and prefix_lbl.winfo_exists() and status_lbl.winfo_exists()):
            return

        status_text = f"[{conn.status}]"
        if conn.status == "运行中":
            status_lbl.config(text=status_text, fg="blue", cursor="hand2")
            # 重新绑定事件（防止多次绑定）
            status_lbl.unbind("<Enter>")
            status_lbl.unbind("<Leave>")
            status_lbl.unbind("<Button-1>")
            self.bind_status_label(status_lbl, conn.remote_port)
        else:
            status_lbl.config(text=status_text, fg="gray", cursor="")
            status_lbl.unbind("<Enter>")
            status_lbl.unbind("<Leave>")
            status_lbl.unbind("<Button-1>")

        # 更新按钮文本
        btn_toggle = conn.buttons.get('toggle')
        if btn_toggle and btn_toggle.winfo_exists():
            btn_toggle.config(text="停止" if conn.status == "运行中" else "启动")

    def toggle_connection(self, conn):
        """切换连接状态（启动/停止）"""
        if conn.status == "运行中":
            self.stop_connection(conn)
        else:
            self.start_connection(conn)

    def start_connection(self, conn):
        """启动 FRP 连接"""
        if conn.status == "运行中":
            self.write_log(f"[提示] {conn.remote_port} 已运行")
            return
        if not os.path.exists(FRPC_EXEC):
            messagebox.showerror("错误", f"找不到 {FRPC_EXEC}")
            return

        # 串口连接需要重建桥接
        if conn.conn_type == "serial":
            if not hasattr(conn, 'baudrate') or conn.baudrate is None:
                messagebox.showerror("错误", "串口连接缺少波特率信息")
                return

            # 停止旧的桥接（如果存在）
            if getattr(conn, 'bridge', None) and conn.bridge.running:
                self.write_log(f"[INFO] 停止旧的串口桥...")
                conn.bridge.stop()

            local_port_num = 20000 + (conn.remote_port % 1000)
            bridge = SerialBridge(conn.serial_port, conn.baudrate, local_port_num, self.write_log)
            if not bridge.start():
                return
            conn.bridge = bridge
            conn.local_port = local_port_num

        # 启动 frpc 子进程
        try:
            self.write_log(f"[启动] {conn.remote_port}")
            startupinfo = None
            if os.name == 'nt':
                # Windows 下隐藏控制台窗口
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
            with conn.process_lock:
                conn.process = subprocess.Popen(
                    [FRPC_EXEC, "-c", conn.ini_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    startupinfo=startupinfo
                )
            # 启动日志读取线程
            threading.Thread(target=self.read_output, args=(conn,), daemon=True).start()
            conn.status = "运行中"
            self.refresh_status(conn)
        except FileNotFoundError:
            messagebox.showerror("错误", f"找不到 {FRPC_EXEC}，请确认路径是否正确。")
            self.write_log(f"[错误] 启动 {conn.remote_port} 失败：找不到 {FRPC_EXEC}")
        except Exception as e:
            messagebox.showerror("错误", f"启动连接 {conn.remote_port} 时发生未知错误：{str(e)}")
            self.write_log(f"[错误] 启动 {conn.remote_port} 失败：{str(e)}")

    def stop_connection(self, conn):
        """停止连接（异步）"""
        if conn.status == "停止":
            self.write_log(f"[提示] {conn.remote_port} 已停止")
            return
        threading.Thread(target=self._do_stop_connection, args=(conn,), daemon=True).start()

    def _do_stop_connection(self, conn):
        """实际停止连接的逻辑（在子线程中执行）"""
        # 停止串口桥（如果存在）
        if getattr(conn, 'bridge', None):
            conn.bridge.stop()
            conn.bridge = None

        # 终止 frpc 进程
        with conn.process_lock:
            process = conn.process
            if process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.write_log(f"[WARN] {conn.remote_port} 未响应 terminate，强制 kill")
                    process.kill()
                    process.wait()
                conn.process = None

        conn.status = "停止"
        # 切回主线程刷新 UI
        self.root.after(0, lambda: self.refresh_status(conn))

    def remove_connection(self, conn):
        """移除连接（先停止再删除 UI）"""
        self.stop_connection(conn)
        if conn in self.connections:
            self.connections.remove(conn)
        if hasattr(conn, 'frame') and conn.frame:
            frame, _, _ = conn.frame
            if frame.winfo_exists():
                frame.destroy()
            # 更新滚动区域
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def read_output(self, conn):
        """读取 frpc 子进程的输出并记录日志"""
        with conn.process_lock:
            process = conn.process
            if process is None:
                return
        try:
            for line in iter(process.stdout.readline, ''):
                if not line:
                    continue
                stripped_line = line.rstrip()
                self.write_log(f"[{conn.remote_port}] {stripped_line}")
                # 检测端口冲突
                if "start error: proxy [" in stripped_line and "] already exists" in stripped_line:
                    self.root.after(0, lambda c=conn: self.handle_proxy_already_exists(c))
            process.wait()
        except Exception as e:
            self.write_log(f"[ERROR] 读取 {conn.remote_port} 输出异常: {e}")
        # 进程结束后更新状态
        with conn.process_lock:
            if conn.process is not None and conn.process.poll() is not None:
                conn.status = "已停止"
                self.root.after(0, lambda: self.refresh_status(conn))

    def on_close(self):
        """主窗口关闭事件处理"""
        if self.closing:
            return
        self.closing = True
        self.close_start_time = time.time()
        self.write_log("[INFO] 正在关闭所有连接，请稍候...")

        # 停止所有连接
        for conn in self.connections[:]:
            self.stop_connection(conn)

        # 开始检查是否全部停止
        self.check_all_stopped()

    def check_all_stopped(self):
        """检查所有连接是否已停止，超时则强制退出"""
        if time.time() - self.close_start_time > 10:
            self.write_log("[WARN] 关闭超时，强制退出")
            self.root.destroy()
            return

        all_stopped = True
        for conn in self.connections:
            if conn.status != "停止":
                all_stopped = False
                break

        if all_stopped:
            # 清理临时配置文件
            for conn in self.connections[:]:
                try:
                    if os.path.exists(conn.ini_path):
                        os.remove(conn.ini_path)
                        self.write_log(f"[INFO] 已删除临时配置文件: {conn.ini_path}")
                except Exception as e:
                    self.write_log(f"[WARN] 删除临时配置文件失败 {conn.ini_path}: {e}")
            self.root.destroy()
        else:
            # 200ms 后再次检查
            self.root.after(200, self.check_all_stopped)

    def handle_proxy_already_exists(self, conn):
        """处理 FRP 端口冲突"""
        messagebox.showwarning("端口冲突", f"外网端口 {conn.remote_port} 已被占用，该映射将被自动移除。")
        self.remove_connection(conn)

    def show_about(self):
        """显示关于对话框"""
        notes = "\n".join(UPDATE_NOTES)
        messagebox.showinfo(
            f"版本 {APP_VERSION}",
            f"作者：Frank.Ni\n"
            f"官网：www.esun21.com\n\n"
            f"【最近更新】\n{notes}"
        )


# ---------------- 主程序入口 ----------------
if __name__ == "__main__":
    root = tk.Tk()

    # === ⭐ 关键：先隐藏窗口，避免闪现左上角 ===
    root.withdraw()

    # 设置窗口图标
    root.iconbitmap(resource_path("logo256.ico"))

    # 居中窗口
    window_width = 400
    window_height = 600
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = (screen_width // 2) - (window_width // 2)
    y = (screen_height // 2) - (window_height // 2)
    root.geometry(f"{window_width}x{window_height}+{x}+{y}")

    # 创建主界面
    app = FRPGUI(root)

    # 关闭 PyInstaller Splash 屏幕（如果存在）
    try:
        import pyi_splash
        pyi_splash.close()
    except ImportError:
        pass

    # ⭐ 窗口置顶确保前台显示
    def bring_to_front():
        root.lift()
        root.focus_force()
        root.attributes('-topmost', True)
        root.after(150, lambda: root.attributes('-topmost', False))

    root.deiconify()  # 👈 此时才真正显示窗口
    root.after(100, bring_to_front)

    root.mainloop()
