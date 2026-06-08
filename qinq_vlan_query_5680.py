#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Huawei MA5680 QinQ 内层 VLAN 查询工具 (Python 3.13 轻量版)
依赖: 仅标准库 (socket, re, getpass, logging)
"""
import re
import sys
import socket
import time
import getpass
import logging
import csv
from datetime import datetime
from typing import List, Set, Dict, Optional

# ================= 日志双通道配置 =================
logger = logging.getLogger("QinQ_Tool")
logger.setLevel(logging.DEBUG)  # 全局记录级别（文件将捕获所有日志）

# 统一日志格式
log_fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S")

# 1. 控制台输出：保持简洁，仅显示 INFO 及以上（避免 DEBUG 刷屏干扰交互）
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_fmt)

# 2. 文件输出：记录完整 DEBUG 日志，方便事后排查
log_filename = f"qinq_query_5680_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
csv_filename = f"可用内层vlan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
file_handler = logging.FileHandler(log_filename, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(log_fmt)

# 绑定处理器
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# 启动时提示日志路径
logger.info(f"📝 运行日志已自动保存至: {log_filename}")


def find_missing_numbers(lst, max_num=4095):
    """返回 1..max_num 范围内不在 lst 中的数字列表"""
    full_set = set(range(1, max_num + 1))
    given_set = set(lst)  # 自动去重
    missing = full_set - given_set
    return sorted(missing)  # 排序后输出，便于阅读


def write_to_csv(numbers, filename=csv_filename):
    """将数字列表写入 CSV 文件，每行一个数字"""
    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        for num in numbers:
            writer.writerow([num])  # 每个数字单独一行
    logging.info(f"已生成文件{filename}")


# ================= 轻量 Telnet 客户端 (零依赖) =================
class SimpleTelnetSession:
    """基于 socket 的轻量 Telnet 客户端（Python 3.13 兼容，零 bytes/str 混用）"""

    def __init__(self, host: str, port: int = 23, timeout: int = 10):
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(timeout)
        # ✅ 预编译 Telnet IAC 过滤正则（安全高效）
        self._iac_pattern = re.compile(rb'\xff[\x00-\xff]{2}')

    def _read_until(self, pattern: str, timeout: int = 2) -> str:
        start = time.time()
        regex = re.compile(pattern)
        buffer = ""
        while time.time() - start < timeout:
            try:
                chunk = self.sock.recv(8192)
                if not chunk:
                    raise ConnectionError("连接已断开")

                # ✅ 安全过滤 IAC 协商字节，避免手动索引越界
                clean = self._iac_pattern.sub(b'', chunk).decode("utf-8", errors="ignore")
                buffer += clean

                match = regex.search(buffer)
                if match:
                    output = buffer[:match.start()]

                    return output.strip()
            except socket.timeout:
                continue
        raise TimeoutError(f"等待提示符 '{pattern}' 超时")

    def login(self, username: str, password: str) -> None:
        logger.debug("等待用户名提示...")
        self._read_until(r"[Uu]ser\s*[Nn]ame[:\s]*")
        self.sock.sendall(f"{username}\n".encode())

        logger.debug("等待密码提示...")
        self._read_until(r"[Pp]assword[:\s]*")
        self.sock.sendall(f"{password}\n".encode())

        logger.debug("等待用户视图提示符(>)...")
        for i in range(5):
            self.sock.sendall(b" ")
        self._read_until(r">")
        logger.info("✅ 登录成功。当前处于用户视图")

    def enable(self) -> None:
        logger.debug("发送 enable 提权...")
        self.sock.sendall(f"enable\r\n".encode())
        self._read_until(r"#")
        logger.info("✅ 已进入特权模式(#)")

    def disable_paging(self) -> bool:
        """尝试将终端行数设为最大，减少翻页频率"""
        logger.debug("尝试设置终端分页行数 (scroll 512)...")
        try:
            self.sock.sendall(f"scroll 512\r\n".encode())
            logger.info("✅ 已设置终端行数: 512")
            return True
        except Exception:
            logger.warning("⚠️ 设置 scroll 512 失败，将完全依赖自动翻页逻辑")
            return False

    def send_long_command(self, cmd: str, timeout: int = 10) -> str:
        cmd = cmd.strip()  # ✅ 清除外部传入的多余换行符
        logger.debug(f"发送长命令: {cmd}")
        self.sock.sendall(f"{cmd}\r\n\r\n".encode())
        time.sleep(0.4)  # ✅ 给 MA5680 留出命令解析与首包响应时间

        full_output = []
        buffer = ""
        start_time = time.time()
        last_data_time = time.time()

        # ✅ 弹性正则：兼容空格/括号/引号差异
        more_pattern = re.compile(r'----\s*More\s*\(?\s*Press\s+[\'"]Q[\'"]\s+to\s+break\s*\)?\s*----')
        prompt_pattern = re.compile(r'#\s*$')

        while time.time() - start_time < timeout:
            try:
                self.sock.settimeout(2.0)
                chunk = self.sock.recv(8192)
                if not chunk:
                    break

                last_data_time = time.time()
                # 清理 Telnet IAC 协商字节
                clean = re.sub(rb'\xff[\x00-\xff]{2}', b'', chunk).decode("utf-8", errors="ignore")
                buffer += clean

                # 1️⃣ 处理分页（while 处理同一批次到达的多个分页符）
                while more_pattern.search(buffer):
                    logger.debug("[PAGING] 触发分页，发送空格翻页...")
                    match = more_pattern.search(buffer)
                    full_output.append(buffer[:match.start()])
                    buffer = buffer[match.end():]  # ✅ 精准保留 More 之后的数据
                    self.sock.sendall(b" ")
                    last_data_time = time.time()
                    start_time = time.time()  # 重置总超时
                    time.sleep(0.2)

                # 2️⃣ 检测结束提示符 #（容忍末尾空格/换行）
                if prompt_pattern.search(buffer):
                    match = prompt_pattern.search(buffer)
                    full_output.append(buffer[:match.start()])
                    buffer = ""
                    # logger.debug("[END] 检测到特权提示符 #，命令执行完毕")
                    break

            except socket.timeout:
                # 连续 4 秒无新数据，判定命令已输出完毕
                if time.time() - last_data_time > 4:
                    if buffer.strip():
                        full_output.append(buffer)
                    break
                continue

        # 🧹 清洗并返回
        result = "".join(full_output) + buffer
        result = result.replace('\r\n', '\n').replace('\r', '\n')
        result = re.sub(r'----\s*More[^\n]*\n?', '\n', result)  # 清理残留 More 行
        return result.strip()

    def close(self) -> None:
        try:
            self.sock.sendall(b"quit\r\n")
        except Exception:
            pass
        self.sock.close()
        logger.info("🔌 会话已安全关闭。")


# ================= 解析层 (Parsing Layer) =================
class HuaweiOutputParser:
    # 1. 匹配 ANSI 终端控制码（如 \x1b[37D 光标左移指令）
    _ANSI_PATTERN = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
    # 2. 索引匹配正则（依赖 \n 定位行首）
    _INDEX_PATTERN = re.compile(r"^\s*(\d+)\s+\d+\s+QinQ", re.MULTILINE)
    # 3. 内层 VLAN 匹配正则
    _LABEL_PATTERN = re.compile(r"(?:Label|C-VLAN|Inner\s*VLAN)\s*[:\s]+(\d+)", re.IGNORECASE)

    @classmethod
    def extract_service_indices(cls, output: str) -> List[int]:
        # ✅ 步骤1：剥离 ANSI 控制序列
        clean_text = cls._ANSI_PATTERN.sub('', output)
        # ✅ 步骤2：统一换行符（关键！保留 \n 才能让 MULTILINE 锚点 ^ 逐行生效）
        clean_text = clean_text.replace('\r\n', '\n').replace('\r', '\n')
        # ✅ 步骤3：仅清理乱码占位符，绝对避开 \n、\r 和空格
        clean_text = clean_text.replace('\ufffd', '')

        # ✅ 步骤4：执行正则提取
        indices = [int(m.group(1)) for m in cls._INDEX_PATTERN.finditer(clean_text)]
        return list(dict.fromkeys(idx for idx in indices if 0 <= idx <= 200000))

    @classmethod
    def extract_inner_vlan_label(cls, output: str) -> Optional[int]:
        match = cls._LABEL_PATTERN.search(output)
        return int(match.group(1)) if match else None


class VlanAnalyzer:
    VALID_VLAN_RANGE = range(1, 4096)

    def __init__(self, outer_vlan: int):
        self.outer_vlan = outer_vlan
        self._used_vlans: Set[int] = set()

    def add_labels(self, labels: List[Optional[int]]) -> None:
        self._used_vlans.update({lbl for lbl in labels if lbl in self.VALID_VLAN_RANGE})

    def get_report(self) -> Dict:
        sorted_used = sorted(self._used_vlans)
        used_count = len(sorted_used)
        ranges, start = [], 1
        for v in sorted_used:
            if start < v: ranges.append(f"{start}-{v - 1}")
            start = v + 1
        if start <= 4095: ranges.append(f"{start}-4095")
        return {"outer_vlan": self.outer_vlan, "used_count": used_count,
                "used_list": sorted_used, "available_count": 4095 - used_count,
                "available_ranges": ranges}


# ================= 交互与主流程 =================
def collect_inputs() -> Dict:
    logger.info("=" * 55)
    logger.info("华为 MA5680 QinQ 内层 VLAN 查询工具 (Python 3.13 轻量版)")
    logger.info("=" * 55)
    try:
        ip = input("目标设备 IP: ").strip()
        username = input("用户名: ").strip()
        password = getpass.getpass("密码: ")
        outer_vlan = int(input("外层 VLAN ID (1-4095): ").strip())
        if not (1 <= outer_vlan <= 4095): raise ValueError
        return {"ip": ip, "username": username, "password": password, "outer_vlan": outer_vlan}
    except ValueError:
        logger.error("输入格式错误，请检查 VLAN 范围")
        sys.exit(1)


def print_report(report: Dict) -> None:
    logger.info("\n" + "=" * 50)
    logger.info(f"查询结果 | 外层 VLAN: {report['outer_vlan']}")
    logger.info("-" * 50)
    logger.info(f"已使用 内层 VLAN 数量 : {report['used_count']}")
    logger.info(f"已使用 内层 VLAN 列表   : {report['used_list']}")
    logger.info(f"剩余 空闲内层 VLAN 数量       : {report['available_count']}")
    if report['available_ranges']:
        dr = report['available_ranges'][:]
        logger.info(f"空闲内层 VLAN 区间     : {', '.join(dr)}{'...' if len(report['available_ranges']) > 5 else ''}")
    logger.info("=" * 50 + "\n")
    available_innervlan = find_missing_numbers(report['used_list'])
    write_to_csv(available_innervlan)


def main() -> None:
    cfg = collect_inputs()
    session = SimpleTelnetSession(cfg["ip"], timeout=5)
    parser = HuaweiOutputParser()
    analyzer = VlanAnalyzer(cfg["outer_vlan"])

    try:
        # 输入用户名和密码并登录
        session.login(cfg["username"], cfg["password"])
        # 进入特权模式
        session.enable()
        # 设置最大显示行数为512
        session.disable_paging()
        # 对外层 VLAN 进行查询
        cmd1 = f"display service-port vlan {cfg['outer_vlan']}"
        logger.info(f"执行: {cmd1}")
        out1 = session.send_long_command(cmd1)

        if "not exist" in out1.lower() or "no service-port" in out1.lower():
            logger.warning(f"外层 VLAN {cfg['outer_vlan']} 下未配置业务流。")
            print_report(analyzer.get_report())
            return

        indices = parser.extract_service_indices(out1)
        if not indices:
            # 打印设备真实回显，方便精准定位格式差异
            logger.info(f"解析失败。请检查以下设备原始回显:")
            logger.info(f"{'=' * 60}")
            logger.info(out1)
            logger.info(f"{'=' * 60}")
            logger.error("未解析到业务流索引。请粘贴上方回显以便调整正则。")
            return
        logger.info(f"发现 {len(indices)} 条业务流，开始提取内层 VLAN...")

        labels = []
        total = len(indices)
        # start_time = time.time()

        # 依次查询 INDEX 对应的 LABEL
        for i, idx in enumerate(indices, 1):
            cmd2 = f"display service-port {idx}"
            out2 = session.send_long_command(cmd2)
            labels.append(parser.extract_inner_vlan_label(out2))

            # 实时进度显示（\r 实现单行覆盖刷新）
            # 百分比显示
            percent = (i / total) * 100
            # 估计剩余时间
            # elapsed = time.time() - start_time
            # eta = (elapsed / i) * (total - i) if i > 0 else 0

            # 末尾预留空格防止历史长字符残留
            sys.stdout.write(
                f"\r查询进度: [{i:>4}/{total}] | {percent:5.1f}% | 当前索引: {idx}")
            sys.stdout.flush()

        logger.info(f"提取完成: 原始流数={len(indices)} | 成功解析={len(labels)}")
        logger.info(f"去重前 VLAN 数: {len(labels)} | 去重后唯一 VLAN 数: {len(set(labels))}")

        analyzer.add_labels(labels)
        print_report(analyzer.get_report())

    except Exception as e:
        logger.error(f"运行异常: {e}")
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
