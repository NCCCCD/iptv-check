#!/usr/bin/env python3
"""IPTV M3U 播放列表检测 + 更新替换工具.

用法:
     python iptv-check.py                                            # 全流程：检测→更新→推送(可选)
     python iptv-check.py <m3u-url>                                  # 指定播放列表运行全流程
     python iptv-check.py --detect-only                              # 仅检测，不更新
     python iptv-check.py --set-token                                # 交互式配置 GitHub Token
     python iptv-check.py --update <仓库-m3u-url> --push            # 仅更新合并并推送
"""

import argparse
import concurrent.futures
import dataclasses
import getpass
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import base64

try:
    import ssl as _ssl
except ImportError:
    _ssl = None

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Stream:
    index: int
    name: str
    url: str
    logo: str = ""
    group: str = ""
    raw_line: str = ""

@dataclasses.dataclass
class CheckResult:
    stream: Stream
    reachable: bool = False
    http_ok: bool = False
    status_code: int = 0
    content_type: str = ""
    dns_time: float = 0.0
    connect_time: float = 0.0
    response_time: float = 0.0
    error: str = ""
    hls_valid: bool | None = None
    hls_segments: int = 0
    hls_error: str = ""
    throughput_mbps: float = 0.0

@dataclasses.dataclass
class ChannelEntry:
    tvg_name: str
    display_name: str
    url: str
    logo: str = ""
    group: str = ""
    tvg_id: str = ""
    catchup: str = ""
    catchup_source: str = ""
    catchup_days: str = ""
    extinf: str = ""
    index: int = 0

# ---------------------------------------------------------------------------
# M3U 解析（完整版：保留所有元数据）
# ---------------------------------------------------------------------------

RE_EXTINF = re.compile(r'#EXTINF:(-?\d+)(?:\s+(.*?))?,(.*)')

def parse_extinf_attrs(line: str) -> dict:
    m = RE_EXTINF.match(line)
    if not m:
        return {}
    attrs_str = m.group(2) or ""
    display = m.group(3) or ""
    logo = re.search(r'tvg-logo="([^"]*)"', attrs_str)
    group = re.search(r'group-title="([^"]*)"', attrs_str)
    tvg_name = re.search(r'tvg-name="([^"]*)"', attrs_str)
    tvg_id = re.search(r'tvg-id="([^"]*)"', attrs_str)
    catchup = re.search(r'catchup="([^"]*)"', attrs_str)
    catchup_source = re.search(r'catchup-source="([^"]*)"', attrs_str)
    catchup_days = re.search(r'catchup-days="([^"]*)"', attrs_str)
    return {
        "display": display.strip(" ,"),
        "logo": logo.group(1) if logo else "",
        "group": group.group(1) if group else "",
        "tvg_name": tvg_name.group(1) if tvg_name else "",
        "tvg_id": tvg_id.group(1) if tvg_id else "",
        "catchup": catchup.group(1) if catchup else "",
        "catchup_source": catchup_source.group(1) if catchup_source else "",
        "catchup_days": catchup_days.group(1) if catchup_days else "",
    }

def parse_m3u_full(text: str) -> list[ChannelEntry]:
    entries: list[ChannelEntry] = []
    info: dict = {}
    idx = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('#EXTINF:'):
            info = parse_extinf_attrs(line)
            info["extinf"] = line
        elif line.startswith('#'):
            continue
        else:
            name = info.get("display") or os.path.basename(urllib.parse.urlparse(line).path) or "未知"
            entries.append(ChannelEntry(
                tvg_name=info.get("tvg_name", ""),
                display_name=name,
                url=line,
                logo=info.get("logo", ""),
                group=info.get("group", ""),
                tvg_id=info.get("tvg_id", ""),
                catchup=info.get("catchup", ""),
                catchup_source=info.get("catchup_source", ""),
                catchup_days=info.get("catchup_days", ""),
                extinf=info.get("extinf", ""),
                index=idx,
            ))
            idx += 1
            info = {}
    return entries

def generate_m3u(entries: list[ChannelEntry], header: str = "#EXTM3U\n") -> str:
    lines = [header.rstrip()]

    for e in entries:
        attrs = f'tvg-id="{e.tvg_id}"' if e.tvg_id else ""
        if e.tvg_name:
            attrs += f' tvg-name="{e.tvg_name}"'
        if e.logo:
            attrs += f' tvg-logo="{e.logo}"'
        if e.group:
            attrs += f' group-title="{e.group}"'
        if e.catchup:
            attrs += f' catchup="{e.catchup}"'
        if e.catchup_source:
            attrs += f' catchup-source="{e.catchup_source}"'
        if e.catchup_days:
            attrs += f' catchup-days="{e.catchup_days}"'
        lines.append(f'#EXTINF:-1 {attrs.strip()},{e.display_name}')
        lines.append(e.url)
    return '\n'.join(lines) + '\n'

# ---------------------------------------------------------------------------
# URL 处理
# ---------------------------------------------------------------------------

def resolve_url(url: str) -> str:
    m = re.match(r'https?://github\.com/([^/]+/[^/]+)/blob/(.+?)(?:\?.*)?$', url)
    if m:
        return f'https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}'
    m = re.match(r'https?://gist\.github\.com/([^/]+)/([^/]+)', url)
    if m:
        return f'https://gist.githubusercontent.com/{m.group(1)}/{m.group(2)}/raw'
    return url

def fetch(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "iptv-check/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='replace')

# ---------------------------------------------------------------------------
# 流检测
# ---------------------------------------------------------------------------

RE_UDP_RTP = re.compile(r'^(udp|rtp)/(\d+\.\d+\.\d+\.\d+):(\d+)$')

def _check_udp_stream(result: CheckResult, host: str, port: int, timeout: int) -> CheckResult:
    try:
        t0 = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', port))
        mcast = struct.pack('4s4s', socket.inet_aton(host), socket.inet_aton('0.0.0.0'))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mcast)
        result.dns_time = time.monotonic() - t0
        sock.settimeout(timeout)
        t0 = time.monotonic()
        try:
            data, _addr = sock.recvfrom(1024)
            result.connect_time = time.monotonic() - t0
            result.response_time = result.connect_time
            result.reachable = True
            result.http_ok = True
            result.status_code = 200
            result.content_type = f'UDP 多播 ({len(data)}B)'
        except socket.timeout:
            result.connect_time = time.monotonic() - t0
            result.reachable = True
            result.status_code = 200
            result.content_type = 'UDP 多播'
            result.error = '多播组已加入，等待数据中'
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mcast)
        except OSError:
            pass
        sock.close()
    except OSError as e:
        result.error = f'UDP 多播失败: {e}'
    return result

def _tcp_probe(result: CheckResult, host: str, port: int, path: str, use_ssl: bool, timeout: int, request: bytes | None = None) -> CheckResult:
    try:
        t0 = time.monotonic()
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        result.dns_time = time.monotonic() - t0
        if not infos:
            result.error = 'DNS 返回空地址'
            return result
        family, type_, proto, _cn, addr = infos[0]
    except OSError as e:
        result.error = f'DNS 失败: {e}'
        return result

    try:
        t0 = time.monotonic()
        sock = socket.socket(family, type_, proto)
        sock.settimeout(timeout)
        sock.connect(addr)
        result.connect_time = time.monotonic() - t0
        result.reachable = True
    except OSError as e:
        result.error = f'TCP 失败: {e}'
        return result

    try:
        if use_ssl:
            if _ssl is None:
                raise OSError('SSL 模块不可用')
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)

        t0 = time.monotonic()
        if request is None:
            request = f'GET {path or "/"} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: iptv-check/1.0\r\nAccept: */*\r\n\r\n'.encode()
        sock.sendall(request)

        data = b''
        while len(data) < 4096:
            try:
                chunk = sock.recv(4096 - len(data))
                if not chunk:
                    break
                data += chunk
                if b'\r\n\r\n' in data:
                    break
            except socket.timeout:
                break

        result.response_time = time.monotonic() - t0

        if not data:
            result.error = '连接建立但无数据返回'
            sock.close()
            return result

        if data.startswith(b'HTTP/'):
            status_line = data.split(b'\r\n', 1)[0].decode(errors='replace')
            parts = status_line.split(' ', 2)
            result.status_code = int(parts[1]) if len(parts) > 1 else 0
            result.http_ok = 200 <= result.status_code < 400
            ct_match = re.search(rb'Content-Type:\s*(\S+)', data, re.IGNORECASE)
            if ct_match:
                result.content_type = ct_match.group(1).decode()
        elif data[0] == 0x47:
            result.http_ok = True
            result.status_code = 200
            result.content_type = 'video/MP2T (TS 流)'
        else:
            result.http_ok = True
            result.status_code = 200
            result.content_type = f'数据流 (首字节 0x{data[0]:02x})'

        # 4K 频道持续测速 (~2s)
        if result.http_ok and '4K' in result.stream.name:
            try:
                sock.settimeout(2.0)
                t_start = time.monotonic()
                total = len(data)
                while time.monotonic() - t_start < 2.0:
                    try:
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                    except socket.timeout:
                        break
                elapsed = time.monotonic() - t_start
                if elapsed > 0.5:
                    result.throughput_mbps = round((total * 8) / (elapsed * 1_000_000), 1)
            except OSError:
                pass

        sock.close()
    except OSError as e:
        result.error = f'读取失败: {e}'

    return result

def check_stream(stream: Stream, timeout: int) -> CheckResult:
    result = CheckResult(stream=stream)
    url = stream.url
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in ('udp', 'rtp'):
        return _check_udp_stream(result, parsed.hostname or '', parsed.port or 0, timeout)

    if not scheme:
        m = RE_UDP_RTP.match(url)
        if m:
            return _check_udp_stream(result, m.group(2), int(m.group(3)), timeout)

    host = parsed.hostname or ''
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query

    if scheme == 'rtsp':
        port = parsed.port or 554
        req = f'DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: iptv-check/1.0\r\nAccept: application/sdp\r\n\r\n'.encode()
        return _tcp_probe(result, host, port, path, False, timeout, request=req)

    port = parsed.port or (443 if scheme == 'https' else 80)
    use_ssl = scheme == 'https'
    return _tcp_probe(result, host, port, path, use_ssl, timeout)

def run_checks(entries: list[ChannelEntry], max_workers: int, timeout: int) -> list[CheckResult]:
    streams = [Stream(index=e.index, name=e.display_name, url=e.url, logo=e.logo, group=e.group) for e in entries]
    results: list[CheckResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_to_s = {pool.submit(check_stream, s, timeout): s for s in streams}
        for fut in concurrent.futures.as_completed(fut_to_s):
            results.append(fut.result())
    results.sort(key=lambda r: r.stream.index)
    return results

# ---------------------------------------------------------------------------
# RTSP 302 重定向解析
# ---------------------------------------------------------------------------

def _connect_rtsp(host: str, port: int, proxy_url: str, timeout: int) -> socket.socket | None:
    """Connect to RTSP server, optionally through HTTP CONNECT proxy."""
    try:
        if proxy_url:
            up = urllib.parse.urlparse(proxy_url)
            ph = up.hostname or ''
            pp = up.port or 8080
            infos = socket.getaddrinfo(ph, pp, type=socket.SOCK_STREAM)
            if not infos:
                return None
            family, type_, proto, _cn, addr = infos[0]
            sock = socket.socket(family, type_, proto)
            sock.settimeout(timeout)
            sock.connect(addr)
            connect_req = f'CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n'.encode()
            sock.sendall(connect_req)
            resp = b''
            while b'\r\n\r\n' not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
            if b'200' not in resp.split(b'\r\n', 1)[0]:
                sock.close()
                return None
            return sock
        else:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            if not infos:
                return None
            family, type_, proto, _cn, addr = infos[0]
            sock = socket.socket(family, type_, proto)
            sock.settimeout(timeout)
            sock.connect(addr)
            return sock
    except OSError:
        return None

def resolve_rtsp_redirect(url: str, timeout: int = 8, proxy_url: str = '') -> str:
    """Follow RTSP 302 redirect chain and return the final URL."""
    if not url.startswith('rtsp://'):
        return url

    max_hops = 5
    current = url
    seen: set[str] = set()

    for hop in range(max_hops):
        if current in seen:
            break
        seen.add(current)

        parsed = urllib.parse.urlparse(current)
        host = parsed.hostname or ''
        port = parsed.port or 554
        if not host:
            return current

        req = f'DESCRIBE {current} RTSP/1.0\r\nCSeq: {hop + 1}\r\nUser-Agent: iptv-check/1.0\r\nAccept: application/sdp\r\n\r\n'.encode()

        sock = _connect_rtsp(host, port, proxy_url, timeout)
        if sock is None:
            return current

        try:
            sock.sendall(req)
            data = b''
            while len(data) < 8192:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b'\r\n\r\n' in data:
                        break
                except socket.timeout:
                    break
            sock.close()
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            return current

        if not data:
            return current

        status = data.split(b'\r\n', 1)[0].decode(errors='replace')
        code_str = status.split(' ', 2)
        if len(code_str) >= 2:
            try:
                code = int(code_str[1])
            except ValueError:
                return current
            if code == 302:
                rm = re.search(rb'Location:\s*(\S+)', data, re.IGNORECASE)
                if rm:
                    loc = rm.group(1).decode(errors='replace').strip()
                    if loc.startswith('rtsp://'):
                        current = loc
                        continue
        return current

    return current

# ---------------------------------------------------------------------------
# TS 流深度分析（PAT/PMT/SDT/SPS）
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class StreamAnalysis:
    index: int
    name: str
    valid_ts: bool = False
    ts_sync_ratio: float = 0.0
    has_video: bool = False
    video_codec: str = ""
    has_audio: bool = False
    width: int = 0
    height: int = 0
    service_name: str = ""
    estimated_kbps: int = 0
    error: str = ""

def _read_ue(data: bytes, bitpos: int) -> tuple[int, int]:
    leading = 0
    pos = bitpos
    while pos < len(data) * 8:
        byte_i = pos // 8
        if byte_i >= len(data):
            break
        bit = (data[byte_i] >> (7 - (pos % 8))) & 1
        pos += 1
        if bit:
            break
        leading += 1
    if not leading:
        return 0, pos
    value = 1
    for _ in range(leading):
        byte_i = pos // 8
        if byte_i >= len(data):
            break
        bit = (data[byte_i] >> (7 - (pos % 8))) & 1
        pos += 1
        value = (value << 1) | bit
    return value - 1, pos

def _parse_h264_sps(data: bytes) -> tuple[int, int]:
    if len(data) < 10:
        return 0, 0
    bitpos = 8  # skip nal_unit_type (1 byte)
    bitpos += 24  # profile_idc (8), constraints (8), level_idc (8)
    if bitpos >= len(data) * 8:
        return 0, 0
    _, bitpos = _read_ue(data, bitpos)   # seq_parameter_set_id
    _, bitpos = _read_ue(data, bitpos)   # log2_max_frame_num_minus4
    poc_type, bitpos = _read_ue(data, bitpos)
    if poc_type == 0:
        _, bitpos = _read_ue(data, bitpos)  # log2_max_pic_order_cnt_lsb_minus4
    elif poc_type == 1:
        _, bitpos = _read_ue(data, bitpos)  # delta_pic_order_always_zero_flag
        _, bitpos = _read_ue(data, bitpos)  # offset_for_non_ref_pic (se)
        bitpos += 1  # offset_for_top_to_bottom_field (se) - skip for simplicity
        num_ref, bitpos = _read_ue(data, bitpos)
        for _ in range(min(num_ref, 100)):
            _, bitpos = _read_ue(data, bitpos)
    _, bitpos = _read_ue(data, bitpos)   # max_num_ref_frames
    bitpos += 1  # gaps_in_frame_num_value_allowed_flag
    w_mb, bitpos = _read_ue(data, bitpos)   # pic_width_in_mbs_minus1
    h_mb, bitpos = _read_ue(data, bitpos)   # pic_height_in_map_units_minus1
    w = (w_mb + 1) * 16
    h = (h_mb + 1) * 16
    if w < 320 or w > 7680 or h < 240 or h > 4320:
        return 0, 0
    return w, h

def _parse_h265_sps(data: bytes) -> tuple[int, int]:
    # Strip emulation prevention bytes (0x03)
    raw = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            raw.append(0)
            raw.append(0)
            i += 3
        else:
            raw.append(data[i])
            i += 1
    if len(raw) < 14:
        return 0, 0
    bp = 0
    def rb(n):
        nonlocal bp
        v = 0
        for _ in range(n):
            bi, ii = bp // 8, 7 - (bp % 8)
            if bi >= len(raw):
                return 0
            v = (v << 1) | ((raw[bi] >> ii) & 1)
            bp += 1
        return v
    def rue():
        nonlocal bp
        lead = 0
        while True:
            b = rb(1)
            if b is None or bp >= len(raw) * 8:
                return 0
            if b == 0:
                lead += 1
            else:
                break
        if lead == 0:
            return 0
        suf = rb(lead)
        return (1 << lead) - 1 + suf
    rb(4)   # sps_video_parameter_set_id
    max_sl = rb(3)
    rb(1)   # sps_temporal_id_nesting_flag
    # profile_tier_level(): 2+1+5+32+1+1+1+1+44+8 = 96 bits
    rb(96)
    for _ in range(max_sl):
        rb(2)  # sub_layer_profile_present + sub_layer_level_present
    for _ in range(max_sl):
        pass  # sublayer data already skipped via flags above
    rue()  # sps_seq_parameter_set_id
    chroma = rue()
    if chroma == 3:
        rb(1)  # separate_colour_plane_flag
    width = rue()
    height = rue()
    if width == 0 or height == 0:
        return 0, 0
    cfw = rb(1)
    l = r = t = b = 0
    if cfw:
        l = rue(); r = rue(); t = rue(); b = rue()
    sub_h = {0: 1, 1: 2, 2: 1, 3: 1}.get(chroma, 1)
    sub_v = {0: 1, 1: 2, 2: 2, 3: 1}.get(chroma, 1)
    dw = width - sub_h * (l + r)
    dh = height - sub_v * (t + b)
    if dw < 320 or dw > 7680 or dh < 240 or dh > 4320:
        return 0, 0
    return dw, dh

_TS_SIZE = 188

def _analyze_ts_data(data: bytes) -> dict:
    result = {"valid_ts": False, "sync_ratio": 0.0, "video_pid": 0,
              "audio_pid": 0, "video_codec": "", "width": 0, "height": 0,
              "service_name": ""}
    total = len(data) // _TS_SIZE
    valid = 0
    for i in range(total):
        if data[i * _TS_SIZE] == 0x47:
            valid += 1
    result["sync_ratio"] = valid / max(total, 1)
    result["valid_ts"] = result["sync_ratio"] > 0.9
    if not result["valid_ts"]:
        return result

    pid_video = 0
    pid_audio = 0
    stype_video = 0
    pmt_pids: list[int] = []

    for i in range(total):
        pkt = data[i * _TS_SIZE:(i + 1) * _TS_SIZE]
        if pkt[0] != 0x47:
            continue
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        afc = (pkt[3] >> 4) & 0x03
        pusi = (pkt[1] >> 6) & 1
        payload_start = 4
        if afc == 2 or afc == 3:
            payload_start = 5 + pkt[4] if (afc == 3 and 4 + pkt[4] < _TS_SIZE) else 4
        if payload_start >= _TS_SIZE:
            continue
        payload = pkt[payload_start:]
        if not payload:
            continue

        # PAT (PID 0)
        if pid == 0 and pusi and len(payload) > 8 and payload[0] == 0x00:
            section_len = ((payload[1] & 0x0F) << 8) | payload[2]
            pat_data = payload[8:8 + section_len - 4]
            pos = 0
            while pos + 4 <= len(pat_data):
                prog_num = (pat_data[pos] << 8) | pat_data[pos + 1]
                pmt_pid = ((pat_data[pos + 2] & 0x1F) << 8) | pat_data[pos + 3]
                if prog_num != 0:
                    pmt_pids.append(pmt_pid)
                pos += 4

        # PMT
        if pid in pmt_pids and pusi and len(payload) > 12 and payload[0] == 0x02:
            section_len = ((payload[1] & 0x0F) << 8) | payload[2]
            pmt_data = payload[12:12 + section_len - 4 - 4 - 4]
            pos = 0
            while pos + 5 <= len(pmt_data):
                stype = pmt_data[pos]
                epid = ((pmt_data[pos + 1] & 0x1F) << 8) | pmt_data[pos + 2]
                es_len = ((pmt_data[pos + 3] & 0x0F) << 8) | pmt_data[pos + 4]
                if stype in (0x1B, 0x24, 0x01, 0x02):
                    if not pid_video:
                        pid_video = epid
                        stype_video = stype
                elif stype in (0x03, 0x04, 0x0F, 0x11):
                    if not pid_audio:
                        pid_audio = epid
                pos += 5 + es_len

        # SDT (PID 0x0011)
        if pid == 0x0011 and pusi and len(payload) > 11 and payload[0] == 0x42:
            sec_len = ((payload[1] & 0x0F) << 8) | payload[2]
            sdt_data = payload[11:11 + sec_len - 4 - 3 - 3]
            pos = 0
            while pos + 5 <= len(sdt_data):
                desc_loop = ((sdt_data[pos + 3] & 0x0F) << 8) | sdt_data[pos + 4]
                dpos = pos + 5
                dend = dpos + desc_loop
                while dpos + 2 <= dend and dpos + 2 <= len(sdt_data):
                    tag, dlen = sdt_data[dpos], sdt_data[dpos + 1]
                    if tag == 0x48 and dlen >= 3 and dpos + 2 + dlen <= len(sdt_data):
                        snlen = sdt_data[dpos + 2]
                        if snlen > 0:
                            sname = sdt_data[dpos + 3:dpos + 3 + snlen].decode('utf-8', errors='replace')
                            result["service_name"] = sname
                            break
                    dpos += 2 + dlen
                if result["service_name"]:
                    break
                pos += 5 + desc_loop

    # 没有 PAT/PMT 时，扫描所有 PIDs 找 H.264/H.265 NAL 同步头
    H264_VIDEO_TYPES = {1, 5, 7, 8}
    H265_DEFINITE_TYPES = {33}  # SPS only; VPS(32)/PPS(34) ambiguous w/ H.264 nal_ref_idc=2 slices

    if not pid_video:
        pid_h264: dict[int, set] = {}
        pid_h264_hint: dict[int, int] = {}
        pid_h265_definite: dict[int, set] = {}
        pid_h265_definite_count: dict[int, int] = {}
        pid_h265_hint: dict[int, int] = {}
        for i in range(min(total, 1000)):
            pkt = data[i * _TS_SIZE:(i + 1) * _TS_SIZE]
            if pkt[0] != 0x47:
                continue
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            afc = (pkt[3] >> 4) & 0x03
            pusi = (pkt[1] >> 6) & 1
            if pid == 0x1FFF:
                continue
            payload_start = 4
            if afc == 2 or afc == 3:
                payload_start = 5 + pkt[4] if (afc == 3 and 4 + pkt[4] < _TS_SIZE) else 4
            if payload_start >= _TS_SIZE - 4:
                continue
            payload = pkt[payload_start:]
            if pusi and len(payload) >= 9 and payload[0] == 0 and payload[1] == 0 and payload[2] == 1:
                payload = payload[9 + payload[8]:]
            if len(payload) < 4:
                continue
            for off in range(len(payload) - 4):
                sc_len = 0
                if payload[off:off + 3] == b'\x00\x00\x01':
                    sc_len = 3
                elif off + 4 <= len(payload) and payload[off:off + 4] == b'\x00\x00\x00\x01':
                    sc_len = 4
                if not sc_len:
                    continue
                nalu_byte = payload[off + sc_len]
                h264_type = nalu_byte & 0x1F
                h265_type = (nalu_byte >> 1) & 0x3F
                if h265_type in H265_DEFINITE_TYPES:
                    if off + sc_len + 1 < len(payload):
                        v = payload[off + sc_len + 1]
                        nuh_layer_id = v >> 3
                        tid_plus1 = v & 0x07
                        if not (nuh_layer_id == 0 and 1 <= tid_plus1 <= 6):
                            continue
                    if pid not in pid_h265_definite:
                        pid_h265_definite[pid] = set()
                    pid_h265_definite[pid].add(h265_type)
                    pid_h265_definite_count[pid] = pid_h265_definite_count.get(pid, 0) + 1
                    if h265_type == 33 and not result["width"]:
                        w, h = _parse_h265_sps(payload[off + sc_len + 2:])
                        if w > 0 and h > 0:
                            result["width"] = w
                            result["height"] = h
                    break
                if h264_type in H264_VIDEO_TYPES:
                    if pid not in pid_h264:
                        pid_h264[pid] = set()
                    pid_h264[pid].add(h264_type)
                    pid_h264_hint[pid] = pid_h264_hint.get(pid, 0) + 1
                    if h264_type == 7 and not result["width"]:
                        w, h = _parse_h264_sps(payload[off + sc_len:])
                        if w > 0 and h > 0:
                            result["width"] = w
                            result["height"] = h
                    break
                if h265_type not in (6, 12):
                    pid_h265_hint[pid] = pid_h265_hint.get(pid, 0) + 1
        all_pids = set(pid_h264) | set(pid_h265_definite) | set(pid_h265_hint)
        if all_pids:
            h265_candidates = sorted(pid_h265_definite.keys(),
                                     key=lambda p: (pid_h265_definite_count.get(p, 0), len(pid_h265_definite[p])), reverse=True)
            h264_candidates = sorted(pid_h264.keys(),
                                     key=lambda p: pid_h264_hint.get(p, 0), reverse=True)
            hint_candidates = sorted(pid_h265_hint.keys(),
                                     key=lambda p: pid_h265_hint[p], reverse=True)
            if h265_candidates and pid_h265_definite_count.get(h265_candidates[0], 0) >= 3:
                pid_video = h265_candidates[0]
                stype_video = 0x24
            elif h264_candidates:
                top_h264_score = pid_h264_hint.get(h264_candidates[0], 0)
                top_hint_pid = hint_candidates[0] if hint_candidates else None
                top_hint_score = pid_h265_hint.get(top_hint_pid, 0) if top_hint_pid else 0
                if top_hint_score > top_h264_score * 2 and top_hint_score >= 5:
                    pid_video = top_hint_pid
                    stype_video = 0x24
                else:
                    pid_video = h264_candidates[0]
                    stype_video = 0x1B
            else:
                pid_video = hint_candidates[0]
                stype_video = 0x24
            if not result["width"]:
                for i in range(min(total, 3000)):
                    pkt = data[i * _TS_SIZE:(i + 1) * _TS_SIZE]
                    if pkt[0] != 0x47:
                        continue
                    pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
                    if pid != pid_video:
                        continue
                    afc = (pkt[3] >> 4) & 0x03
                    pusi = (pkt[1] >> 6) & 1
                    payload_start = 4
                    if afc == 2 or afc == 3:
                        payload_start = 5 + pkt[4] if (afc == 3 and 4 + pkt[4] < _TS_SIZE) else 4
                    if payload_start >= _TS_SIZE:
                        continue
                    payload = pkt[payload_start:]
                    if pusi and len(payload) >= 9 and payload[0] == 0 and payload[1] == 0 and payload[2] == 1:
                        payload = payload[9 + payload[8]:]
                    for off in range(len(payload) - 4):
                        sc_len = 0
                        if payload[off:off + 3] == b'\x00\x00\x01':
                            sc_len = 3
                        elif off + 4 <= len(payload) and payload[off:off + 4] == b'\x00\x00\x00\x01':
                            sc_len = 4
                        if not sc_len:
                            continue
                        nalu_byte = payload[off + sc_len]
                        if stype_video == 0x24:
                            if (nalu_byte >> 1) & 0x3F == 33:
                                w, h = _parse_h265_sps(payload[off + sc_len + 2:])
                                if w > 0 and h > 0:
                                    result["width"] = w
                                    result["height"] = h
                                break
                        elif nalu_byte & 0x1F == 7:
                            w, h = _parse_h264_sps(payload[off + sc_len:])
                            if w > 0 and h > 0:
                                result["width"] = w
                                result["height"] = h
                            break
                    if result["width"] > 0:
                        break

    if stype_video == 0x1B:
        result["video_codec"] = "h264"
    elif stype_video == 0x24:
        result["video_codec"] = "h265"
    elif stype_video in (0x01, 0x02):
        result["video_codec"] = "mpeg2"
    if pid_video:
        result["video_pid"] = pid_video
    if pid_audio:
        result["audio_pid"] = pid_audio
    return result

def analyze_stream(stream: Stream, timeout: int) -> StreamAnalysis:
    sa = StreamAnalysis(index=stream.index, name=stream.name)
    parsed = urllib.parse.urlparse(stream.url)
    host = parsed.hostname or ''
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query
    port = parsed.port or 80
    use_ssl = parsed.scheme == 'https'

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not infos:
            sa.error = 'DNS 失败'
            return sa
        family, type_, proto, _cn, addr = infos[0]
        sock = socket.socket(family, type_, proto)
        sock.settimeout(timeout)
        sock.connect(addr)
    except OSError as e:
        sa.error = f'连接失败: {e}'
        return sa

    try:
        if use_ssl:
            if _ssl is None:
                raise OSError('SSL 不可用')
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)

        req = f'GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: iptv-check/1.0\r\nAccept: */*\r\n\r\n'.encode()
        sock.sendall(req)

        # 先读 HTTP 头
        header = b''
        while b'\r\n\r\n' not in header:
            chunk = sock.recv(4096)
            if not chunk:
                break
            header += chunk
        if b'\r\n\r\n' not in header:
            sa.error = 'HTTP 头不完整'
            sock.close()
            return sa

        # 剩余数据（已经在 header 中的 body 部分 + 继续读）
        body_start = header.index(b'\r\n\r\n') + 4
        data = header[body_start:]
        t_start = time.monotonic()
        while len(data) < 4194304 and time.monotonic() - t_start < timeout:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                break

        elapsed = time.monotonic() - t_start
        if elapsed > 0.5 and len(data) > 188:
            sa.estimated_kbps = int((len(data) * 8) / (elapsed * 1000))

        if len(data) < 188:
            sa.error = f'TS 数据不足 ({len(data)}B)'
            sock.close()
            return sa

        ts = _analyze_ts_data(data)
        sa.valid_ts = ts["valid_ts"]
        sa.ts_sync_ratio = ts["sync_ratio"]
        sa.video_codec = ts["video_codec"]
        sa.has_video = bool(ts["video_pid"])
        sa.has_audio = bool(ts["audio_pid"])
        sa.width = ts["width"]
        sa.height = ts["height"]
        sa.service_name = ts["service_name"]

        if not sa.valid_ts:
            sa.error = '非 TS 流'
        elif not sa.has_video:
            sa.error = '无视频流'

        sock.close()
    except OSError as e:
        sa.error = f'读取失败: {e}'

    return sa

def run_analysis(entries: list[ChannelEntry], max_workers: int, timeout: int) -> list[StreamAnalysis]:
    streams = [Stream(index=e.index, name=e.display_name, url=e.url) for e in entries]
    results: list[StreamAnalysis] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_to_s = {pool.submit(analyze_stream, s, timeout): s for s in streams}
        for fut in concurrent.futures.as_completed(fut_to_s):
            results.append(fut.result())
    results.sort(key=lambda r: r.index)
    return results

# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------

def build_report(results: list[CheckResult], total_time: float) -> str:
    total = len(results)
    ok = sum(1 for r in results if r.http_ok)
    tcp = sum(1 for r in results if r.reachable)
    rt_list = [r.response_time for r in results if r.response_time > 0]
    rt_sorted = sorted(rt_list)

    lines = [
        "# IPTV 播放列表检测报告",
        "",
        f"**检测时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"**总流数**: {total}",
        f"**可用**: {ok} | **无数据**: {tcp - ok} | **连接失败**: {total - tcp}",
        f"**检测耗时**: {total_time:.1f}s",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 平均 DNS | {sum(r.dns_time for r in results if r.dns_time)/max(tcp,1)*1000:.0f}ms |",
        f"| 平均连接 | {sum(r.connect_time for r in results if r.connect_time)/max(tcp,1)*1000:.0f}ms |",
        f"| 平均响应 | {sum(rt_list)/max(len(rt_list),1)*1000:.0f}ms |",
        f"| 响应 P50 | {rt_sorted[len(rt_sorted)//2]*1000:.0f}ms |" if rt_sorted else "| 响应 P50 | - |",
        f"| 响应 P90 | {rt_sorted[int(len(rt_sorted)*0.9)]*1000:.0f}ms |" if rt_sorted else "| 响应 P90 | - |",
        "",
        "## 逐条详情",
        "",
        "| # | 频道 | 状态 | 协议 | DNS | 连接 | 响应 | 备注 |",
        "|---|------|------|------|-----|------|------|------|",
    ]

    for r in results:
        if r.http_ok:
            status = "✅"
        elif r.reachable:
            status = "🟡"
        else:
            status = "❌"
        proto = r.content_type or (f'HTTP {r.status_code}' if r.status_code else '-')
        lines.append(
            f"| {r.stream.index} | {r.stream.name} | {status} "
            f"| {proto} "
            f"| {f'{r.dns_time*1000:.0f}ms' if r.dns_time else '-'} "
            f"| {f'{r.connect_time*1000:.0f}ms' if r.connect_time else '-'} "
            f"| {f'{r.response_time*1000:.0f}ms' if r.response_time else '-'} "
            f"| {r.error or '-'} |"
        )

    lines.append("")

    # 4K 测速汇总
    fourk_results = [r for r in results if '4K' in r.stream.name and r.throughput_mbps > 0]
    if fourk_results:
        lines.append("## 4K 频道测速\n")
        lines.append("| # | 频道 | 吞吐量 (Mbps) | 评估 |")
        lines.append("|---|------|---------------|------|")
        for r in fourk_results:
            mbps = r.throughput_mbps
            if mbps >= 25:
                rating = "✅ 流畅"
            elif mbps >= 15:
                rating = "⚠️ 可能卡顿"
            else:
                rating = "❌ 会卡顿"
            lines.append(f"| {r.stream.index} | {r.stream.name} | {mbps} | {rating} |")
        lines.append("")

    return '\n'.join(lines)

# ---------------------------------------------------------------------------
# 更新合并逻辑
# ---------------------------------------------------------------------------

def merge_playlist(user_entries: list[ChannelEntry], repo_entries: list[ChannelEntry], metadata_only: bool = False) -> tuple[list[ChannelEntry], list[str]]:
    """合并用户列表和仓库列表，返回新列表和变更日志。
    规则：保留用户 proxy IP，只替换多播地址。
    metadata_only=True 时跳过多播替换（规则1、2），仅更新元数据（规则3）。
    """
    repo_by_tvg: dict[str, ChannelEntry] = {}
    repo_by_display: dict[str, ChannelEntry] = {}
    for e in repo_entries:
        if e.tvg_name:
            repo_by_tvg[e.tvg_name] = e
        if e.display_name:
            repo_by_display[e.display_name] = e

    # 构建 4K 升级映射: 仓库有 "山东卫视" + "山东卫视4K" → 可升级
    repo_4k_map: dict[str, str] = {}
    for name in repo_by_tvg:
        if name.endswith('4K') and len(name) > 2:
            base = name[:-2]
            if base in repo_by_tvg:
                repo_4k_map[base] = name

    USER_PROXY = os.environ['PROXY_URL']

    def _pick_display_name(user_name: str, repo_name: str) -> str:
        if '4K' in user_name and '4K' not in repo_name:
            return user_name
        return repo_name or user_name

    def _keep_proxy(repo_url: str) -> str:
        """保留用户 proxy，只取仓库的多播地址部分。"""
        m = re.search(r'/udp/\d+\.\d+\.\d+\.\d+:\d+', repo_url)
        if m:
            return USER_PROXY + m.group(0)
        return repo_url

    new_entries: list[ChannelEntry] = []
    changelog: list[str] = []

    def _repo_lookup(key: str) -> ChannelEntry | None:
        return repo_by_tvg.get(key) or repo_by_display.get(key)

    for ue in user_entries:
        tvg = ue.tvg_name
        match_key = ue.display_name
        changed = False

        # 规则1: 4K 升级（用户是非 4K，仓库有同名 4K）
        if not metadata_only and tvg and not tvg.endswith('4K') and tvg in repo_4k_map:
            fourk_tvg = repo_4k_map[tvg]
            repo_e = repo_by_tvg[fourk_tvg]
            new_url = _keep_proxy(repo_e.url) if repo_e.url else ue.url
            new_entries.append(ChannelEntry(
                tvg_name=fourk_tvg,
                display_name=_pick_display_name(ue.display_name, repo_e.display_name) or (ue.display_name + '4K'),
                url=new_url,
                logo=repo_e.logo or ue.logo,
                group=repo_e.group or ue.group,
                tvg_id=repo_e.tvg_id or ue.tvg_id,
                catchup=repo_e.catchup or ue.catchup,
                catchup_source=repo_e.catchup_source or ue.catchup_source,
                catchup_days=repo_e.catchup_days or ue.catchup_days,
                extinf='',
                index=ue.index,
            ))
            changelog.append(f'4K升级: "{ue.display_name}" → "{repo_e.display_name}" (多播: {new_url})')
            continue

        # 规则2: 同频道多播地址不同 → 更新多播地址
        repo_e = _repo_lookup(tvg or '') or _repo_lookup(match_key)
        if not metadata_only and repo_e:
            u_mcast = re.search(r'/udp/\d+\.\d+\.\d+\.\d+:\d+', ue.url)
            r_mcast = re.search(r'/udp/\d+\.\d+\.\d+\.\d+:\d+', repo_e.url)
            if u_mcast and r_mcast and u_mcast.group(0) != r_mcast.group(0):
                new_url = _keep_proxy(repo_e.url)
                new_entries.append(ChannelEntry(
                    tvg_name=repo_e.tvg_name or tvg,
                    display_name=_pick_display_name(ue.display_name, repo_e.display_name),
                    url=new_url,
                    logo=repo_e.logo or ue.logo,
                    group=repo_e.group or ue.group,
                    tvg_id=repo_e.tvg_id or ue.tvg_id,
                    catchup=repo_e.catchup or ue.catchup,
                    catchup_source=repo_e.catchup_source or ue.catchup_source,
                    catchup_days=repo_e.catchup_days or ue.catchup_days,
                    extinf='',
                    index=ue.index,
                ))
                changelog.append(f'更新: "{ue.display_name}" 多播 {u_mcast.group(0)} → {r_mcast.group(0)}')
                continue

        # 规则3: 补充/更新元数据（logo、group、tvg_id、tvg_name）
        if repo_e:
            new_tvg_name = repo_e.tvg_name or tvg
            new_logo = repo_e.logo or ue.logo
            new_group = repo_e.group or ue.group
            new_tvg_id = repo_e.tvg_id or ue.tvg_id
            new_catchup = repo_e.catchup or ue.catchup
            new_catchup_source = repo_e.catchup_source or ue.catchup_source
            new_catchup_days = repo_e.catchup_days or ue.catchup_days
            if (new_tvg_name != tvg or new_logo != ue.logo or new_group != ue.group
                or new_tvg_id != ue.tvg_id
                or new_catchup != ue.catchup or new_catchup_source != ue.catchup_source
                or new_catchup_days != ue.catchup_days):
                new_entries.append(ChannelEntry(
                    tvg_name=new_tvg_name,
                    display_name=_pick_display_name(ue.display_name, repo_e.display_name),
                    url=ue.url,
                    logo=new_logo,
                    group=new_group,
                    tvg_id=new_tvg_id,
                    catchup=repo_e.catchup or ue.catchup,
                    catchup_source=repo_e.catchup_source or ue.catchup_source,
                    catchup_days=repo_e.catchup_days or ue.catchup_days,
                    extinf='',
                    index=ue.index,
                ))
                meta_changes = []
                if new_logo != ue.logo: meta_changes.append('logo')
                if new_group != ue.group: meta_changes.append('分组')
                if new_tvg_id != ue.tvg_id: meta_changes.append('tvg-id')
                if new_catchup != ue.catchup or new_catchup_source != ue.catchup_source or new_catchup_days != ue.catchup_days:
                    meta_changes.append('回放')
                changelog.append(f'元数据: "{ue.display_name}" 更新 {", ".join(meta_changes)}')
                continue

        # 无变更
        new_entries.append(ue)

    # 无替换提示（仓库无匹配的频道）
    def _in_repo(name: str) -> bool:
        return name in repo_by_tvg or name in repo_by_display
    no_match = [ue for ue in user_entries
                if ue.tvg_name
                and not _in_repo(ue.tvg_name)
                and not _in_repo(ue.display_name)
                and not (ue.tvg_name.endswith('4K') and ue.tvg_name[:-2] in repo_by_tvg)]
    for ue in no_match:
        changelog.append(f'无替换: "{ue.display_name}" (tvg: {ue.tvg_name}) 仓库无此频道')

    return new_entries, changelog

# ---------------------------------------------------------------------------
# 配置管理 & GitHub Push
# ---------------------------------------------------------------------------

CONFIG_DIR = os.path.expanduser("~/.config/iptv-check")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

def load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_config(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.chmod(CONFIG_FILE, 0o600)

RE_RAW_GITHUB = re.compile(
    r'https?://raw\.githubusercontent\.com/([^/]+/[^/]+)/(?:refs/heads/)?([^/]+)/(.+)'
)

def parse_github_raw_url(url: str) -> tuple[str, str, str] | None:
    m = RE_RAW_GITHUB.match(url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None

RE_GITHUB_REF = re.compile(
    r'https://raw\.githubusercontent\.com/([^/]+/[^/]+)/refs/heads/[^/]+/(.+)'
)

def get_previous_version_url(url: str) -> str | None:
    """通过 GitHub API 获取上一次提交的文件 raw URL。"""
    m = RE_GITHUB_REF.match(url)
    if not m:
        return None
    repo = m.group(1)
    path = m.group(2)
    api_url = f"https://api.github.com/repos/{repo}/commits?path={path}&per_page=2"
    req = urllib.request.Request(api_url, headers={"User-Agent": "iptv-check/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if len(data) > 1:
                prev_sha = data[1]['sha']
                return f"https://raw.githubusercontent.com/{repo}/{prev_sha}/{path}"
    except Exception:
        pass
    return None

def push_to_github_api(token: str, repo: str, path: str, content: str,
                        message: str, branch: str = "main") -> bool:
    import http.client
    import json

    encoded_path = urllib.parse.quote(path, safe='/')

    def _req(method: str, url_path: str,
             req_body: bytes | None = None) -> tuple[int, bytes]:
        """Raw HTTP request via low-level putheader (bytes bypass latin-1)."""
        conn = http.client.HTTPSConnection("api.github.com", timeout=10)
        conn.putrequest(method, url_path)
        conn.putheader(b"Host", b"api.github.com")
        conn.putheader(b"Authorization", f"Bearer {token}".encode("utf-8"))
        conn.putheader(b"Accept", b"application/vnd.github.v3+json")
        conn.putheader(b"User-Agent", b"iptv-check/1.0")
        if req_body is not None:
            conn.putheader(b"Content-Type", b"application/json")
            conn.putheader(b"Content-Length", str(len(req_body)).encode("latin-1"))
        conn.endheaders()
        if req_body is not None:
            conn.send(req_body)
        resp = conn.getresponse()
        return resp.status, resp.read()

    # GET 获取 SHA
    get_path = f"/repos/{repo}/contents/{encoded_path}"
    status, body = _req("GET", get_path)
    sha = ""
    if status == 200:
        data = json.loads(body)
        sha = data.get("sha", "")
    elif status != 404:
        print(f"GitHub API 错误: {status}", file=sys.stderr)
        return False

    # PUT 推送新文件
    payload_bytes = json.dumps({
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "sha": sha,
        "branch": branch,
    }).encode("utf-8")

    put_path = f"/repos/{repo}/contents/{encoded_path}"
    status, body = _req("PUT", put_path, payload_bytes)
    if status in (200, 201):
        result = json.loads(body)
        print(f"✅ 已推送到 GitHub: {result['content']['html_url']}")
        return True
    else:
        print(f"❌ 推送失败 ({status}): {body.decode(errors='replace')}", file=sys.stderr)
        return False

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IPTV M3U 播放列表检测 + 更新替换",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('url', nargs='?', help='M3U 播放列表 URL（不填则交互模式）')
    p.add_argument('--update', metavar='REPO_URL', help='更新模式：指定仓库 M3U URL，自动合并替换')
    p.add_argument('--concurrent', type=int, default=10, help='并发检测数 (默认 10)')
    p.add_argument('--timeout', type=int, default=10, help='单条流超时秒数 (默认 10)')
    p.add_argument('--output', '-o', help='输出报告/新列表到文件')
    p.add_argument('--json', action='store_true', help='以 JSON 格式输出')
    p.add_argument('--push', action='store_true', help='更新后自动推送到 GitHub 仓库')
    p.add_argument('--set-token', action='store_true', help='交互式设置 GitHub Token（安全，不经过 shell history）')
    p.add_argument('--detect-only', action='store_true', help='仅检测，不执行更新合并')
    p.add_argument('--metadata-only', action='store_true', help='仅更新元数据（logo、tvg-id、分组），保留原始多播地址')
    p.add_argument('--analyze', action='store_true', help='TS 流深度分析：检测分辨率、编码、服务名')
    return p.parse_args(argv)

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 设置 token 模式（交互式，不经过 CLI 参数）
    if args.set_token:
        print("输入 GitHub Personal Access Token（输入时不显示字符）")
        token = getpass.getpass("Token: ").strip()
        while not token:
            token = getpass.getpass("Token 不能为空: ").strip()
        cfg = load_config()
        cfg['github_token'] = token
        save_config(cfg)
        print("✅ GitHub Token 已安全保存到 ~/.config/iptv-check/config.json")
        return 0

    cfg = load_config()
    saved_m3u = cfg.get('m3u_url', '')
    saved_repo = cfg.get('repo_url', '')

    # 首次运行：无 CLI 参数、无保存 URL、无 --update → 交互式配置
    if args.url is None and not saved_m3u and not args.update:
        print("=" * 50)
        print("  首次运行 - 请输入配置信息")
        print("=" * 50)
        m3u = input("\nM3U 播放列表链接\n> ").strip()
        while not m3u:
            m3u = input("链接不能为空\n> ").strip()
        repo = input("\n仓库 M3U 链接（用于更新替换，可跳过）\n> ").strip()
        cfg['m3u_url'] = m3u
        if repo:
            cfg['repo_url'] = repo
        save_config(cfg)
        url_str = m3u
        update_repo = repo
    elif args.url:
        url_str = args.url
        update_repo = args.update or saved_repo
    else:
        url_str = saved_m3u
        update_repo = args.update or saved_repo

    # 元数据模式：自动回退到上一次提交的版本（恢复原始多播地址）
    user_url = resolve_url(url_str)
    parsed_push = parse_github_raw_url(user_url)  # 保留原始分支信息用于推送
    if args.metadata_only:
        prev_url = get_previous_version_url(user_url)
        if prev_url:
            print(f"↩ 回退到上一次提交版本")
            user_url = prev_url
        else:
            print("⚠ 无法获取上一次提交版本，使用当前版本")

    # 下载用户列表
    print(f"\n获取 M3U 列表: {user_url}")
    try:
        user_text = fetch(user_url, timeout=args.timeout)
    except Exception as e:
        print(f"M3U 下载失败: {e}", file=sys.stderr)
        return 1

    user_entries = parse_m3u_full(user_text)
    if not user_entries:
        print("未检测到流条目", file=sys.stderr)
        return 1
    print(f"用户列表: {len(user_entries)} 条流")

    # ---- 检测 ----
    print(f"检测 {len(user_entries)} 条流...")
    t0 = time.monotonic()
    results = run_checks(user_entries, args.concurrent, args.timeout)
    elapsed = time.monotonic() - t0
    report = build_report(results, elapsed)
    print("\n" + report)

    # ---- TS 深度分析 ----
    analysis_results: list[StreamAnalysis] = []
    if args.analyze:
        print(f"\n深度分析 {len(user_entries)} 条流...")
        t0 = time.monotonic()
        analysis_results = run_analysis(user_entries, min(args.concurrent, 5), args.timeout)
        a_elapsed = time.monotonic() - t0
        valid_ts = sum(1 for a in analysis_results if a.valid_ts)
        print(f"分析完成: {valid_ts}/{len(analysis_results)} 有效TS流 ({a_elapsed:.1f}s)")

        # 添加分析结果到报告中
        report += "\n## TS 流深度分析\n\n"
        report += "| # | 频道 | TS有效 | 编码 | 分辨率 | 服务名 | 码率(Kbps) |\n"
        report += "|---|------|--------|------|--------|--------|------------|\n"
        for a in analysis_results:
            ts_ok = "✅" if a.valid_ts else "❌"
            res = f"{a.width}x{a.height}" if a.width else "-"
            sn = a.service_name or "-"
            kbps = str(a.estimated_kbps) if a.estimated_kbps else "-"
            report += f"| {a.index} | {a.name} | {ts_ok} | {a.video_codec or '-'} | {res} | {sn} | {kbps} |\n"

    # JSON 输出
    if args.json:
        class SetEncoder(json.JSONEncoder):
            def default(self, obj):
                if dataclasses.is_dataclass(obj):
                    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
                return super().default(obj)
        output = json.dumps(results, ensure_ascii=False, indent=2, cls=SetEncoder)
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output)
            print(f"JSON 已保存: {args.output}")
        else:
            print(output)
        return 0

    # 保存检测报告
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"报告已保存: {args.output}")

    # 纯检测模式
    if args.detect_only:
        return 0

    # ---- 更新合并 ----
    if not update_repo:
        print("\n⚠️ 未配置仓库链接，跳过更新")
        print("   下次运行可用 --update <仓库URL> 或首次运行配置")
        return 0

    repo_url = resolve_url(update_repo)
    print(f"\n获取仓库列表: {repo_url}")
    try:
        repo_text = fetch(repo_url, timeout=args.timeout)
    except Exception as e:
        print(f"仓库 M3U 下载失败: {e}", file=sys.stderr)
        return 1

    repo_entries = parse_m3u_full(repo_text)
    print(f"仓库列表: {len(repo_entries)} 条流")

    print("\n合并列表...")
    new_entries, changelog = merge_playlist(user_entries, repo_entries, metadata_only=args.metadata_only)
    for i, e in enumerate(new_entries):
        e.index = i

    # 解析 RTSP 重定向（仅 catchup-source 为 RTSP 的频道）
    proxy_url = os.environ.get('PROXY_URL', '')  # 通过 HTTP CONNECT 隧道直达内网 RTSP
    resolve_entries = [e for e in new_entries if e.catchup_source and e.catchup_source.startswith('rtsp://')]
    if resolve_entries:
        print(f"\n解析 RTSP 重定向 ({len(resolve_entries)} 条)...")
        resolved_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.concurrent, 10)) as pool:
            def _resolve(e: ChannelEntry) -> bool:
                original = e.catchup_source
                resolved = resolve_rtsp_redirect(original, proxy_url=proxy_url)
                if resolved != original:
                    e.catchup_source = resolved
                    return True
                return False
            fut_to_e = {pool.submit(_resolve, e): e for e in resolve_entries}
            for fut in concurrent.futures.as_completed(fut_to_e):
                if fut.result():
                    resolved_count += 1
        if resolved_count:
            print(f"  ✅ {resolved_count} 条已解析为直达 URL")
        else:
            print(f"  ⚠️ 无法解析（RTSP 可能不可达），保留原始 URL")

    new_m3u = generate_m3u(new_entries)

    # 输出变更日志
    print(f"\n{'=' * 50}")
    print(f"  更新完成: {len(changelog)} 项变更")
    print(f"{'=' * 50}")
    for line in changelog:
        print(f"  • {line}")

    # 保存更新后的 M3U
    m3u_path = ""
    if args.output:
        m3u_path = args.output.rsplit('.', 1)[0] + '_updated.m3u'
    else:
        m3u_path = f"/tmp/iptv_update_{time.strftime('%Y%m%d_%H%M%S')}_updated.m3u"
    with open(m3u_path, 'w', encoding='utf-8') as f:
        f.write(new_m3u)
    print(f"\n新列表已保存: {m3u_path}")

    # 自动推送（有 token 且能解析仓库时自动执行）
    push_token = os.environ.get('GITHUB_TOKEN', '') or cfg.get('github_token', '')
    do_push = args.push or bool(push_token and parsed_push)

    if do_push:
        if not push_token:
            push_token = os.environ.get('GITHUB_TOKEN', '') or cfg.get('github_token', '')
        if not push_token:
            print("❌ 未配置 GitHub Token，运行 --set-token 或设置 GITHUB_TOKEN 环境变量", file=sys.stderr)
            return 1
        if not parsed_push:
            parsed_push = parse_github_raw_url(user_url)
        if not parsed_push:
            print("❌ 无法解析 GitHub 仓库路径", file=sys.stderr)
            return 1
        _repo, branch, path = parsed_push
        print(f"\n推送至 {_repo}/{path} ({branch})...")
        push_to_github_api(
            push_token, _repo, path, new_m3u,
            f"chore: update IPTV playlist ({time.strftime('%Y-%m-%d')})",
            branch,
        )

    return 0

if __name__ == '__main__':
    sys.exit(main())
