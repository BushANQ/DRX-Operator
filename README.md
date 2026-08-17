# DRX-Operator

[English](README.md) | [简体中文](README-zh.md)

An autonomous red-team penetration testing expert system — an **Agent-First**, LLM-driven autonomous security testing platform.

[Python 3.10+] [Alpha]

**Author**: [BushSEC](https://github.com/BushANQ) · [bushsec.cn](https://bushsec.cn)

---

DRX-Operator is an autonomous penetration testing system built around an **Agent-First architecture**. Unlike traditional security tools, the core of DRX-Operator is an LLM-powered **Master Agent** that autonomously makes decisions, invokes toolchains, analyzes results, and continuously executes security testing tasks through a ReAct (Reasoning + Acting) loop.

The terminal interface (TUI) is merely a thin presentation layer. The Agent is a first-class citizen of the system — **all operations are performed through LLM tool calls**.

DRX-Operator includes built-in Python/Bash sandboxes, persistent shell session management, OOB callback listeners, MCP protocol extensions, a declarative permission engine, a five-level safety gating system, an evidence-driven vulnerability discovery model, and a seven-layer context compaction pipeline designed to support extremely long-running red-team sessions.

**This tool is intended exclusively for authorized security testing. Unauthorized access to systems you do not own or have explicit permission to test may be illegal. Before using DRX-Operator, ensure that you have obtained written authorization from the owner of the target system.**

---

## Table of Contents

* [Core Features](#core-features)
* [Installation and Configuration](#installation-and-configuration)
* [Quick Start](#quick-start)
* [Architecture Overview](#architecture-overview)
* [Tool Reference](#tool-reference)
* [Roadmap](#roadmap)
* [Disclaimer](#disclaimer)

---

## Core Features

**Autonomous ReAct Decision Loop** — A five-stage reasoning cycle consisting of Plan, Think, Act, Observe, and Reflect. The Agent continuously adapts its strategy based on real-time tool outputs without requiring manual intervention.

**30+ Built-in Tools** — HTTP fetching, Bash/Python sandbox execution, persistent shell sessions (SSH/reverse shells), OOB callback listeners, NVD CVE lookup, web search, file read/write and precise editing, structured parsing (nmap XML and raw HTTP), and wordlist management.

**Evidence-Driven Vulnerability Discovery** — Every Finding includes an Evidence array, confidence score, `verified` field, and optional CVE association. Conclusions must be based on concrete data returned by tools; unsupported or hallucinated findings are prohibited.

**Five-Level Safety Gating** — From L0 (reconnaissance, automatically approved) to L4 (destructive operations requiring a confirmation phrase), combined with a declarative `PermissionEngine` based on ordered glob-style `allow` / `ask` / `deny` rules, where the first matching rule takes effect.

**MCP Protocol Support** — External tool servers can be integrated through the Model Context Protocol. MCP tool schemas are automatically injected into the LLM tool list and invoked using the `mcp__<server>__<tool>` naming convention.

**Priority-Based Task Scheduling and Parallel Dispatch** — A five-level priority queue: Exploit > Recon > Lateral > Persist > Report. Supports per-target concurrency limits and global QPS controls. Each SubAgent maintains its own message history and ReAct loop, allowing multiple subtasks to run concurrently.

**Session Persistence** — SQLite metadata combined with JSON file storage enables complete session save/restore, including the knowledge base, message history, todo list, operating mode, and token usage statistics.

**LLM Resilience Layer** — Exponential-backoff retries plus Provider fallback chains. HTTP 429, 5xx, and connection-related errors are retried automatically. If retries are exhausted, the system automatically switches to the next Provider, while the UI displays the failover status in real time.

**Seven-Layer Context Compaction Pipeline** — From L1 (automatic archival of large tool outputs) through L7 (cross-Agent artifact sharing), enabling tens of thousands of interaction turns within a single session without exceeding the model's context window.

**Terminal Interface (Textual TUI)** — A four-region interface consisting of a chat panel, sidebar (task board + SubAgent status), input composer, and status bar. Press `Ctrl+S` to interrupt the current task.

---

## Installation and Configuration

### Prerequisites

* Python 3.10 or later
* Optional system tools that the Agent may invoke when needed: `nmap`, `curl`, `git`, `ssh`, `openssl`, etc.

### Install from Source

```bash
git clone https://github.com/BushANQ/DRX-Operator.git
cd DRX-Operator
pip install -r requirements.txt
```

Major Python dependencies include:

* `textual` — TUI framework
* `ddgs` — web search
* `requests` / `urllib3` — HTTP requests
* `anthropic` / `openai` — LLM Providers
* `pyyaml` — skill configuration parsing

### Configure the LLM

**Step 1 (required): Configure your API key through environment variables.**

The repository's `configs/default_config.json` is a clean template with an empty `api_key` field. **Do not store API keys in any file that may be committed to version control.**

Recommended setup:

```bash
cp .env.example .env       # Copy the template (.env is ignored by .gitignore)
# Edit .env and add your API key, for example:
#   DRX_LLM_API_KEY=sk-xxxxxxxx
set -a && source .env && set +a    # Load variables into the current shell
```

API key resolution priority:

`DRX_LLM_API_KEY` > Provider-specific environment variable (see below) > `api_key` field in the configuration file.

**Step 2 (optional): Configure the Provider / model.**

Edit the `llm` section in `configs/default_config.json`. Only modify non-sensitive fields such as `provider`, `model`, `base_url`, and `temperature`:

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

Supported Provider types:

| `provider` value              | Provider implementation | Environment variable |
| ----------------------------- | ----------------------- | -------------------- |
| `anthropic` or `claude`       | AnthropicProvider       | `ANTHROPIC_API_KEY`  |
| `openai`                      | OpenAIProvider          | `OPENAI_API_KEY`     |
| `openai_compatible` (default) | DeepSeekProvider        | `DEEPSEEK_API_KEY`   |

**Fallback chain** (optional): automatically switch Providers when the primary Provider becomes unavailable.

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

### Configure MCP Servers (Optional)

Add MCP server definitions under `mcp.servers` in `configs/default_config.json`:

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

When DRX-Operator starts, it automatically connects to all MCP servers with `enabled: true` and injects their tools into the LLM tool list using the following naming convention:

```text
mcp__<server_name>__<tool_name>
```

### Launch

```bash
python -m drx_agent.main
```

---

## Quick Start

### Interface Layout

After launching DRX-Operator, the Textual TUI is divided into four regions:

* **Chat Panel** (main area on the left): displays Agent reasoning, tool-call cards, and results
* **Sidebar** (right): shows the todo task list at the top and active SubAgent status below
* **Input Composer** (bottom): accepts natural-language instructions and slash commands
* **Status Bar** (bottom-most): displays current system status

### Basic Interaction

Enter natural-language instructions directly into the input box. The Agent will autonomously decompose the task, invoke tools, analyze the results, and report its findings.

For example:

```text
Scan the 192.168.1.0/24 subnet for web services
```

```text
Perform a port scan and service identification against target.example.com
```

```text
Check whether http://test.example.com is vulnerable to SQL injection
```

```text
Generate a report summarizing the findings from this session
```

### Slash Commands

| Command                             | Description                                                |
| ----------------------------------- | ---------------------------------------------------------- |
| `/scan <target>`                    | Start a reconnaissance scan                                |
| `/exploit <target>`                 | Start vulnerability exploitation                           |
| `/target <host>`                    | Manage target host information                             |
| `/status`                           | Show current system status                                 |
| `/plan`                             | Switch to Plan mode (read-only tools only)                 |
| `/act`                              | Switch to Act mode (all tools enabled)                     |
| `/mode`                             | Show the current operating mode                            |
| `/stop`, `/cancel`, or `/interrupt` | Interrupt the current task                                 |
| `/dream`                            | Trigger deep context compaction (L6)                       |
| `/context`                          | Show context usage                                         |
| `/progress`                         | Show the progress document (9-section structure)           |
| `/memory`                           | Show project memory (`DRX.md` / `AGENTS.md` / `CLAUDE.md`) |
| `/memory reload`                    | Reload project memory files                                |

### Plan Mode and Act Mode

To reduce operational risk, the Agent supports two execution modes:

* **Plan mode**: Only read-only tools are permitted (`read_file`, `grep`, `web_search`, `cve_lookup`, `http_fetch`, `parse_nmap`, `parse_http`, `todo_write`, `shell_list`). Calls involving write/exec/shell/dispatch operations are rejected with an explanatory message. This mode is intended for analysis and planning.

* **Act mode** (default): All tools are available. The Agent may perform scans, exploitation, file writes, and other active operations.

Use `/plan` and `/act` to switch between modes.

### Session Management

Sessions are automatically saved under the `sessions/` directory.

Stored data includes:

* Knowledge base (targets, findings, credentials) → `sessions/<id>/kb.json`
* Message history → `sessions/<id>/messages.json`
* Metadata (todo list, mode, usage statistics) → `sessions/sessions.db` (SQLite)

Enter `"restore session"` in the chat to restore the most recently saved session.

### Project Memory

Create any of the following files in the project root or one of its parent directories:

* `DRX.md`
* `AGENTS.md`
* `CLAUDE.md`

Their contents are injected into the System Prompt of every LLM call as project memory.

This is useful for storing persistent instructions such as testing scope, engagement rules, and compliance requirements.

After editing these files, use:

```text
/memory reload
```

to reload them.

---

## Architecture Overview

### System Layers

```text
+------------------------------------------------------------------+
|                        TUI (Textual App)                          |
|  ChatPanel | Sidebar | Composer | StatusFooter                   |
|         Thin presentation layer — all operations via EventBus     |
+------------------------------------------------------------------+
                                | EventBus
+------------------------------------------------------------------+
|                     Master Agent (ReAct Loop)                     |
|  Plan -> Think -> Act -> Observe -> Reflect                       |
|  System Prompt Builder | Tool Schema Mgmt | Tool Execution Router |
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

### SubAgent Dispatch Mechanism

When a task can be decomposed into independent subtasks, the Master Agent dispatches SubAgents through the `task` tool.

Each SubAgent:

1. Has its own unique `agent_id` (for example, `recon-a1b2c3`) and independent message history
2. Shares the parent Agent's `tool_executor` and therefore uses the same sandbox, shell sessions, and knowledge base
3. Runs its own ReAct loop with a maximum iteration count and TTL limit
4. Publishes a `SUB_AGENT_RESULT` event through the EventBus when finished, allowing the sidebar to update in real time

SubAgents cannot recursively invoke the `task` tool, preventing uncontrolled recursive Agent spawning.

### Security Model

DRX-Operator implements a two-layer security model.

**L0-L4 SafetyGate** — operation-level risk gating:

| Level | Description                           | Behavior                                   |
| ----- | ------------------------------------- | ------------------------------------------ |
| L0    | Reconnaissance                        | Automatically approved                     |
| L1    | Passive vulnerability scanning        | Approved once, then cached for the session |
| L2    | Active vulnerability exploitation     | Requires user approval for every operation |
| L3    | Credential / persistence attacks      | Requires user approval for every operation |
| L4    | Destructive / irreversible operations | Requires a confirmation phrase             |

**PermissionEngine** — declarative tool rules:

Independent of the SafetyGate, the PermissionEngine evaluates tool calls against an ordered list of rules:

```json
{
  "tool": "execute_bash",
  "match": "*rm -rf /*",
  "action": "deny",
  "note": "destructive root delete"
}
```

Supported decisions:

* `allow` — execute immediately
* `ask` — request user approval
* `deny` — reject the operation and return the reason

The first matching rule takes effect.

Users may respond with `always` to promote an approval rule to a session-wide permanent allowance.

### LLM Resilience Design

`ResilientProvider` wraps one or more Provider instances:

1. Send the request to the primary Provider
2. If the request fails due to a transient error (`429`, `5xx`, timeout, connection reset), retry using exponential backoff up to N times
3. If retries are exhausted, or the error is non-transient, automatically switch to the next Fallback Provider
4. If a streaming request fails after visible output (`text` / `tool_call`) has already been emitted, do not retry; return the error immediately to prevent duplicate output
5. The UI displays real-time status notifications such as `"Retrying..."` and `"Switching to Provider X..."`

---

## Tool Reference

### Networking and Information Gathering

| Tool         | Description                                                                                                           |
| ------------ | --------------------------------------------------------------------------------------------------------------------- |
| `http_fetch` | Fetch HTTP/HTTPS URLs. Supports GET/POST/PUT/DELETE/HEAD/OPTIONS, custom headers, and request bodies                  |
| `web_search` | Search-engine query returning title/url/snippet. Backend: ddgs (preferred) → DuckDuckGo Instant Answer API (fallback) |
| `cve_lookup` | Query the NVD API 2.0 and return CVE description, CVSS score, CWE, affected products, and references                  |

### Code and Command Execution

| Tool             | Description                                                                                                                                                                                                      |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `execute_bash`   | Execute a one-shot Bash command. Includes allowlist validation and destructive-pattern blocking (`rm -rf`, `mkfs`, `dd`, fork bombs, etc.)                                                                       |
| `execute_python` | Execute Python code in a sandbox (default 60-second timeout, 256 MB memory limit). Allows `socket`/`ssl`/`urllib`/`requests`/`re`/`json`/`base64`/`hashlib`; blocks `os`/`subprocess`/`shutil`/`ctypes`/`pickle` |

### Persistent Shell Sessions

| Tool           | Description                                                                                                                            |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `shell_open`   | Open a persistent PTY shell session. Typical usage: `shell_open('ssh user@host')`, `shell_open('bash')`, `shell_open('nc -lvnp 4444')` |
| `shell_exec`   | Send commands to a specified session and read its output. Supports `timeout` and `idle_timeout`                                        |
| `shell_signal` | Send a signal to a session (SIGINT by default), typically used to interrupt a stuck command                                            |
| `shell_close`  | Close a specified session and release its resources                                                                                    |
| `shell_list`   | List all active shell sessions and their current status                                                                                |

### OOB Callback Listener

| Tool        | Description                                                                                                            |
| ----------- | ---------------------------------------------------------------------------------------------------------------------- |
| `oob_start` | Start a local HTTP callback listener for confirming SSRF/blind XSS/Log4j/blind RCE. Returns a `callback_url` and token |
| `oob_logs`  | Query callback records. Entries with `token_match=true` were triggered by payloads associated with the current session |
| `oob_stop`  | Stop the listener and release its port                                                                                 |

### File Operations

| Tool              | Description                                                                                                                        |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `read_file`       | Read a file with line numbers (default: 2,000 lines). Supports `offset`/`limit` pagination                                         |
| `write_file`      | Create or overwrite a file. Automatically displays a unified diff                                                                  |
| `edit_file`       | Perform exact string replacement. `old_string` must match exactly once in the file                                                 |
| `multi_edit_file` | Apply multiple edits atomically. If any edit fails, all changes are rolled back. Supports `replace_all`                            |
| `grep`            | Perform cross-file regex searches. Supports glob filters and ignores directories such as `.git`, `node_modules`, and `__pycache__` |

### Knowledge Base and Credentials

| Tool            | Description                                                                                                                                       |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `update_target` | Add or update target information, including open ports, service versions, and notes                                                               |
| `cred_add`      | Store credentials (`password`/`hash`/`token`/`key`/`ssh-key`). Identical `(host,user,service,port,secret)` entries are automatically deduplicated |
| `cred_list`     | List credentials, optionally filtered by host or verification status                                                                              |
| `cred_verify`   | Mark a credential as verified after successful authentication                                                                                     |

### Structured Parsing

| Tool         | Description                                                                                                                               |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `parse_nmap` | Parse nmap XML/text output into structured JSON (`hosts[ports[service,product,version]]`). Optionally calls `update_target` automatically |
| `parse_http` | Parse raw HTTP request/response text into structured fields (`method`/`status`/`headers`/`body`)                                          |

### Planning, Collaboration, and Reporting

| Tool                 | Description                                                                                                                             |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `todo_write`         | Create or update the todo list. Each item contains `content` + `status` (`pending`/`in_progress`/`completed`). Displayed in the sidebar |
| `task`               | Dispatch an independent SubAgent to execute a self-contained subtask with its own message history and ReAct loop                        |
| `dispatch_sub_agent` | Dispatch a specialized red-team SubAgent (`recon`/`exploit`/`lateral`/`persist`/`report`)                                               |
| `generate_report`    | Generate a Markdown/HTML penetration testing report from session findings. Optionally includes token/cost statistics                    |

### Context Management

| Tool            | Description                                                                                                                                    |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `read_artifact` | Retrieve the complete output of an archived tool result when an `artifact://<id>` pointer is encountered. Supports `offset`/`limit` pagination |

### Wordlist Management

| Tool            | Description                                                                                         |
| --------------- | --------------------------------------------------------------------------------------------------- |
| `wordlist_list` | Scan common locations (SecLists/Kali/system directories) for installed wordlist files               |
| `wordlist_top`  | Read the first N lines of a wordlist, preventing large wordlists from exhausting the context window |

### MCP Extension Tools

Tools exposed by all connected MCP servers are automatically injected into the LLM tool list using the following naming convention:

```text
mcp__<server>__<tool>
```

They appear alongside built-in tools and are automatically routed to the corresponding MCP client when invoked.

---

## Roadmap

The following features and improvements are planned, in no particular order.

### Near Term

* Docker-based deployment support
* PyPI package release (`pip install drx-agent`)
* Customizable report templates using Jinja2
* Multilingual report generation (English / Chinese / Japanese)

### Mid Term

* Plugin system allowing third parties to register custom tools and SubAgent types
* Web dashboard to replace or complement the TUI, enabling remote monitoring and control
* Multi-Agent collaboration mode, allowing multiple Master Agents to share a knowledge base and perform distributed testing
* Automated target-scope discovery, including ASN discovery, DNS enumeration, and subdomain brute-force integration
* Burp Suite / ZAP integration through MCP or dedicated adapters

### Long Term

* Adversary emulation with full MITRE ATT&CK mapping and tactic orchestration
* Continuous security testing mode with scheduled automated scanning and change detection
* Community skill marketplace for reusable exploit/recon skill packages
* Multi-tenant SaaS platform for authorized security testing

---

## Disclaimer

DRX-Operator (hereinafter referred to as "the Tool") is intended solely for **authorized security testing, research, education, and lawful red-team exercises**.

**By using this Tool, you acknowledge and agree that:**

1. You have obtained explicit written authorization from the owner of the target system to perform security testing.
2. You will comply with all applicable laws, regulations, and rules.
3. Unauthorized access to computer systems may be illegal and may result in civil and/or criminal penalties.
4. The developers and contributors of this Tool shall not be held liable for any damage, loss, or legal consequences resulting from the use or misuse of the Tool.
5. You assume full responsibility for all actions performed using the Tool and for any consequences arising from those actions.

**If you are unsure whether you are authorized to test a target system, do not use this Tool against that system. If necessary, consult qualified legal counsel before proceeding.**

The built-in `SafetyGate` and `PermissionEngine` are auxiliary safety controls only. They are **not substitutes for professional judgment, proper authorization, or legal and regulatory compliance**.
