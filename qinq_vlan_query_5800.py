#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Huawei MA5800 QinQ 内层 VLAN 查询工具 (Python 3.13 轻量版)
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
log_filename = f"qinq_query_5800_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
csv_file = f"可用内层vlan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
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


def write_to_csv(numbers, filename=csv_file):
    """将数字列表写入 CSV 文件，每行一个数字"""
    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        for num in numbers:
            writer.writerow([num])  # 每个数字单独一行
    logging.info(f"已生成文件{filename}")

# ================= 轻量 Telnet 客户端 (零依赖) =================
class SimpleTelnetSession:
    """基于 socket 的轻量 Telnet 客户端（Python 3.13 兼容，零 bytes/str 混用）"""

    def __init__(self, host: str, port: int = 23, timeout: int = 15):
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(timeout)
        self._buf = ""  # ✅ 统一使用字符串缓冲区，避免正则类型冲突

    def _read_until(self, pattern: str, timeout: int = 15) -> str:
        start = time.time()
        regex = re.compile(pattern)
        while time.time() - start < timeout:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("连接已断开")

                # 🔍 过滤 Telnet IAC 协商字节 (0xFF + 2字节指令)，仅保留可打印字符
                clean = bytearray()
                i = 0
                while i < len(chunk):
                    if chunk[i] == 255 and i + 2 < len(chunk):
                        i += 3  # 跳过 IAC 序列
                    else:
                        clean.append(chunk[i])
                        i += 1
                self._buf += clean.decode("utf-8", errors="ignore")

                match = regex.search(self._buf)
                if match:
                    output = self._buf[:match.start()]
                    self._buf = self._buf[match.end():]
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
        self._read_until(r">")
        logger.info("✅ 登录成功。当前处于用户视图")

    def enable(self) -> None:
        logger.debug("发送 enable 提权...")
        self.sock.sendall(f"enable\n".encode())
        self._read_until(r"#")
        logger.info("✅ 已进入特权模式(#)")

    def send_command(self, cmd: str) -> str:
        self.sock.sendall(f"{cmd}\n\n".encode())
        time.sleep(0.1)  # 给 OLT 留出命令处理与网络缓冲时间
        return self._read_until(r"#").strip()

    def close(self) -> None:
        try:
            self.sock.sendall(b"quit\r\n")
        except Exception:
            pass
        self.sock.close()
        logger.info("🔌 会话已安全关闭。")


# ================= 解析层 (Parsing Layer) =================
class HuaweiOutputParser:
    # ✅ 适配真实回显：INDEX在行首，格式为 "  <INDEX> <VLAN_ID> QinQ ..."
    # 示例: "3 2046 QinQ     gpon 0/1 /13 1    1     vlan  101        10   9    down"
    _INDEX_PATTERN = re.compile(r"^\s*(\d+)\s+\d+\s+QinQ", re.MULTILINE)
    _LABEL_PATTERN = re.compile(r"(?:Label|C-VLAN|Inner\s+VLAN)\s*[:\s]+(\d+)", re.IGNORECASE)

    @classmethod
    def extract_service_indices(cls, output: str) -> List[int]:
        # 使用 finditer 直接扫描全文，避免 splitlines 导致的对齐偏差
        indices = [int(m.group(1)) for m in cls._INDEX_PATTERN.finditer(output)]
        # 过滤异常值并去重保序
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
    logger.info("华为 MA5800 QinQ 内层 VLAN 查询工具 (Python 3.13 轻量版)")
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
    logger.info(f"📊 查询结果 | 外层 VLAN: {report['outer_vlan']}")
    logger.info("-" * 50)
    logger.info(f"✅ 已使用内层 VLAN 数量 : {report['used_count']}")
    logger.info(f"📋 已使用 VLAN 列表   : {report['used_list']}")
    logger.info(f"🟢 剩余可用数量       : {report['available_count']}")
    if report['available_ranges']:
        dr = report['available_ranges'][:]
        logger.info(f"🔍 可用区间 (示例)     : {', '.join(dr)}{'...' if len(report['available_ranges']) > 5 else ''}")
    logger.info("=" * 50 + "\n")
    available_innervlan = find_missing_numbers(report['used_list'])
    write_to_csv(available_innervlan)

def main() -> None:
    cfg = collect_inputs()
    session = SimpleTelnetSession(cfg["ip"], timeout=15)
    parser = HuaweiOutputParser()
    analyzer = VlanAnalyzer(cfg["outer_vlan"])

    try:
        session.login(cfg["username"], cfg["password"])
        session.enable()

        cmd1 = f"display service-port vlan {cfg['outer_vlan']} | no-more"
        logger.info(f"执行: {cmd1}")

        out1 = session.send_command(cmd1)

        if "not exist" in out1.lower() or "no service-port" in out1.lower():
            logger.warning(f"外层 VLAN {cfg['outer_vlan']} 下未配置业务流。")
            print_report(analyzer.get_report())
            return

        indices = parser.extract_service_indices(out1)
        if not indices:
            # 🟢 关键：打印设备真实回显，方便精准定位格式差异
            print(f"\n⚠️ 解析失败。请检查以下设备原始回显：\n{'=' * 60}")
            print(out1)
            print(f"{'=' * 60}\n")
            logger.error("未解析到业务流索引。请粘贴上方回显以便调整正则。")
            return
        logger.info(f"发现 {len(indices)} 条业务流，开始提取内层 VLAN...")

        labels = []
        total = len(indices)
        start_time = time.time()

        for i, idx in enumerate(indices, 1):
            cmd2 = f"display service-port {idx} | no-more"
            out2 = session.send_command(cmd2)
            labels.append(parser.extract_inner_vlan_label(out2))

            # 🟢 实时进度显示（\r 实现单行覆盖刷新）
            percent = (i / total) * 100
            # elapsed = time.time() - start_time
            # eta = (elapsed / i) * (total - i) if i > 0 else 0
            # 末尾预留空格防止历史长字符残留
            sys.stdout.write(
                f"\r📊 查询进度: [{i:>4}/{total}] | {percent:5.1f}% | 当前索引: {idx}")
            sys.stdout.flush()

        # print("\n✅ 所有业务流查询完毕！")
        logger.info(f"📊 提取完成: 原始流数={len(indices)} | 成功解析={len(labels)}")
        logger.info(f"🔍 去重前 VLAN 数: {len(labels)} | 去重后唯一 VLAN 数: {len(set(labels))}")

        analyzer.add_labels(labels)
        print_report(analyzer.get_report())

    except Exception as e:
        logger.error(f"运行异常: {e}")
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
