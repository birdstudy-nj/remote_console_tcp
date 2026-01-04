# -*- coding: utf-8 -*-
"""
Remote Serial & TCP Slave-side GUI Tool (v1.2)
Features:
  - Expose local serial port to public network via FRP (supports RAW mode terminal)
  - Expose local TCP services (e.g., SSH, RDP) to public network via FRP
  - Supports loading FRP server config from external config.ini
  - Built-in serial-to-TCP bridge (no extra tools needed)

Author: Frank.Ni
Website: www.esun21.com
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


# --- Resource path handling ---
# Used for correctly accessing resource files (e.g., frpc.exe, logo256.ico) after PyInstaller packaging
def resource_path(relative_path):
    """
    Get absolute path to resource.
    In PyInstaller bundled app, resources are unpacked to _MEIPASS;
    during development, use current working directory.
    """
    try:
        # If running as PyInstaller bundle, sys._MEIPASS points to temp resource dir
        base_path = sys._MEIPASS
    except Exception:
        # Otherwise use script's directory
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# --- Application metadata ---
APP_VERSION = "v1.2"
UPDATE_NOTES = [
    "• Added: About dialog",
    "• Added: External config.ini support",
    "• Improved: UI details and messages",
]


# FRPC_EXEC points to the bundled frpc.exe (internal resource)
FRPC_EXEC = resource_path('frpc.exe')

# --- Default FRP server config (built-in, not exposed in file) ---
# Used when config.ini is missing or fails to load
DEFAULT_FRP_CONFIG = {
    "addr": "your_frp_server_ip_address",   # FRP server address
    "port": 7000,                          # FRP server port
    "token": "your_frp_server_secure_token"  # Authentication token
}


# --- Securely load config.ini: read-only, never create ---
def load_frp_config():
    """
    Load FRP server configuration from config.ini in the application's directory.
    Priority: external config.ini > built-in defaults.
    Note: This function only reads existing files; it never creates config.ini.
    Returns: (addr, port, token)
    """
    # 1. Get the root directory where the executable resides
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        base_dir = os.path.dirname(sys.executable)
    else:
        # Running from source
        base_dir = os.path.dirname(os.path.abspath(__file__))

    # 2. Define external config path (same level as .exe)
    external_config = os.path.join(base_dir, "config.ini")

    # 3. Priority: external first, then default
    if os.path.exists(external_config):
        config_path = external_config
    else:
        # No config found → return defaults
        return DEFAULT_FRP_CONFIG["addr"], DEFAULT_FRP_CONFIG["port"], DEFAULT_FRP_CONFIG["token"]

    # 4. Attempt to read config
    cfg = configparser.ConfigParser()
    try:
        # Use UTF-8 encoding to avoid issues from manual edits
        cfg.read(config_path, encoding='utf-8')
        frps_addr = cfg.get("frp_server", "frps_addr", fallback=DEFAULT_FRP_CONFIG["addr"])
        frps_port = cfg.getint("frp_server", "frps_port", fallback=DEFAULT_FRP_CONFIG["port"])
        frps_token = cfg.get("frp_server", "frps_token", fallback=DEFAULT_FRP_CONFIG["token"])
        return frps_addr, frps_port, frps_token
    except Exception:
        # On parse error, fall back to defaults
        return DEFAULT_FRP_CONFIG["addr"], DEFAULT_FRP_CONFIG["port"], DEFAULT_FRP_CONFIG["token"]


# Global: Load FRP server config once at startup
SERVER_ADDR, SERVER_PORT, SERVER_TOKEN = load_frp_config()


# ---------------- Serial-to-TCP Bridge ----------------
class SerialBridge:
    """
    Implements bidirectional bridge between serial port and TCP.
    Uses socketserver to create a TCP server that forwards data between serial and TCP clients.
    """

    def __init__(self, serial_port, baudrate, local_port, log_func):
        self.serial_port = serial_port  # e.g., "COM3"
        self.baudrate = baudrate        # Baud rate
        self.local_port = local_port    # Local TCP port to listen on (for FRP)
        self.log = log_func             # Logging callback
        self.ser = None                 # serial.Serial object
        self.server = None              # socketserver.TCPServer object
        self.thread = None              # Server thread
        self.running = False            # Running flag

    def start(self):
        """Start the serial bridge"""
        try:
            # Open serial port
            self.ser = serial.Serial(
                self.serial_port,
                self.baudrate,
                timeout=0.1,            # Read timeout
                write_timeout=1.0,      # Write timeout
                inter_byte_timeout=0.05 # Inter-byte timeout
            )
        except Exception as e:
            self.log(f"[ERROR] Failed to open serial port {self.serial_port}: {e}")
            return False

        # Dynamically create TCP request handler (closure captures ser and log)
        handler = self.make_handler()
        # Create multi-threaded TCP server (listen on all interfaces)
        self.server = socketserver.ThreadingTCPServer(('0.0.0.0', self.local_port), handler)
        self.server.allow_reuse_address = True  # Allow port reuse

        # Start server thread
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True
        self.log(f"[INFO] Serial bridge started: {self.serial_port}@{self.baudrate} → TCP {self.local_port}")
        return True

    def stop(self):
        """Stop the serial bridge"""
        if not self.running:
            return

        self.running = False
        self.log(f"[INFO] Stopping serial bridge: {self.serial_port}")

        # Safely close serial port
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception as e:
            self.log(f"[WARN] Error closing serial port: {e}")

        # Shut down TCP server
        try:
            if self.server:
                self.server.shutdown()      # Stop serve_forever
                self.server.server_close()  # Close socket
        except Exception as e:
            self.log(f"[WARN] Error during server shutdown: {e}")

        # Wait for thread to finish (max 2 seconds)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

        self.log(f"[INFO] Serial bridge stopped: {self.serial_port}")

    def make_handler(self):
        """
        Return a dynamically generated TCP request handler class.
        Uses closure to capture current instance's ser and log.
        """
        ser = self.ser
        log = self.log

        class Handler(socketserver.BaseRequestHandler):
            """Handle each TCP client connection"""

            def handle(self):
                self.request.settimeout(0.5)  # Set socket timeout
                client_addr = self.client_address
                log(f"[INFO] New client connected: {client_addr}")

                stop_event = threading.Event()  # Coordinate thread exit

                def serial_to_tcp():
                    """Forward data from serial → TCP"""
                    try:
                        while not stop_event.is_set():
                            data = ser.read(1024)  # Read from serial
                            if data:
                                try:
                                    self.request.sendall(data)  # Send to TCP client
                                    # Try to decode for logging (ignore undecodable bytes)
                                    decoded = data.decode('utf-8', errors='replace')
                                    for line in decoded.splitlines(keepends=True):
                                        log(f"[SERIAL → TCP] {line.rstrip()}")
                                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                                    log(f"[WARN] Client {client_addr} disconnected (serial→TCP): {e}")
                                    break
                    except Exception as e:
                        log(f"[ERROR] Serial read exception: {e}")
                    finally:
                        stop_event.set()

                def tcp_to_serial():
                    """Forward data from TCP → serial"""
                    try:
                        while not stop_event.is_set():
                            try:
                                data = self.request.recv(1024)  # Receive from TCP client
                                if not data:
                                    log(f"[INFO] Client {client_addr} closed connection normally")
                                    break
                                try:
                                    ser.write(data)  # Write to serial
                                    decoded = data.decode('utf-8', errors='replace')
                                    for line in decoded.splitlines(keepends=True):
                                        log(f"[TCP → SERIAL] {line.rstrip()}")
                                except serial.SerialException as e:
                                    log(f"[ERROR] Serial write failed: {e}")
                                    break
                            except socket.timeout:
                                continue  # Timeout → continue loop
                            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                                log(f"[WARN] Client {client_addr} disconnected: {e}")
                                break
                    except Exception as e:
                        log(f"[ERROR] TCP receive exception: {e}")
                    finally:
                        stop_event.set()

                # Start both direction threads
                t1 = threading.Thread(target=serial_to_tcp, daemon=True)
                t2 = threading.Thread(target=tcp_to_serial, daemon=True)
                t1.start()
                t2.start()

                # Wait until either thread ends
                try:
                    while t1.is_alive() or t2.is_alive():
                        t1.join(timeout=0.1)
                        t2.join(timeout=0.1)
                except KeyboardInterrupt:
                    pass

                stop_event.set()
                log(f"[INFO] Session ended for client {client_addr}")

        return Handler


# ---------------- FRP Connection Manager ----------------
class FRPConnection:
    """
    Represents an FRP mapping connection (serial or TCP).
    Holds config, subprocess, UI references, etc.
    """

    def __init__(self, local_ip, local_port, remote_port, ini_path, conn_type="tcp", serial_port=None):
        self.local_ip = local_ip          # Local IP (127.0.0.1 for serial)
        self.local_port = local_port      # Local port (bridge port for serial)
        self.remote_port = remote_port    # Public mapped port
        self.ini_path = ini_path          # Temporary FRP config file path
        self.conn_type = conn_type        # "serial" or "tcp"
        self.serial_port = serial_port    # Only valid for serial
        self.process = None               # subprocess.Popen object
        self.status = "Stopped"           # "Stopped" / "Running"
        self.frame = None                 # UI Frame reference (frame, prefix_lbl, status_lbl)
        self.buttons = {}                 # UI button references
        self.bridge = None                # SerialBridge instance (serial only)
        self.process_lock = threading.Lock()  # Protect process access
        self.is_temp_ini = True           # Mark if ini is temporary (for cleanup)
        self.baudrate = None              # Baud rate (serial only)


# ---------------- Main GUI Window ----------------
class FRPGUI:
    """Main GUI window class"""

    def __init__(self, root):
        self.root = root
        self.root.title("Remote Serial & TCP Slave")
        self.root.geometry("400x600")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)  # Bind close event

        self.connections = []   # Store all FRPConnection instances
        self.closing = False    # Closing flag
        self.close_start_time = None  # Time when closing started (for timeout)

        # ====== Top: Notebook + "About" Button ======
        top_bar = tk.Frame(root)
        top_bar.pack(fill=tk.X, padx=10, pady=(10, 0))

        # Create Notebook (tabs)
        notebook = ttk.Notebook(top_bar)
        notebook.pack(fill=tk.X)

        # "About" button: fixed at top-right using place
        about_btn = tk.Button(
            top_bar,
            text="About",
            command=self.show_about,
            relief=tk.FLAT,
            fg="gray",
            font=("TkDefaultFont", 9),
            cursor="hand2",
            bd=0,
            activeforeground="black",
            bg=root.cget("bg")  # Match window background
        )
        about_btn.place(relx=1.0, y=2, x=0, anchor="ne")  # Top-right alignment

        # Hover effect
        about_btn.bind("<Enter>", lambda e: about_btn.config(fg="black"))
        about_btn.bind("<Leave>", lambda e: about_btn.config(fg="gray"))

        # Create two tabs
        serial_frame = ttk.Frame(notebook)
        notebook.add(serial_frame, text="Serial Mapping")
        tcp_frame = ttk.Frame(notebook)
        notebook.add(tcp_frame, text="TCP Mapping")

        # Initialize tab contents
        self.setup_serial_page(serial_frame)
        self.setup_tcp_page(tcp_frame)

        # ====== Active Connections List (with scrollbar) ======
        conn_frame = tk.Frame(root, height=150)
        conn_frame.pack(fill=tk.X, pady=5, padx=10)
        conn_frame.pack_propagate(False)  # Fix height
        tk.Label(conn_frame, text="Active Connections:").pack(anchor=tk.W)

        # Use Canvas + Scrollbar for scrollable area
        self.canvas = tk.Canvas(conn_frame, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(conn_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_conn_frame = ttk.Frame(self.canvas)

        # Bind scroll region update
        self.scrollable_conn_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        # Embed scrollable frame into canvas
        self.canvas.create_window((0, 0), window=self.scrollable_conn_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.conn_container = self.scrollable_conn_frame

        # Bind mouse wheel (Windows)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # ====== Log Area ======
        log_frame = tk.Frame(root)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5, padx=10)
        tk.Label(log_frame, text="Log:").pack(anchor=tk.W)
        self.log = scrolledtext.ScrolledText(log_frame, width=50, height=8, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)

        # ====== Startup Tips ======
        self.write_log("For remote serial usage:")
        self.write_log("Set terminals like SecureCRT or PuTTY to RAW mode.")
        self.write_log("")
        self.write_log("In PuTTY, also set 'Local echo' and 'Local line editing'")
        self.write_log("to 'Force Off' under Terminal settings.")
        self.write_log("")


    def _on_mousewheel(self, event):
        """Handle mouse wheel scrolling (Windows only)"""
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def setup_serial_page(self, parent):
        """Initialize serial mapping tab"""
        serial_group = tk.LabelFrame(parent, text="Serial Settings", padx=10, pady=10)
        serial_group.pack(fill=tk.X, padx=10, pady=5)

        # Serial port selection
        tk.Label(serial_group, text="Port:").grid(row=0, column=0, sticky=tk.W)
        self.com_var = tk.StringVar()
        self.com_combo = ttk.Combobox(serial_group, textvariable=self.com_var, width=20, state="readonly")
        self.com_combo.grid(row=0, column=1, padx=5, sticky=tk.W)
        self.refresh_com_ports()  # Initial refresh
        tk.Button(serial_group, text="Refresh", command=self.refresh_com_ports).grid(row=0, column=2, padx=5)

        # Baud rate selection
        tk.Label(serial_group, text="Baud Rate:").grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
        self.baud_var = tk.StringVar(value="9600")
        self.baud_combo = ttk.Combobox(serial_group, textvariable=self.baud_var, width=20, state="readonly")
        self.baud_combo['values'] = [9600, 19200, 38400, 57600, 115200, 230400]
        self.baud_combo.grid(row=1, column=1, padx=5, pady=(10, 0), sticky=tk.W)

        # Public port input
        remote_group = tk.LabelFrame(parent, text="Public Port (1-65535)", padx=10, pady=10)
        remote_group.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(remote_group, text="Public Port:").grid(row=0, column=0, sticky=tk.W)
        self.serial_remote_entry = tk.Entry(remote_group, width=20)
        self.serial_remote_entry.grid(row=0, column=1, padx=5, sticky=tk.W)
        self.serial_remote_entry.insert(0, "3000")  # Default

        # Add button
        tk.Button(parent, text="Add & Start Serial Mapping", command=self.add_serial_connection, bg="#4CAF50", fg="white").pack(
            pady=10, padx=10, fill=tk.X)

    def setup_tcp_page(self, parent):
        """Initialize TCP mapping tab"""
        ip_group = tk.LabelFrame(parent, text="Internal IP:Port", padx=10, pady=10)
        ip_group.pack(fill=tk.X, padx=10, pady=5)

        # IP input
        tk.Label(ip_group, text="IP Address:").grid(row=0, column=0, sticky=tk.W)
        self.ip_var = tk.StringVar()
        self.ip_entry = tk.Entry(ip_group, textvariable=self.ip_var, width=20)
        self.ip_entry.grid(row=0, column=1, padx=5, sticky=tk.W)
        # Bind events to auto-calculate public port
        self.ip_entry.bind('<KeyRelease>', self.update_tcp_remote_port)
        self.ip_entry.bind('<FocusOut>', self.update_tcp_remote_port)

        # Port input
        tk.Label(ip_group, text="Port:").grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
        self.port_var = tk.StringVar()
        self.port_entry = tk.Entry(ip_group, textvariable=self.port_var, width=20)
        self.port_entry.grid(row=1, column=1, padx=5, pady=(10, 0), sticky=tk.W)
        self.port_entry.insert(0, "22")  # Default: SSH
        self.port_entry.bind('<KeyRelease>', self.update_tcp_remote_port)
        self.port_entry.bind('<FocusOut>', self.update_tcp_remote_port)

        # Common ports tip
        tip_label = tk.Label(
            ip_group,
            text="SSH 22, Telnet 23, RDP 3389, Winbox 8291",
            fg="gray",
            font=("TkDefaultFont", 8)
        )
        tip_label.grid(row=2, column=1, sticky=tk.W, pady=(2, 0))

        # Public port input (editable)
        remote_group = tk.LabelFrame(parent, text="Public Port (1-65535)", padx=10, pady=10)
        remote_group.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(remote_group, text="Public Port:").grid(row=0, column=0, sticky=tk.W)
        self.tcp_remote_entry = tk.Entry(remote_group, width=20)
        self.tcp_remote_entry.grid(row=0, column=1, padx=5, sticky=tk.W)

        # Add button
        tk.Button(parent, text="Add & Start TCP Mapping", command=self.add_tcp_connection, bg="#2196F3", fg="white").pack(
            pady=10, padx=10, fill=tk.X)

    def update_tcp_remote_port(self, event=None):
        """
        Auto-suggest public port based on last IP octet + local port.
        Rule: last_octet + local_port → if >65535, truncate to 5 or 4 digits.
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
        """Thread-safe log writing (use root.after to switch to main thread)"""
        self.root.after(0, lambda: self._do_write_log(text))

    def _do_write_log(self, text):
        """Actual log writing (must run in main thread)"""
        if not self.root.winfo_exists():
            return
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)  # Auto-scroll to bottom

    def refresh_com_ports(self):
        """Refresh available serial ports"""
        ports = serial.tools.list_ports.comports()
        com_list = [p.device for p in ports]
        self.com_combo['values'] = com_list
        if com_list:
            self.com_combo.current(0)  # Select first item

    def add_serial_connection(self):
        """Handle 'Add Serial Mapping' button click"""
        local_port = self.com_var.get().strip()
        remote = self.serial_remote_entry.get().strip()
        if not local_port:
            messagebox.showerror("Error", "Please select a serial port")
            return
        if not remote.isdigit():
            messagebox.showerror("Error", "Public port must be numeric")
            return
        remote_port = int(remote)
        if not (1 <= remote_port <= 65535):
            messagebox.showwarning("Warning", f"Public port {remote_port} is out of range (1-65535).")
            self.write_log(f"[Warning] Failed to add serial mapping: public port {remote_port} invalid.")
            return
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            messagebox.showerror("Error", "Baud rate must be numeric")
            return

        # Assign local port for serial bridge (avoid conflicts)
        local_port_num = 20000 + (remote_port % 1000)
        bridge = SerialBridge(local_port, baud, local_port_num, self.write_log)
        if not bridge.start():
            return

        # Generate FRP config
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

        # Create temporary config file
        with tempfile.NamedTemporaryFile(prefix=f"frpc_{remote_port}_", suffix=".ini", delete=False, mode='w',
                                         encoding='utf-8') as tmpf:
            cfg.write(tmpf)
            ini_path = tmpf.name

        # Create connection object
        conn = FRPConnection("127.0.0.1", local_port_num, remote_port, ini_path, "serial", local_port)
        conn.baudrate = baud
        conn.bridge = bridge
        self.connections.append(conn)
        self.add_connection_ui(conn)
        self.start_connection(conn)

    def add_tcp_connection(self):
        """Handle 'Add TCP Mapping' button click"""
        ip = self.ip_var.get().strip()
        port = self.port_var.get().strip()
        remote = self.tcp_remote_entry.get().strip()
        if not ip:
            messagebox.showerror("Error", "Please enter IP address")
            return
        if not port or not port.isdigit():
            messagebox.showerror("Error", "Please enter a valid port number")
            return
        if not remote or not remote.isdigit():
            messagebox.showerror("Error", "Public port must be numeric")
            return

        port = int(port)
        remote_port = int(remote)
        if not (1 <= remote_port <= 65535):
            messagebox.showwarning("Warning", f"Public port {remote_port} is out of range (1-65535).")
            self.write_log(f"[Warning] Failed to add TCP mapping: public port {remote_port} invalid.")
            return

        # Generate FRP config
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

        # Create temporary config file
        with tempfile.NamedTemporaryFile(prefix=f"frpc_{remote_port}_", suffix=".ini", delete=False, mode='w',
                                         encoding='utf-8') as tmpf:
            cfg.write(tmpf)
            ini_path = tmpf.name

        # Create connection object
        conn = FRPConnection(ip, port, remote_port, ini_path, "tcp")
        self.connections.append(conn)
        self.add_connection_ui(conn)
        self.start_connection(conn)

    def bind_status_label(self, label, remote_port):
        """
        Bind interactive behavior to "[Running]" label:
          - Hover: underline
          - Click: copy public address to clipboard, show "[Copied!]"
        """
        # Get current font
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
            # Determine public host
            host = "www.esun21.com" if SERVER_ADDR == DEFAULT_FRP_CONFIG['addr'] else SERVER_ADDR
            address = f"{host}:{remote_port}"
            self.root.clipboard_clear()
            self.root.clipboard_append(address)
            self.write_log(f"[INFO] Copied address: {address}")
            # Temporarily show green "[Copied!]"
            label.config(text="[Copied!]", fg="green", font=normal_font)
            label.after(1000, lambda: label.config(text="[Running]", fg="blue", font=normal_font))

        label.bind("<Enter>", on_enter)
        label.bind("<Leave>", on_leave)
        label.bind("<Button-1>", on_click)

    def add_connection_ui(self, conn):
        """Add a UI row to the connections list"""
        frame = tk.Frame(self.conn_container)
        frame.pack(fill=tk.X, pady=2, padx=5)

        # Build description text
        if conn.conn_type == "serial":
            prefix_text = f"Serial {conn.serial_port} → Public {conn.remote_port} "
        else:
            prefix_text = f"TCP {conn.local_ip}:{conn.local_port} → Public {conn.remote_port} "

        prefix_lbl = tk.Label(frame, text=prefix_text, anchor="w")
        prefix_lbl.pack(side=tk.LEFT)

        status_text = f"[{conn.status}]"
        status_lbl = tk.Label(
            frame,
            text=status_text,
            anchor="w",
            fg="blue" if conn.status == "Running" else "gray",
            cursor="hand2" if conn.status == "Running" else ""
        )
        status_lbl.pack(side=tk.LEFT)

        # Bind interaction (only if running)
        if conn.status == "Running":
            self.bind_status_label(status_lbl, conn.remote_port)

        # Button area
        button_frame = tk.Frame(frame)
        button_frame.pack(side=tk.RIGHT, fill=tk.Y)

        btn_toggle = tk.Button(button_frame, text="Stop" if conn.status == "Running" else "Start", width=5,
                               command=lambda c=conn: self.toggle_connection(c))
        btn_remove = tk.Button(button_frame, text="Remove", width=7,
                               command=lambda c=conn: self.remove_connection(c))

        conn.buttons = {'toggle': btn_toggle, 'remove': btn_remove}
        btn_remove.pack(side=tk.RIGHT, padx=1)
        btn_toggle.pack(side=tk.RIGHT, padx=1)

        # Save UI references
        conn.frame = (frame, prefix_lbl, status_lbl)

    def refresh_status(self, conn):
        """Refresh connection UI status (thread-safe)"""
        if not hasattr(conn, 'frame') or not conn.frame:
            return

        frame, prefix_lbl, status_lbl = conn.frame
        if not (frame.winfo_exists() and prefix_lbl.winfo_exists() and status_lbl.winfo_exists()):
            return

        status_text = f"[{conn.status}]"
        if conn.status == "Running":
            status_lbl.config(text=status_text, fg="blue", cursor="hand2")
            # Rebind events (prevent multiple bindings)
            status_lbl.unbind("<Enter>")
            status_lbl.unbind("<Leave>")
            status_lbl.unbind("<Button-1>")
            self.bind_status_label(status_lbl, conn.remote_port)
        else:
            status_lbl.config(text=status_text, fg="gray", cursor="")
            status_lbl.unbind("<Enter>")
            status_lbl.unbind("<Leave>")
            status_lbl.unbind("<Button-1>")

        # Update button text
        btn_toggle = conn.buttons.get('toggle')
        if btn_toggle and btn_toggle.winfo_exists():
            btn_toggle.config(text="Stop" if conn.status == "Running" else "Start")

    def toggle_connection(self, conn):
        """Toggle connection state (start/stop)"""
        if conn.status == "Running":
            self.stop_connection(conn)
        else:
            self.start_connection(conn)

    def start_connection(self, conn):
        """Start FRP connection"""
        if conn.status == "Running":
            self.write_log(f"[Info] {conn.remote_port} already running")
            return
        if not os.path.exists(FRPC_EXEC):
            messagebox.showerror("Error", f"Cannot find {FRPC_EXEC}")
            return

        # For serial, rebuild bridge
        if conn.conn_type == "serial":
            if not hasattr(conn, 'baudrate') or conn.baudrate is None:
                messagebox.showerror("Error", "Missing baud rate for serial connection")
                return

            # Stop old bridge if exists
            if getattr(conn, 'bridge', None) and conn.bridge.running:
                self.write_log(f"[INFO] Stopping old serial bridge...")
                conn.bridge.stop()

            local_port_num = 20000 + (conn.remote_port % 1000)
            bridge = SerialBridge(conn.serial_port, conn.baudrate, local_port_num, self.write_log)
            if not bridge.start():
                return
            conn.bridge = bridge
            conn.local_port = local_port_num

        # Start frpc subprocess
        try:
            self.write_log(f"[Starting] {conn.remote_port}")
            startupinfo = None
            if os.name == 'nt':
                # Hide console window on Windows
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
            # Start log reading thread
            threading.Thread(target=self.read_output, args=(conn,), daemon=True).start()
            conn.status = "Running"
            self.refresh_status(conn)
        except FileNotFoundError:
            messagebox.showerror("Error", f"Cannot find {FRPC_EXEC}, please check path.")
            self.write_log(f"[Error] Failed to start {conn.remote_port}: {FRPC_EXEC} not found")
        except Exception as e:
            messagebox.showerror("Error", f"Unknown error starting {conn.remote_port}: {str(e)}")
            self.write_log(f"[Error] Failed to start {conn.remote_port}: {str(e)}")

    def stop_connection(self, conn):
        """Stop connection (asynchronously)"""
        if conn.status == "Stopped":
            self.write_log(f"[Info] {conn.remote_port} already stopped")
            return
        threading.Thread(target=self._do_stop_connection, args=(conn,), daemon=True).start()

    def _do_stop_connection(self, conn):
        """Actual stop logic (run in background thread)"""
        # Stop serial bridge if exists
        if getattr(conn, 'bridge', None):
            conn.bridge.stop()
            conn.bridge = None

        # Terminate frpc process
        with conn.process_lock:
            process = conn.process
            if process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.write_log(f"[WARN] {conn.remote_port} did not respond to terminate, forcing kill")
                    process.kill()
                    process.wait()
                conn.process = None

        conn.status = "Stopped"
        # Switch back to main thread to refresh UI
        self.root.after(0, lambda: self.refresh_status(conn))

    def remove_connection(self, conn):
        """Remove connection (stop first, then delete UI)"""
        self.stop_connection(conn)
        if conn in self.connections:
            self.connections.remove(conn)
        if hasattr(conn, 'frame') and conn.frame:
            frame, _, _ = conn.frame
            if frame.winfo_exists():
                frame.destroy()
            # Update scroll region
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def read_output(self, conn):
        """Read frpc subprocess output and log it"""
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
                # Detect port conflict
                if "start error: proxy [" in stripped_line and "] already exists" in stripped_line:
                    self.root.after(0, lambda c=conn: self.handle_proxy_already_exists(c))
            process.wait()
        except Exception as e:
            self.write_log(f"[ERROR] Exception reading output for {conn.remote_port}: {e}")
        # Update status after process ends
        with conn.process_lock:
            if conn.process is not None and conn.process.poll() is not None:
                conn.status = "Stopped"
                self.root.after(0, lambda: self.refresh_status(conn))

    def on_close(self):
        """Handle main window close event"""
        if self.closing:
            return
        self.closing = True
        self.close_start_time = time.time()
        self.write_log("[INFO] Closing all connections, please wait...")

        # Stop all connections
        for conn in self.connections[:]:
            self.stop_connection(conn)

        # Begin checking if all stopped
        self.check_all_stopped()

    def check_all_stopped(self):
        """Check if all connections are stopped; force exit on timeout"""
        if time.time() - self.close_start_time > 10:
            self.write_log("[WARN] Close timeout, forcing exit")
            self.root.destroy()
            return

        all_stopped = True
        for conn in self.connections:
            if conn.status != "Stopped":
                all_stopped = False
                break

        if all_stopped:
            # Clean up temporary config files
            for conn in self.connections[:]:
                try:
                    if os.path.exists(conn.ini_path):
                        os.remove(conn.ini_path)
                        self.write_log(f"[INFO] Deleted temp config: {conn.ini_path}")
                except Exception as e:
                    self.write_log(f"[WARN] Failed to delete temp config {conn.ini_path}: {e}")
            self.root.destroy()
        else:
            # Check again in 200ms
            self.root.after(200, self.check_all_stopped)

    def handle_proxy_already_exists(self, conn):
        """Handle FRP port conflict"""
        messagebox.showwarning("Port Conflict", f"Public port {conn.remote_port} is already in use. This mapping will be removed.")
        self.remove_connection(conn)

    def show_about(self):
        """Show About dialog"""
        notes = "\n".join(UPDATE_NOTES)
        messagebox.showinfo(
            f"Version {APP_VERSION}",
            f"Author: Frank.Ni\n"
            f"Website: www.esun21.com\n\n"
            f"[Recent Updates]\n{notes}"
        )


# ---------------- Main Entry Point ----------------
if __name__ == "__main__":
    root = tk.Tk()

    # === Key: Hide window first to avoid flash at top-left ===
    root.withdraw()

    # Set window icon
    root.iconbitmap(resource_path("logo256.ico"))

    # Center window
    window_width = 400
    window_height = 600
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = (screen_width // 2) - (window_width // 2)
    y = (screen_height // 2) - (window_height // 2)
    root.geometry(f"{window_width}x{window_height}+{x}+{y}")

    # Create main UI
    app = FRPGUI(root)

    # Close PyInstaller splash screen (if exists)
    try:
        import pyi_splash
        pyi_splash.close()
    except ImportError:
        pass

    # Bring window to front
    def bring_to_front():
        root.lift()
        root.focus_force()
        root.attributes('-topmost', True)
        root.after(150, lambda: root.attributes('-topmost', False))

    root.deiconify()  # Show window now
    root.after(100, bring_to_front)

    root.mainloop()
