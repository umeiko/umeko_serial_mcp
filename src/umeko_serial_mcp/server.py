from mcp.server.fastmcp import FastMCP
import serial
import serial.tools.list_ports
import threading
import asyncio
import websockets
import json
import pathlib
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

mcp = FastMCP("SerialPortMCP")

active_serial = None
ws_clients = set()
ws_loop = None
ws_server_running = False

# --- 新增：LLM 上下文缓冲与串口自动读取线程 ---
llm_read_buffer = []       # [(source, msg), ...]
buffer_lock = threading.Lock()
serial_read_thread = None
serial_read_running = False

# --- 新增：串口连接状态（供浏览器同步） ---
serial_status = {"connected": False, "port": None, "baudrate": 115200}


class DashboardHandler(BaseHTTPRequestHandler):
    # 强制使用 HTTP/1.0，禁用 keep-alive，避免单线程服务器被空闲连接阻塞
    protocol_version = 'HTTP/1.0'

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        html_path = pathlib.Path(__file__).with_name("dashboard.html")
        html = html_path.read_text(encoding="utf-8").replace("{WS_PORT}", str(self.server.ws_port))
        self.wfile.write(html.encode('utf-8'))
    def log_message(self, format, *args): pass

def run_http_server(http_port, ws_port):
    server = HTTPServer(('0.0.0.0', http_port), DashboardHandler)
    server.ws_port = ws_port
    server.serve_forever()

def broadcast_to_ui(source: str, msg: str):
    if ws_server_running and ws_clients and ws_loop:
        def _broadcast():
            websockets.broadcast(ws_clients, json.dumps({"source": source, "msg": msg}))
        ws_loop.call_soon_threadsafe(_broadcast)

def broadcast_status():
    """广播当前串口状态给所有 WebSocket 客户端。"""
    if ws_server_running and ws_clients and ws_loop:
        def _broadcast():
            websockets.broadcast(ws_clients, json.dumps({
                "type": "status",
                "connected": serial_status["connected"],
                "port": serial_status["port"],
                "baudrate": serial_status["baudrate"]
            }))
        ws_loop.call_soon_threadsafe(_broadcast)


def serial_read_loop():
    """后台线程：持续读取串口数据，广播到 UI 并累积到 LLM 缓冲区。"""
    global active_serial, serial_read_running, llm_read_buffer
    while serial_read_running:
        if active_serial and active_serial.is_open:
            try:
                # 非阻塞读取，不再依赖 in_waiting（某些平台不可靠）
                data = active_serial.read(4096)
                if data:
                    text_data = data.decode('utf-8', errors='ignore')
                    if text_data:
                        broadcast_to_ui("HARDWARE", text_data)
                        with buffer_lock:
                            llm_read_buffer.append(("HARDWARE", text_data))
                else:
                    time.sleep(0.05)
            except Exception as e:
                print(f"[serial_read_loop] 异常: {e}", file=sys.stderr)
                time.sleep(0.1)
        else:
            time.sleep(0.2)


def start_serial_reader():
    """启动串口自动读取线程。"""
    global serial_read_thread, serial_read_running
    stop_serial_reader()
    serial_read_running = True
    serial_read_thread = threading.Thread(target=serial_read_loop, daemon=True)
    serial_read_thread.start()


def stop_serial_reader():
    """停止串口自动读取线程。"""
    global serial_read_running, serial_read_thread
    serial_read_running = False
    if serial_read_thread:
        serial_read_thread.join(timeout=1.0)
        serial_read_thread = None


async def ws_handler(websocket):
    ws_clients.add(websocket)
    # 新客户端连接时立即推送当前状态
    try:
        await websocket.send(json.dumps({
            "type": "status",
            "connected": serial_status["connected"],
            "port": serial_status["port"],
            "baudrate": serial_status["baudrate"]
        }))
    except Exception:
        pass

    try:
        async for message in websocket:
            data = json.loads(message)
            action = data.get("action")
            if action == "write" and active_serial and active_serial.is_open:
                cmd = data.get("payload", "") + "\r\n"
                active_serial.write(cmd.encode('utf-8'))
                cmd_stripped = cmd.strip()
                broadcast_to_ui("USER_OVERRIDE", cmd_stripped)
                with buffer_lock:
                    llm_read_buffer.append(("USER_OVERRIDE", cmd_stripped))
            elif action == "connect":
                payload = data.get("payload", {})
                port = payload.get("port", "")
                baud = payload.get("baudrate", 115200)
                if port:
                    connect_port(port, baud)
            elif action == "close":
                close_port()
            elif action == "get_status":
                try:
                    await websocket.send(json.dumps({
                        "type": "status",
                        "connected": serial_status["connected"],
                        "port": serial_status["port"],
                        "baudrate": serial_status["baudrate"]
                    }))
                except Exception:
                    pass
    finally:
        ws_clients.remove(websocket)


def run_ws_server_thread(ws_port):
    global ws_loop, ws_server_running
    import traceback
    async def serve():
        global ws_loop, ws_server_running
        server = await websockets.serve(ws_handler, "0.0.0.0", ws_port)
        ws_loop = asyncio.get_running_loop()
        ws_server_running = True
        await asyncio.Future()  # run forever
    try:
        asyncio.run(serve())
    except Exception:
        ws_server_running = False
        traceback.print_exc()

@mcp.tool()
def start_monitor_ui(http_port: int = 8080) -> str:
    """启动监控UI。成功后返回链接给用户。"""
    global ws_server_running
    ws_port = http_port + 1 
    if ws_server_running: return f"已启动: http://localhost:{http_port}"
    try:
        threading.Thread(target=run_ws_server_thread, args=(ws_port,), daemon=True).start()
        threading.Thread(target=run_http_server, args=(http_port, ws_port), daemon=True).start()
        return f"启动成功！请点击: http://localhost:{http_port}"
    except Exception as e: return f"失败: {str(e)}"

@mcp.tool()
def list_ports() -> str:
    ports = serial.tools.list_ports.comports()
    return "\n".join([f"{p.device} - {p.description}" for p in ports]) if ports else "未找到串口"

@mcp.tool()
def close_port() -> str:
    global active_serial, serial_status
    try:
        if active_serial and active_serial.is_open:
            stop_serial_reader()
            active_serial.close()
            port_name = serial_status.get("port", "未知")
            serial_status["connected"] = False
            serial_status["port"] = None
            broadcast_to_ui("SYSTEM", f"已断开串口 {port_name}")
            broadcast_status()
            return f"已断开串口 {port_name}"
        serial_status["connected"] = False
        broadcast_status()
        return "串口未打开"
    except Exception as e:
        return f"断开失败: {str(e)}"

@mcp.tool()
def connect_port(port: str, baudrate: int = 115200) -> str:
    global active_serial, serial_status
    try:
        if active_serial and active_serial.is_open:
            stop_serial_reader()
            active_serial.close()
        active_serial = serial.Serial(port, baudrate, timeout=0)
        start_serial_reader()
        serial_status["connected"] = True
        serial_status["port"] = port
        serial_status["baudrate"] = baudrate
        broadcast_to_ui("SYSTEM", f"已连接到 {port}")
        broadcast_status()
        return f"已连接到 {port} (波特率: {baudrate})"
    except Exception as e:
        serial_status["connected"] = False
        broadcast_to_ui("SYSTEM", f"连接失败: {str(e)}")
        broadcast_status()
        return f"连接失败: {str(e)}"

@mcp.tool()
def write_data(data: str) -> str:
    global active_serial
    if not active_serial or not active_serial.is_open: return "串口未打开"
    try:
        payload = data + "\r\n" if not data.endswith("\n") else data
        active_serial.write(payload.encode('utf-8'))
        broadcast_to_ui("LLM", data.strip())
        return "发送成功"
    except Exception as e: return str(e)

@mcp.tool()
def read_data() -> str:
    """读取串口数据。优先返回用户干预与下位机回应的历史记录，清空后返回实时串口数据或无数据。"""
    global active_serial, llm_read_buffer
    if not active_serial or not active_serial.is_open: return "串口未打开"
    try:
        # 优先返回 buffer 中累积的用户干预 + 下位机回应
        with buffer_lock:
            if llm_read_buffer:
                result = []
                for source, msg in llm_read_buffer:
                    label = "[用户]" if source == "USER_OVERRIDE" else "[下位机]"
                    result.append(f"{label} {msg}")
                llm_read_buffer.clear()
                return "\n".join(result)
        return "无数据"
    except Exception as e: return str(e)

# --- 必须包装在 main 函数中供 uvx 调用 ---
def main():
    mcp.run(transport='stdio')
