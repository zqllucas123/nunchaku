# 客户端配置与使用手册

本文面向**客户机器**，说明如何把提供方部署的 `nunchaku` 远端 Skill 接到本地 Agent 中并使用。服务端的部署、私有 Skill 编写和管理台用法见 [README](../README.md) 与 [架构说明](architecture.md)。

## 概览

提供方的服务端运行一个**统一网关**，它背后可以有任意多个远端服务（如 `md2docx`、`translate`）。客户机器上只需要装两样东西，**都不随服务增减而变化**：

| 内容 | 来源 | 作用 |
|---|---|---|
| `nunchaku` CLI | 提供方给的 wheel | 本地桥接器：读取文件、调用网关、校验冲突、写回结果 |
| `nunchaku-remote` 通用连接 Skill | wheel 内自带，用 `nunchaku install-skill` 安装 | 告诉 Agent：远端能力以 MCP 工具的形式提供，看工具描述来选 |

提供方还会给你一个**网关地址**，形如 `http://127.0.0.1:8750/mcp` 或 `https://your-host/mcp`。

之后提供方新增服务时，**只需刷新 MCP 连接**，新服务就会作为一个新工具出现，不用再安装或修改任何东西。

## 1. 安装 CLI

```bash
uv tool install nunchaku-0.1.1-py3-none-any.whl
nunchaku --help
```

wheel 文件名以实际拿到的为准。Windows 上装好后一般在 `C:\Users\<用户名>\.local\bin\nunchaku.exe`。

**配置 MCP 时请写 `.exe` 全路径，不要只写 `nunchaku`。** Agent 客户端拉起 STDIO 子进程时不保证继承你终端里的 `PATH`，只写命令名容易报"找不到程序"。

以后升级 wheel 用 `uv tool install --force <新 wheel>`。

## 2. 安装通用连接 Skill

```bash
nunchaku install-skill
```

它会列出本机上找到的 Agent Skill 目录让你选（可多选，直接回车表示全部）：

```
Agent Skill directories found:
  1. Claude Code  C:\Users\sdw\.claude\skills
  2. AIKO         C:\Users\sdw\.sdw\skills
  3. Codex        C:\Users\sdw\.agents\skills
  4. other directory
Install into [1-4, comma-separated, Enter = all found]:
```

选 `other directory` 可以手动输入其他目录。也可以不走交互，直接指定（可重复）：

```bash
nunchaku install-skill --path "$HOME/.sdw/skills" --url http://127.0.0.1:8750/mcp
```

- 装好后目录名为 `nunchaku-remote`。里面不包含任何服务信息，所以一台机器只需装一次。
- 目标目录已存在时会拒绝覆盖，避免冲掉你手改过的内容；确实要覆盖时加 `--force`。
- 加 `--url` 时，安装完成后打印的下一步命令会直接带上网关地址。

## 3. 注册 MCP

**MCP 服务器名请用 `nunchaku`。** 连接 Skill 里就是这样称呼它的。

访问网关需要 **Service Key**（形如 `nk_…`），由管理员在管理台为你生成，并决定你能使用哪些服务。把它放在 MCP 配置的环境变量 `NUNCHAKU_SERVICE_KEY` 里，不要写进命令行参数。Key 等同于个人账号，不要转借；怀疑泄露时请管理员停用后重新生成。

推荐用**桥接模式**：本地运行 `nunchaku bridge`，作为 STDIO 服务器。

```
nunchaku.exe bridge --url <网关地址> --root <项目目录>
```

桥接器启动时向网关拉取工具列表，在本地为每个服务注册一个同名工具。你只会看到 Key 被授权的服务。Agent 调用时只需给相对路径，由桥接器读取文件、发送、核对哈希并写回。二进制文件（如 .docx）也由它自动处理。

### AIKO

在 MCP 面板点「+」，名称填 `nunchaku`，类型选 **STDIO**，配置填：

```json
{
  "command": "C:\\Users\\sdw\\.local\\bin\\nunchaku.exe",
  "args": [
    "bridge",
    "--url",
    "https://nunchaku.corp.example.com/mcp",
    "--root",
    "D:\\works\\your-project"
  ],
  "env": {
    "NUNCHAKU_SERVICE_KEY": "nk_xxxxxxxxxxxxxxxx"
  }
}
```

**JSON 里的路径要写双反斜杠**（`\\`），单个 `\` 是转义符，会导致解析失败。也可以全部改用正斜杠，如 `C:/Users/...`。

先点「测试」，通过后再点「添加」，成功后状态显示 `connected`。这份配置保存在 `%USERPROFILE%\.sdw\mcp_servers.json` 中。

### Claude Code

```powershell
claude mcp add --scope user -e NUNCHAKU_SERVICE_KEY=nk_xxxxxxxx nunchaku -- C:\Users\sdw\.local\bin\nunchaku.exe bridge `
  --url https://nunchaku.corp.example.com/mcp --root "D:\works\your-project"
claude mcp list
```

`--scope user` 表示对所有项目生效。如果希望不同项目使用不同的 `--root`，可以改用 `--scope project`，在每个项目里各注册一份。

服务端单次调用最长约 180 秒，Claude Code 等待 MCP 工具的默认时间可能更短。遇到超时时，先设置环境变量再启动：

```powershell
$env:MCP_TOOL_TIMEOUT = "210000"
claude
```

### Codex

```bash
NUNCHAKU_SERVICE_KEY=nk_xxxxxxxx codex mcp add nunchaku -- nunchaku bridge --url https://nunchaku.corp.example.com/mcp --root /absolute/path/to/project
```

Key 也可以写进 `~/.codex/config.toml` 的 `[mcp_servers.nunchaku.env]`。确认信任该服务后，可以加入以下配置：

```toml
[mcp_servers.nunchaku]
tool_timeout_sec = 210
```

如果想减少审批打断，可以为具体工具设置 `approval_mode = "approve"`，例如 `[mcp_servers.nunchaku.tools.md2docx]`。注意工具名就是服务名。

### 直连模式（不装桥接器）

网关本身就是标准的 Streamable HTTP MCP 端点，任何支持 HTTP 类型的客户端都可以直接连接：

- AIKO：类型选 **HTTP**，URL 填网关地址，请求头加 `Authorization: Bearer <Service Key>`。
- Claude Code：`claude mcp add --transport http nunchaku https://nunchaku.corp.example.com/mcp --header "Authorization: Bearer nk_xxxxxxxx"`

直连时没有 `--root`，也就没有路径边界，而且 Agent 必须自己传文件内容、自己写回结果。`.docx` 这类二进制文件需要 Agent 自行做 base64 编解码，出错概率明显高于桥接模式，也没有冲突检测。**一般情况请用桥接模式。**

| | 桥接模式 | 直连模式 |
|---|---|---|
| 本地进程 | `nunchaku bridge`（STDIO） | 无 |
| Agent 传什么 | `task` + 相对路径 `paths` | `task` + 文件全文 `files` |
| 二进制文件 | 桥接器自动处理 | Agent 自己做 base64 编解码 |
| 冲突检测、自动写回 | 有 | 无 |
| 新增服务后 | 重启这条 MCP 连接 | 刷新工具列表即可 |
| 读写范围 | 限制在 `--root` 内 | 无限制 |

## `--root` 怎么填

`--root` 是桥接器的**读写边界**：只允许访问这个目录下的文件，Agent 传来的路径一律按相对于它来解析。它是进程启动参数，**不能在会话里临时指定**。这是有意的设计：哪些文件能读，由人在配置里决定，而不是交给可能被提示词带偏的模型。

- 填**你实际要处理的项目目录**。目录不存在时会报 `project root is not a directory`；目录存在但文件不在其中时会报 `not a regular file`。
- 不要填盘符根目录或用户主目录，否则就失去了边界的意义。
- 可以使用中文路径。报错信息里的中文显示成乱码，是 stderr 编码问题，不影响功能。
- 修改 `--root` 后，需要**重启这条 MCP 连接**才会生效。

如果要处理的文档分散在几个目录里，可以把 `--root` 设为它们的公共父目录，会话里再给相对路径，例如 `docs/guide.md`。

## 使用

Agent 会根据工具描述自动选择服务，直接说需求即可：

```
把 数据同步功能修复方案.md 转成 docx
```

也可以点名服务：

```
用 nunchaku 的 md2docx 工具把 数据同步功能修复方案.md 转成 docx
```

如果本地已有同类 Skill（比如本地也装了一个 md 转 docx 的 Skill），Agent 可能会选中本地那个。这时点名工具即可。

### 路径写法

`paths` 中每一项必须是相对于 `--root` 的 **POSIX 纯相对路径**，校验规则见 [src/nunchaku/runner.py](../src/nunchaku/runner.py)：

- 分隔符用 `/`，不能出现 `\`
- 不能是绝对路径，不能带盘符
- 不能以 `./` 开头，也不能含 `..`
- 不能以 `.agents`、`.claude`、`.codex`、`.git`、`.nunchaku` 开头（这些是保留目录）
- 必须指向真实存在的普通文件

正确写法如 `数据同步功能修复方案.md`、`docs/guide.md`。写成 `./数据同步功能修复方案.md` 会报 `file path must be a non-empty POSIX relative path`。

### 先看结果再落盘

默认情况下，桥接器核对哈希后会直接把结果写回项目。如果想先审阅，在会话里说"先不要写入，只给我看结果"，对应参数 `apply_changes: false`。此时二进制文件不会内联返回，而是写到项目下的 `.nunchaku/proposed/` 目录，具体位置见返回结果里的 `proposed_file` 字段。

如果在等待期间文件被其他程序改过，桥接器不会覆盖。`conflicts` 字段会列出这些路径，交给 Agent 用常规编辑方式处理。

## 新增服务后客户端要做什么

- **桥接模式**：在客户端里重启或刷新 `nunchaku` 这条 MCP 连接。桥接器只在启动时拉取一次工具列表。
- **直连模式**：大多数客户端在新会话或重连时会自动重新拉取工具列表。

本地的 wheel、Skill 和 MCP 配置都不需要改。

网关只会列出**正在运行**的服务。如果刷新后仍看不到某个服务，请让提供方确认该服务已经启动。

## 限制

| 项 | 值 |
|---|---|
| 单次调用超时 | 由服务端配置决定，默认 180 秒 |
| 文件数 | 最多 50 个 |
| 单个文件大小 | 10 MB（按解码后的字节计） |
| 单次总量 | 20 MB |
| 符号链接 | 不支持 |
| 调用状态 | 无状态，要接着上一轮继续，需要把必要的上下文重新传入 |

## 常见问题

**`cannot reach gateway http://...`**
桥接器启动时连不上网关。请确认网关已启动、地址和端口正确，以及本机能访问该地址。

**`cannot reach gateway ...: 401 Unauthorized: check NUNCHAKU_TOKEN`**
Service Key 缺失、写错、已停用或已过期。检查 MCP 配置 `env` 里的 `NUNCHAKU_SERVICE_KEY`，仍不行请联系管理员。（旧的变量名 `NUNCHAKU_TOKEN` 也仍然有效。）

**`this service key may not use xxx`**
你的 Key 没有被授权使用这个服务，请向管理员申请。

**`MCP error -32000: Connection closed`，stderr 中是 `project root is not a directory: D:\...`**
`--root` 指向的目录不存在，常见原因是把示例里的占位路径原样留在了配置里。

**`not a regular file: xxx.md`**
`--root` 本身正确，但文件不在这个目录下，或者相对路径写错了。

**`file path must be a non-empty POSIX relative path`**
路径带了 `./` 前缀、反斜杠或盘符。

**`unsafe file path`** / **`reserved file path`**
路径中含有 `..` 或空段，或者以保留目录开头。这是预期的拦截。

**`file payload exceeds POC size limit`**
超过了单文件 10 MB 或单次总量 20 MB 的限制。

**`unknown service: xxx`**
调用的服务在服务端已经被删除或改名。刷新 MCP 连接即可。

**报错里的中文显示成 `????` 或 `����`**
这是 stderr 编码不匹配，不是故障。

**调用超时**
调大客户端的 MCP 工具超时：Claude Code 用 `MCP_TOOL_TIMEOUT`，Codex 用 `tool_timeout_sec`，AIKO 如果支持配置，建议设为 210 秒以上。

**跨机器连不上**
请确认填的是**客户机器实际能访问到的地址**，而不是服务端自己看到的 `127.0.0.1`。如果服务端只监听本机，需要先建 SSH 隧道：`ssh -L 8750:127.0.0.1:8750 user@provider-host`。

**PowerShell 中 `Copy-Item -Recurse` 报 `ResourceExists`**
这是手动复制 Skill 时的常见现象：目标目录已存在时会报这个错，但子文件其实已经复制过去了。改用 `nunchaku install-skill --force` 可以避免。

## 旧方式：逐服务连接包

在网关出现之前，每个服务需要单独安装一个连接 Skill（如 `md2docx-remote`），并单独注册一条 MCP 配置：

```
nunchaku.exe bridge --connector <...>\md2docx-remote\connector.json --root <项目目录>
```

这种方式仍然可用。连接包由提供方通过 `nunchaku client-skill` 或管理台的「导出连接包」生成。从旧方式迁移到网关的步骤：

1. 删除旧的 `nunchaku-<服务名>` MCP 配置。
2. 删除对应的 `<服务名>-remote` Skill 目录，避免 Agent 同时看到两套说明。
3. 按上文第 2、3 步安装通用 Skill 并注册 `nunchaku`。

## 安全提醒

- 你提交的文件会传到服务端。不要提交密钥、凭据或无关文件。
- 访问网关需要 Service Key，它决定你能使用哪些服务。妥善保管，不要提交到代码仓库或发到群里。
- 私有提示词无法仅靠"不要泄露"的指令做到严格保密：拿到访问权限的人可以反复提问，诱导模型复述私有 Skill。
- 桥接模式默认会写入你的工作区。确认信任该服务后再使用，或者每次都用 `apply_changes: false` 先审阅。
