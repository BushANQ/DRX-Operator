# DRX-Operator

自主红队渗透测试专家系统 —— Agent-First 架构，LLM 驱动的自主安全测试平台。

[Python 3.10+] [Alpha]

**Author**: [BushSEC](https://github.com/BushANQ) · [bushsec.cn](https://bushsec.cn)

![preview](JPG/1.jpg)
---

DRX-Operator 是一个 Agent-First 架构的自主渗透测试系统。与传统安全工具不同，DRX-Operator
的核心是一个 LLM 驱动的 **Master Agent**，它通过 ReAct（推理-行动）循环自主决策、
调用工具链、分析结果，持续执行安全测试任务。终端界面（TUI）只是薄展示壳，
Agent 是系统的一等公民 —— 所有操作都是 LLM 工具调用。

系统内置 Python/Bash 沙箱、持久化 Shell 会话管理、OOB 回调监听、MCP 协议扩展、
声明式权限引擎、5 级安全门控、证据链驱动的漏洞发现模型，以及 7 层上下文压缩管线，
支撑超长红队会话。

**本工具仅限授权安全测试使用。未经授权访问他人系统属于违法行为。使用前请确保已获得
目标系统的书面授权。**

---

## 目录

- [核心特性](#核心特性)
- [安装与环境配置](#安装与环境配置)
- [快速开始](#快速开始)
- [架构概览](#架构概览)
- [工具参考](#工具参考)
- [路线图](#路线图)
- [免责声明](#免责声明)

---

## 核心特性

**自主 ReAct 决策循环** —— Plan、Think、Act、Observe、Reflect 五阶段自主推理，
Agent 根据工具返回的实时数据持续调整策略，无需人工干预。

**30+ 内置工具** —— HTTP 抓取、Bash/Python 沙箱执行、持久 Shell 会话（SSH/反弹
Shell）、OOB 回调监听器、NVD CVE 查询、Web 搜索、文件读写与精确编辑、结构化解析
（nmap XML、原始 HTTP）、字典管理。

**证据链驱动的漏洞发现** —— 每个 Finding 携带 Evidence 数组、confidence 评分、
verified 字段和 CVE 关联。结论必须基于工具返回的具体数据，严禁幻觉。

**5 级安全门控** —— L0（侦察，自动批准）到 L4（破坏性操作，需确认短语），配合
声明式 PermissionEngine（基于 glob 的 allow/ask/deny 规则，首匹配即生效）。

**MCP 协议支持** —— 通过 Model Context Protocol 接入外部工具服务器。MCP 工具的
schema 自动注入 LLM 工具列表，通过 `mcp__<server>__<tool>` 格式调用。

**优先级任务调度与并行派发** —— Exploit > Recon > Lateral > Persist > Report
五级优先级队列，支持按目标并发限制和全局 QPS 控制。SubAgent 拥有独立消息历史
和 ReAct 循环，可并行执行子任务。

**会话持久化** —— SQLite 元数据 + JSON 文件存储，支持完整会话保存/恢复（知识库、
消息历史、todo 列表、操作模式、Token 用量统计）。

**LLM 韧性层** —— 指数退避重试 + Provider 回退链。429/5xx/连接错误自动重试；
重试耗尽后自动切换下一个 Provider，UI 实时显示切换状态。

**7 层上下文压缩管线** —— 从 L1（大结果自动存档）到 L7（跨 Agent 产物共享），
支撑单次会话数万轮交互而不会超出模型上下文窗口。

**终端界面（Textual TUI）** —— 聊天区、侧栏（任务看板 + 子 Agent 状态）、输入框、
状态栏四区布局，Ctrl+S 中断当前任务。

---

## 安装与环境配置

### 前置依赖

- Python 3.10 或更高版本
- 可选系统工具（Agent 会根据需要调用）：`nmap`、`curl`、`git`、`ssh`、`openssl` 等

### 源码安装

```bash
git clone https://github.com/BushANQ/DRX-Operator.git
cd DRX-Operator
pip install -r requirements.txt
```

主要 Python 依赖：`textual`（TUI 框架）、`ddgs`（Web 搜索）、`requests`/`urllib3`
（HTTP 请求）、`anthropic`/`openai`（LLM Provider）、`pyyaml`（技能配置解析）。

### 配置 LLM

**第一步（必需）：通过环境变量配置 API 密钥。** 仓库内的
`configs/default_config.json` 是干净模板（`api_key` 为空），**不要把密钥写进任何
会被提交的文件**。推荐做法：

```bash
cp .env.example .env       
# 编辑 .env，填入你的密钥，例如：
#   DRX_LLM_API_KEY=sk-xxxxxxxx
set -a && source .env && set +a    
```

密钥读取优先级：`DRX_LLM_API_KEY` > Provider 专用环境变量（见下表）>
配置文件中的 `api_key` 字段。

**第二步（可选）：调整 Provider / 模型。** 编辑 `configs/default_config.json` 中的
`llm` 段（只改非敏感项，如 provider、model、base_url、temperature）：

```json
{
  "llm": {
    "enabled": true,
    "provider": "openai_compatible",
    "model": "deepseek-chat",
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "temperature": 0.7,
    "max_tokens": 4096,
    "context_window": 65536,
    "retry": {
      "max_retries": 3,
      "base_delay": 1.0,
      "max_delay": 30.0
    },
    "fallback": []
  }
}
```

支持的 Provider 类型：

| provider 值 | 对应 Provider | 环境变量 |
|---|---|---|
| `anthropic` 或 `claude` | AnthropicProvider | `ANTHROPIC_API_KEY` |
| `openai` | OpenAIProvider | `OPENAI_API_KEY` |
| `openai_compatible`（默认）| DeepSeekProvider | `DEEPSEEK_API_KEY` |


**Fallback 链**（可选）：当主 Provider 不可用时自动切换：

```json
{
  "fallback": [
    {
      "provider": "openai",
      "model": "gpt-4o",
      "api_key": "",
      "base_url": "https://api.openai.com/v1"
    }
  ]
}
```

### 配置 MCP 服务器（可选）

在 `configs/default_config.json` 的 `mcp.servers` 中添加 MCP 服务器定义：

```json
{
  "mcp": {
    "servers": {
      "filesystem": {
        "enabled": true,
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "timeout": 30.0
      }
    }
  }
}
```

DRX-Operator 启动时会自动连接所有 `enabled: true` 的 MCP 服务器，并将其工具以
`mcp__<server_name>__<tool_name>` 的命名格式注入 LLM 工具列表。

### 启动

```bash
python -m drx_agent.main
```

---

## 快速开始

### 界面布局

启动后进入 Textual TUI，界面分为四个区域：

- **聊天区**（左侧主区域）：显示 Agent 的思考过程、工具调用卡片和结果
- **侧栏**（右侧）：上方显示 todo 任务列表，下方显示活跃的子 Agent 状态
- **输入框**（底部）：输入自然语言指令或斜杠命令
- **状态栏**（最底部）：显示当前状态信息

### 基本交互

直接在输入框中输入自然语言指令，Agent 会自主分解任务、调用工具、分析结果并汇报。
例如：

```
扫描 192.168.1.0/24 网段的 Web 服务
```

```
对 target.example.com 进行端口扫描和服务识别
```

```
检查 http://test.example.com 是否存在 SQL 注入
```

```
把本次会话的发现整理成报告
```

### 斜杠命令

| 命令 | 说明 |
|---|---|
| `/scan <target>` | 启动侦察扫描 |
| `/exploit <target>` | 启动漏洞利用 |
| `/target <host>` | 管理目标主机信息 |
| `/status` | 查看当前系统状态 |
| `/plan` | 切换到 plan 模式（仅允许只读工具） |
| `/act` | 切换到 act 模式（允许全部工具） |
| `/mode` | 查看当前模式 |
| `/stop` 或 `/cancel` 或 `/interrupt` | 中断当前任务 |
| `/dream` | 触发深度上下文压缩（L6 层） |
| `/context` | 查看上下文使用量 |
| `/progress` | 查看进度文档（9 段结构） |
| `/memory` | 查看项目记忆（DRX.md/AGENTS.md/CLAUDE.md） |
| `/memory reload` | 重新加载项目记忆文件 |

### Plan 模式与 Act 模式

为降低风险，Agent 支持两种运行模式：

- **plan 模式**：仅允许只读工具（`read_file`、`grep`、`web_search`、`cve_lookup`、
  `http_fetch`、`parse_nmap`、`parse_http`、`todo_write`、`shell_list`）。对
  write/exec/shell/dispatch 类调用会直接拒绝并提示。适合分析和规划阶段。
- **act 模式**（默认）：全部工具可用，Agent 可以执行扫描、利用、文件写入等操作。

使用 `/plan` 和 `/act` 命令切换。

### 会话管理

会话自动保存到 `sessions/` 目录。数据包括：

- 知识库（目标、发现、凭据）→ `sessions/<id>/kb.json`
- 消息历史 → `sessions/<id>/messages.json`
- 元数据（todo、模式、用量统计）→ `sessions/sessions.db`（SQLite）

在聊天中输入 "恢复会话" 或 "restore session" 可恢复最近一次保存的会话。

### 项目记忆

在项目根目录或其父目录创建 `DRX.md`、`AGENTS.md` 或 `CLAUDE.md` 文件，其中的内容
会作为项目记忆注入到每次 LLM 调用的 System Prompt 中。适合写入测试范围、规则、
合规要求等持久化指令。使用 `/memory reload` 可在编辑后重新加载。

---

## 架构概览

### 整体分层

```
+------------------------------------------------------------------+
|                        TUI (Textual App)                          |
|  ChatPanel | Sidebar | Composer | StatusFooter                    |
|              薄展示壳 —— 所有操作通过 EventBus 通信                  |
+------------------------------------------------------------------+
                                | EventBus
+------------------------------------------------------------------+
|                     Master Agent (ReAct Loop)                     |
|  Plan -> Think -> Act -> Observe -> Reflect                       |
|  System Prompt 构建 | Tool Schema 管理 | Tool 执行路由              |
|  Context Compaction (L1-L7) | SubAgent Dispatch | Approval Flow   |
+------------------------------------------------------------------+
        |              |              |              |
+---------------+ +-----------+ +-----------+ +-----------+
| LLM Provider  | | Execution | | Safety    | | Knowledge |
| Abstraction   | | Engines   | | Layer     | | Base      |
+---------------+ +-----------+ +-----------+ +-----------+
| Anthropic     | | Python    | | SafetyGate| | Targets   |
| OpenAI        | | Sandbox   | | L0-L4     | | Findings  |
| DeepSeek      | | Bash      | | Permission| | Credential|
| Resilient     | | Sandbox   | | Engine    | | Vault     |
| (retry+fb)    | | Shell     | | Glob Rules| +-----------+
+---------------+ | Sessions  | +-----------+ | Artifact  |
                  | OOB       |               | Store     |
                  | Listener  |               | (disk)    |
                  +-----------+               +-----------+

+------------------------------------------------------------------+
|                     Extension & Persistence                       |
|  MCPManager | HookManager | SkillsRegistry | SessionStore(SQLite)|
+------------------------------------------------------------------+
```

### 子 Agent 派发机制

当任务可拆分时，Master Agent 通过 `task` 工具派发 SubAgent。每个 SubAgent：

1. 拥有独立的 `agent_id`（如 `recon-a1b2c3`）和独立消息历史
2. 共享父 Agent 的 tool_executor（同一套沙箱/Shell/KB）
3. 运行自己的 ReAct 循环（最大迭代次数 + TTL 限制）
4. 完成后通过 EventBus 发布 `SUB_AGENT_RESULT`，侧栏实时更新

子 Agent 无法递归调用 `task` 工具（防止失控递归）。

### 安全模型

双层安全设计：

**L0-L4 SafetyGate**（操作风险门控）：

| 级别 | 说明 | 行为 |
|---|---|---|
| L0 | 侦察类 | 自动批准 |
| L1 | 被动漏洞扫描 | 首次批准后会话级缓存 |
| L2 | 主动漏洞利用 | 需用户逐次 approve |
| L3 | 凭据/持久化攻击 | 需用户逐次 approve |
| L4 | 破坏性/不可逆操作 | 需输入确认短语 |

**PermissionEngine**（声明式工具规则）：

独立于 SafetyGate，通过有序规则列表匹配工具调用：

```json
{
  "tool": "execute_bash",
  "match": "*rm -rf /*",
  "action": "deny",
  "note": "destructive root delete"
}
```

决策：`allow`（直接放行）、`ask`（弹出审批请求）、`deny`（拒绝并返回原因）。首条
匹配规则生效。用户可通过回答 `always` 将该规则升级为会话级永久允许。

### LLM 韧性设计

`ResilientProvider` 包装一个或多个 Provider 实例：

1. 发送请求到主 Provider
2. 若失败且错误为瞬时类型（429/5xx/timeout/连接重置）—— 指数退避重试（最多 N 次）
3. 重试耗尽或非瞬时错误 —— 自动切换下一个 Fallback Provider
4. 若在流式传输中途失败且已产出可见内容（text/tool_call）—— 不重试，直接报错
   （防止重复输出）
5. UI 实时显示 "正在重试..." 和 "正在切换到 Provider X..." 的状态通知

---

## 工具参考

### 网络与信息收集

| 工具名 | 说明 |
|---|---|
| `http_fetch` | HTTP/HTTPS URL 抓取。支持 GET/POST/PUT/DELETE/HEAD/OPTIONS，自定义 headers 和 body |
| `web_search` | 搜索引擎查询，返回 title/url/snippet。后端：ddgs（优先）→ DuckDuckGo Instant Answer API（回退） |
| `cve_lookup` | 查询 NVD API 2.0，返回 CVE 描述/CVSS 评分/CWE/受影响产品/参考链接 |

### 代码与命令执行

| 工具名 | 说明 |
|---|---|
| `execute_bash` | 一次性 Bash 命令执行。白名单校验 + 破坏模式拦截（rm -rf、mkfs、dd、fork bomb 等） |
| `execute_python` | Python 沙箱执行（默认 60s 超时，256MB 内存限制）。可用 socket/ssl/urllib/requests/re/json/base64/hashlib；禁止 os/subprocess/shutil/ctypes/pickle |

### 持久 Shell 会话

| 工具名 | 说明 |
|---|---|
| `shell_open` | 打开持久 PTY Shell 会话。典型用法：`shell_open('ssh user@host')`、`shell_open('bash')`、`shell_open('nc -lvnp 4444')` |
| `shell_exec` | 向指定会话发送命令并读取输出。支持 timeout 和 idle_timeout 参数 |
| `shell_signal` | 向会话发送信号（默认 SIGINT），用于中断卡住的命令 |
| `shell_close` | 关闭指定会话并清理资源 |
| `shell_list` | 列出所有活跃会话及其状态 |

### OOB 回调监听

| 工具名 | 说明 |
|---|---|
| `oob_start` | 启动本地 HTTP 回调监听器（用于确认 SSRF/blind XSS/Log4j/blind RCE）。返回 callback_url 和 token |
| `oob_logs` | 查询回调记录。`token_match=true` 的是本会话 payload 触发的 |
| `oob_stop` | 停止监听器，释放端口 |

### 文件操作

| 工具名 | 说明 |
|---|---|
| `read_file` | 读文件，返回带行号的内容（默认 2000 行）。支持 offset/limit 分页 |
| `write_file` | 创建或覆盖文件。自动展示 unified diff |
| `edit_file` | 精确字符串替换。old_string 必须在文件中唯一匹配 |
| `multi_edit_file` | 批量编辑。任意 edit 失败则全部回滚。支持 replace_all |
| `grep` | 跨文件正则搜索。支持 glob 过滤、忽略目录（.git/node_modules/__pycache__ 等） |

### 知识库与凭据

| 工具名 | 说明 |
|---|---|
| `update_target` | 写入/更新目标信息（开放端口、服务版本、备注） |
| `cred_add` | 存入凭据（password/hash/token/key/ssh-key）。相同 (host,user,service,port,secret) 自动去重 |
| `cred_list` | 列出凭据库（可过滤 host、只看已验证） |
| `cred_verify` | 将凭据标记为已验证（登录成功后调用） |

### 结构化解析

| 工具名 | 说明 |
|---|---|
| `parse_nmap` | 解析 nmap XML/文本输出为结构化 JSON（hosts[ports[service,product,version]]）。可选自动 update_target |
| `parse_http` | 解析原始 HTTP 请求/响应文本为结构化字段（method/status/headers/body） |

### 规划、协作与报告

| 工具名 | 说明 |
|---|---|
| `todo_write` | 写入/更新 todo 列表。每个 item 含 content + status（pending/in_progress/completed）。显示在侧栏 |
| `task` | 派发独立 SubAgent 执行子任务（自包含描述，独立消息历史，独立 ReAct 循环） |
| `dispatch_sub_agent` | 派发红队专业 SubAgent（recon/exploit/lateral/persist/report） |
| `generate_report` | 汇总会话发现为 Markdown/HTML 渗透测试报告。可选包含 token/成本统计 |

### 上下文管理

| 工具名 | 说明 |
|---|---|
| `read_artifact` | 取回被存档的完整工具输出（看到 `artifact://<id>` 指针时使用）。支持 offset/limit 分页 |

### 字典管理

| 工具名 | 说明 |
|---|---|
| `wordlist_list` | 扫描常见路径（SecLists/Kali/系统目录）找已安装的字典文件 |
| `wordlist_top` | 读取字典前 N 行（避免大字典直接 read_file 爆上下文） |

### MCP 扩展工具

所有已连接 MCP 服务器的工具自动以 `mcp__<server>__<tool>` 格式注入，在 LLM 工具
列表中与内置工具并列。调用时自动路由到对应 MCP 客户端。

---

## 路线图

以下是计划中的功能和改进（无特定顺序）：

**近期**

- Docker 容器化部署支持
- pip 包发布（`pip install drx-agent`）
- 报告模板自定义（Jinja2 模板引擎）
- 多语言报告生成（英文 / 中文 / 日文）

**中期**

- 插件系统：第三方可注册自定义工具和 SubAgent 类型
- Web 仪表板（替代/补充 TUI）：远程监控和控制
- 多 Agent 协作模式：多个 Master Agent 共享知识库，分布式测试
- 目标范围自动发现（ASN、DNS 枚举、子域名爆破集成）
- 集成 Burp Suite / ZAP（通过 MCP 或专用适配器）

**远期**

- 对抗性模拟：ATT&CK 框架完整映射，战术编排
- 持续安全测试模式：定期自动扫描 + 变更检测
- 社区技能市场：可共享的 exploit/recon 技能包
- 多租户 SaaS 平台（仅限授权测试）

---

## 免责声明

DRX-Operator（以下简称"本工具"）仅供授权的安全测试、研究、教育和合法的红队演练使用。

**使用本工具即表示您确认：**

1. 您已获得目标系统所有者的明确书面授权，允许对其进行安全测试。
2. 您将遵守所有适用的法律、法规和规章。
3. 未经授权访问计算机系统属于违法行为，可能导致民事和/或刑事处罚。
4. 本工具的开发者/贡献者不对因使用或滥用本工具而造成的任何损害、损失或法律后果
   承担责任。
5. 您对使用本工具所采取的一切行动及其后果承担全部责任。

**如果您不确定是否有权测试目标系统，请不要使用本工具。如有疑问，请咨询法律顾问。**

本工具的安全门控（SafetyGate）和权限引擎（PermissionEngine）仅为辅助控制，
不能替代使用者的专业判断和法律合规意识。
