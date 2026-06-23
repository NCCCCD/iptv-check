<!-- 本项目是独立仓库/独立规则项目。 -->

# AGENTS.md｜IPTV播放列表检测 (iptv-check)

## 1. 项目信息
- 项目名称：IPTV播放列表检测
- 项目代号：iptv-check
- 项目类型: cli + Docker daemon
- 当前项目状态：**已投产，稳定运行中**
- 当前轮次：第二轮
- 当前优先级：高

## 2. 项目目标
- 核心目标：从用户 M3U + 仓库 M3U 自动合并更新，检测流可用性，推送至 GitHub → Watchtower 自动部署
- 目标用户：本人（维护自用 IPTV 列表）
- 成功标准：每天 3:00 自动合并 → 检测 → 推送，无需人工介入

## 3. 当前架构

```
光猫 IPTV 口 → OpenWrt eth1 (10.187.230.83/20)
                   ↓ 静态路由 182.139.0.0/16 via 10.187.224.1
                   ↓ rtp2httpd (10.10.2.6:5140) → HTTP 代理直播/回看
                   ↓ eth0 (10.10.2.6/24)
              UDM SE VLAN2
              ├── Mac (APTV) ← http://10.10.2.6:5140/playlist.m3u
              └── CasaOS 服务器 (10.10.2.8)
                   └── Docker: iptv-check (每天 3:00 crond 更新推送)
                        └── GitHub NCCCCD/HMvemi → M3U 存储
```

## 4. 核心文件
| 文件 | 路径 | 说明 |
|------|------|------|
| 主脚本 | `iptv-check.py` | 全功能脚本（检测/合并/RTSP解析/推送） |
| Dockerfile | `Dockerfile` | Python 3.11-alpine + busybox crond |
| 入口 | `entrypoint.sh` | 立即执行 + crond 定时 + tail 日志 |
| 工作流 | `.github/workflows/docker-publish.yml` | GitHub Actions CI/CD |
| 部署配置 | `/opt/stacks/iptv-check/compose.yaml` | 服务器 Dockge 部署 |
| 运行配置 | `/opt/stacks/iptv-check/config/config.json` | m3u_url / repo_url / github_token |
| OpenWrt rtp2httpd | `/etc/config/rtp2httpd` | upstream-interface eth1, external-m3u → GitHub |

## 5. 已完成功能

### 核心
- M3U M3U8 解析（频道名、URL、分组、Logo、tvg-id、catchup 全套标签）
- 并发流检测（ThreadPoolExecutor，可配置 concurrent/timeout）
- TS 流深度分析（PAT/PMT/SDT/SPS，检测分辨率、编码、服务名）
- RTSP 302 重定向追踪解析（resolve_rtsp_redirect）
- Markdown / JSON 报告

### 合并更新（merge_playlist）
- **规则1**：4K 升级（用户非4K → 仓库同名4K）
- **规则2**：多播地址更新（同频道不同多播地址）
- **规则3**：元数据更新（logo/分组/tvg-id/catchup）
- **模糊名匹配**：去 "高清" 后缀、去 "-"、去 "HD"，匹配仓库
- **4K 回放继承**：4K 频道无回放 → 从同名 HD 版继承 catchup-source
- **孤儿频道移除**：仓库无匹配且无回放 → 自动删除
- **全局端口替换**：merge 后所有 URL 刷新为当前 PROXY_URL 端口

### 回放 / catchup
- catchup / catchup-source / catchup-days 完整解析与输出
- `{utc:YmdHMS}` → `${(b)yyyyMMddHHmmss}` 占位符转换（北京时间）
- `{utcend:YmdHMS}` → `${(e)yyyyMMddHHmmss}` 占位符转换
- RTSP 302 重定向解析写入最终直达 URL
- rtp2httpd 自动重写 catchup-source：RTSP → HTTP

### 部署与运维
- Docker 容器化（Alpine + Python 3.11 + busybox crond）
- entrypoint.sh：支持 run / shell / 定时（默认）三种模式
- 定时任务（默认每天 3:00），启动时立即执行一次
- GitHub Actions 自动构建并推送 ghcr.io
- Watchtower 自动检测新镜像并重启容器
- config.json 持久化配置（m3u_url / repo_url / github_token）
- bytes header 推送上 GitHub（解决 UnicodeEncodeError）

## 6. 关键决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 占位符格式 | `${(b)yyyyMMddHHmmss}` | APTV 发北京时，服务端按北京时，消除 8h 偏移 |
| 回放源 | 脚本解析 RTSP 302 直达 | rtp2httpd 不自带 302 跟随 |
| 代理 | rtp2httpd 单代理 | 废弃 udpxy，直播/回看统一 5140 端口 |
| PROXY_URL 传递 | `docker exec -e` | 前缀语法在 docker exec 中不生效 |
| 并发检测 | `--concurrent 3` | rtp2httpd 默认 maxclients=5 |

## 7. 网络拓扑要点

```
光猫 IPTV: 10.187.224.0/20 (eth1)
OpenWrt eth1: 10.187.230.83/20
OpenWrt eth0: 10.10.2.6/24
CasaOS: 10.10.2.8
Mac: 10.10.2.x (UDM SE DHCP)
```

**路由需求（OpenWrt iStoreOS）：**
- `182.139.0.0/16 via 10.187.224.1` → RTSP 直达
- `118.123.185.42/32 via 10.187.224.1` → CCTV1 等 RTSP 直达
- **iStoreOS 重启会清除这些路由**，必须在 `/etc/rc.local` 持久化

## 8. 反脆弱沉淀

| 问题 | 症状 | 根因 | 修复 |
|------|------|------|------|
| 全频道回放挂 | rtp2httpd 503，APTV 有按钮但播不了 | iStoreOS 重启丢路由 | `route add -net 182.139.0.0 netmask 255.255.0.0 gw 10.187.224.1` + rc.local 持久化 |
| 部分频道无回放 | 回放按钮消失 | 仓库 M3U 无该频道 catchup-source | 4K→HD 继承 / 模糊名匹配 / 查 merge 日志 |
| 推送失败 SSL | `SSL: UNEXPECTED_EOF_WHILE_READING` | 容器内 GitHub API 网络不稳定 | 重试即可，不影响已生成内容 |
| Alexander Sofronov IPTV | 有加载动画但回退直播 | app 不支持 RTSP `playseek=` catchup | 换用支持 RTSP catchup 的 app（TiviMate / mytv-android） |

## 9. 诊断速查

```bash
# 全频道回放挂 → 查 OpenWrt 路由
ssh root@10.10.2.6 "route -n | grep 182"

# 部分频道无回放 → 查 merge 日志
docker exec iptv-check cat /var/log/iptv-check.log | grep -E "回放|4K|继承"

# 手动触发更新
docker exec -e PROXY_URL=http://10.10.2.6:5140 iptv-check \
  python3 /app/iptv-check.py --concurrent 3 \
  --update "https://raw.githubusercontent.com/suzukua/iptv-cd-telecom/master/home/udpxy_iptv.m3u8" \
  --push

# 覆盖新脚本到容器（更新前测试）
docker cp iptv-check.py iptv-check:/app/iptv-check.py

# 看 rtp2httpd 实际 M3U
curl -s http://10.10.2.6:5140/playlist.m3u | grep "频道名" -A1
```

## 10. 质量升级规则
- 首轮提交门槛：新需求先明确可交付目标、关键风险、验证方式
- 三档方案机制：方向未锁死时，比较"保守/标准/突破"三档，只输出推荐方案和关键取舍
- 真实路径回放：交付前按真实用户路径完整跑一遍
- 阶段决策门：需求不清不设计，方案不稳不开发，未自测不交付
- 创造性要求：每轮至少主动补 1 个有效建议
- 反脆弱沉淀：老板指出的问题必须判断是否可迁移，通用问题要沉淀为项目规则
