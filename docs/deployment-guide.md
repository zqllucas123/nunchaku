# nunchaku 私有化部署与使用教程

本教程面向两类读者：

- **管理员**：在公司服务器上部署 nunchaku，管理远端 Skill 服务。见第 1～4 章。
- **员工**：在自己电脑的 AIKO 中安装、配置并使用这些服务。见第 5～6 章。

第 7 章是日常运维，第 8 章是常见问题。

---

## 0. 整体结构

```mermaid
flowchart LR
    subgraph client["员工电脑"]
        A["AIKO"] -->|"STDIO"| B["nunchaku bridge<br/>--root 项目目录"]
    end
    subgraph server["公司服务器"]
        N["Nginx :443 / :80<br/>可选"] --> G["nunchaku 网关<br/>127.0.0.1:8750"]
        G --> S1["md2docx 服务<br/>127.0.0.1:8765"]
        G --> S2["其他服务<br/>127.0.0.1:87xx"]
        S1 --> E1["执行引擎 + 私有 Skill"]
        S2 --> E2["执行引擎 + 私有 Skill"]
    end
    B -->|"HTTP(S) /mcp"| N
```

| 组件                      | 位置     | 说明                                                                                                         |
| ------------------------- | -------- | ------------------------------------------------------------------------------------------------------------ |
| 服务进程                  | 服务器   | 每个服务一个进程、一个端口，只监听本机。调用时在临时目录里运行执行引擎（Codex、Claude、Hermes、aiko.exe 等） |
| 网关                      | 服务器   | **唯一对外入口**。把每个在线的服务列成一个 MCP 工具，按工具名转发                                      |
| 私有 Skill                | 服务器   | 真正的方法论和脚本，**不会下发给员工**                                                                 |
| nunchaku CLI              | 员工电脑 | 本地桥接器：读取文件 → 调用网关 → 校验冲突 → 写回结果                                                     |
| `nunchaku-remote` Skill | 员工电脑 | 通用连接说明，随 CLI 分发，不含任何服务信息                                                                  |

**关键特性**：服务器新增服务后，员工**只需在 AIKO 里重启一下 MCP 连接**，不需要重新安装任何东西。

---

## 1. 部署前准备

### 1.1 服务器要求

| 项       | 要求                                                                |
| -------- | ------------------------------------------------------------------- |
| 操作系统 | Linux（推荐）或 Windows Server                                      |
| Python   | 3.11 及以上（用 `uv` 管理即可，不必系统安装）                     |
| 工具     | `uv`、`git`（可选）                                             |
| 执行引擎 | 每个服务配置的引擎 CLI 必须在服务器上可用，并已登录或配置好模型凭据 |
| 网络     | 员工电脑能访问网关端口（或 Nginx 端口）                             |

> **执行引擎的凭据只放在服务器上**。员工电脑不需要任何模型账号或 API Key。

### 1.2 规划地址

先确定员工访问网关用的地址，后面所有配置都要用到。下面以 `nunchaku.corp.example.com` 为例：

| 方案                      | 员工使用的地址                            | 适用                 |
| ------------------------- | ----------------------------------------- | -------------------- |
| A. Nginx 反向代理（推荐） | `https://nunchaku.corp.example.com/mcp` | 正式使用             |
| B. 网关直接对内网开放     | `http://10.0.0.20:8750/mcp`             | 快速试用、小范围验证 |

### 1.3 安全须知（请务必阅读）

当前版本**没有登录和鉴权**：能访问网关的人就能调用所有服务。部署时必须：

1. **只在公司内网开放**，不要映射到公网。
2. 用防火墙或 Nginx `allow/deny` 把访问限制在办公网段。
3. 各服务端口（8765 等）和管理台（8787）**只监听 `127.0.0.1`**，不要对外开放。
4. 告知员工：提交的文件会传到服务器处理，**不要提交密钥、凭据、无关文件**。
5. 私有 Skill 的内容不能仅靠"不要泄露"的提示词做到绝对保密。真正必须保密的规则和数据，应写进服务端脚本或工具，而不是 Skill 文本。

---

## 2. 服务器安装

以下以 Linux、安装目录 `/opt/nunchaku` 为例。Windows 服务器的差异见 2.5。

### 2.1 安装 uv 和代码

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # 安装 uv（内网可改用离线包）
sudo mkdir -p /opt/nunchaku && sudo chown $USER /opt/nunchaku
git clone https://github.com/zqllucas123/nunchaku.git /opt/nunchaku
cd /opt/nunchaku
uv sync
uv run nunchaku --help
```

内网有 PyPI 镜像时，在 `uv sync` 前设置：

```bash
export UV_INDEX_URL=https://mirrors.example.com/pypi/simple/
```

### 2.2 放置私有 Skill

每个服务对应一个目录，目录里必须有 `SKILL.md`，可以附带脚本和模板：

```
/srv/private-skills/
└── md-to-docx/
    ├── SKILL.md
    ├── scripts/md_to_docx.py
    └── templates/default.docx
```

建议权限只给运行 nunchaku 的账号：

```bash
sudo chown -R nunchaku:nunchaku /srv/private-skills
sudo chmod -R o-rwx /srv/private-skills
```

### 2.3 编写 `services.json`

在 `/opt/nunchaku/services.json` 中：

```json
{
  "gateway": {"host": "127.0.0.1", "port": 8750},
  "services": [
    {
      "name": "md2docx",
      "description": "把 Markdown 文档转换为符合公司模板的 Word（.docx）文档。",
      "skill": "/srv/private-skills/md-to-docx",
      "engine": "custom",
      "command": "/usr/local/bin/aiko",
      "args": ["--ci", "--permission-mode", "autoApprove", "--timeout", "170", "-p", "-"],
      "host": "127.0.0.1",
      "port": 8765,
      "timeout_seconds": 180
    }
  ]
}
```

字段说明：

| 字段                   | 说明                                                                                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gateway.host`       | 用 Nginx 时保持 `127.0.0.1`；网关直接对内网开放时改成 `0.0.0.0`                                                                               |
| `gateway.port`       | 网关端口，不能和任何服务重复                                                                                                                      |
| `name`               | 服务名，也是员工在 AIKO 里看到的**工具名**。小写字母开头，只能含小写字母、数字、`-`、`_`                                                |
| `description`        | **非常重要**。AIKO 靠这段话判断什么时候调用这个服务，请写清楚"做什么、输入什么、产出什么"                                                   |
| `skill`              | 私有 Skill 目录                                                                                                                                   |
| `engine`             | `codex`、`claude`、`hermes` 或 `custom`                                                                                                   |
| `command` / `args` | 自定义引擎的可执行文件和参数。参数里可用 `{prompt}`、`{workspace}`、`{output}`、`{skill}` 占位符；不写 `{prompt}` 时提示词从 stdin 传入 |
| `port`               | 服务自己的端口，每个服务不同                                                                                                                      |
| `timeout_seconds`    | 单次执行上限。自定义引擎自身的超时（如上面的 `--timeout 170`）应略小于它                                                                        |

校验配置：

```bash
uv run nunchaku --config services.json list
```

### 2.4 用 systemd 常驻运行

创建服务账号：

```bash
sudo useradd --system --home /opt/nunchaku --shell /usr/sbin/nologin nunchaku
sudo chown -R nunchaku:nunchaku /opt/nunchaku
```

`/etc/systemd/system/nunchaku-services.service`（运行所有服务，每个服务一个子进程）：

```ini
[Unit]
Description=nunchaku skill services
After=network-online.target

[Service]
User=nunchaku
WorkingDirectory=/opt/nunchaku
ExecStart=/home/nunchaku/.local/bin/uv run --frozen nunchaku --config services.json serve-all
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/nunchaku-gateway.service`：

```ini
[Unit]
Description=nunchaku gateway
After=nunchaku-services.service
Wants=nunchaku-services.service

[Service]
User=nunchaku
WorkingDirectory=/opt/nunchaku
ExecStart=/home/nunchaku/.local/bin/uv run --frozen nunchaku --config services.json gateway
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

`uv` 的路径以 `which uv` 的结果为准。执行引擎需要的环境变量（如模型 API Key）用 `Environment=` 或 `EnvironmentFile=` 加进 `nunchaku-services.service`。

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nunchaku-services nunchaku-gateway
sudo systemctl status nunchaku-services nunchaku-gateway
journalctl -u nunchaku-gateway -f        # 查看网关日志
```

> 也可以不用 systemd，改用管理台（第 4 章）手动启停。但管理台退出时会停掉它启动的所有进程，**正式环境请用 systemd**。

### 2.5 Windows 服务器的差异

- 路径用 `D:\\nunchaku\\...` 形式，JSON 里反斜杠要写两遍。
- 用 [NSSM](https://nssm.cc/) 把两条命令注册成 Windows 服务：

  ```powershell
  nssm install nunchaku-services "C:\Users\svc\.local\bin\uv.exe" "run --frozen nunchaku --config services.json serve-all"
  nssm set nunchaku-services AppDirectory D:\nunchaku
  nssm install nunchaku-gateway "C:\Users\svc\.local\bin\uv.exe" "run --frozen nunchaku --config services.json gateway"
  nssm set nunchaku-gateway AppDirectory D:\nunchaku
  nssm start nunchaku-services
  nssm start nunchaku-gateway
  ```
- 服务账号需要有执行引擎（如 `aiko.exe`）的运行权限和登录态。
- 在 Windows 防火墙里只放行 Nginx 或网关端口。

---

## 3. 对内网开放网关

### 3.1 方案 A：Nginx 反向代理（推荐）

网关保持 `"host": "127.0.0.1"`，由 Nginx 对外提供 HTTPS：

```nginx
server {
    listen 443 ssl;
    server_name nunchaku.corp.example.com;

    ssl_certificate     /etc/nginx/certs/nunchaku.crt;
    ssl_certificate_key /etc/nginx/certs/nunchaku.key;

    # 只允许办公网段
    allow 10.0.0.0/8;
    deny  all;

    location /mcp {
        proxy_pass http://127.0.0.1:8750/mcp;
        proxy_http_version 1.1;

        # 网关只接受 Host 为 127.0.0.1 的请求，不要改成 $host
        proxy_set_header Host 127.0.0.1:8750;
        # 带外部域名的 Origin 会被网关拒绝（403），桥接器本身不发这个头
        proxy_set_header Origin "";
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # 远端执行最长 180 秒，代理要等得比它久
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;

        # 文件额度为 20 MB，base64 编码后约 27 MB
        client_max_body_size 32m;
        proxy_buffering off;
    }
}
```

三个容易踩的坑：

1. **`Host` 头必须是 `127.0.0.1:8750`**。网关监听本机时会开启防 DNS 重绑定校验，收到别的 Host 会返回 `421`；带外部域名的 `Origin` 头会返回 `403`，所以一并清掉。
2. **超时要大于 180 秒**。Nginx 默认 60 秒，会在转换完成前断开。
3. **请求体上限要放大**。Nginx 默认 1 MB，稍大的文档就会返回 `413`。

员工使用的地址：`https://nunchaku.corp.example.com/mcp`

如果用的是公司内部 CA 签发的证书，员工电脑需要信任这个 CA，见 8.2。

### 3.2 方案 B：网关直接对内网开放

把 `services.json` 中的网关改为：

```json
"gateway": {"host": "0.0.0.0", "port": 8750}
```

然后在防火墙里只对办公网段放行 8750：

```bash
sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=10.0.0.0/8 port port=8750 protocol=tcp accept'
sudo firewall-cmd --reload
```

员工使用的地址：`http://<服务器IP>:8750/mcp`

此方案为明文 HTTP，且网关不再校验 Host 头，**只建议用于试用**。

### 3.3 验证

在**一台员工电脑**上执行（地址换成你的）：

```powershell
curl.exe -s -X POST https://nunchaku.corp.example.com/mcp `
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" `
  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}'
```

返回内容里应能看到 `"name":"md2docx"`。如果返回的工具列表为空，说明网关通了，但服务没在运行（网关只列出在线的服务）。

---

## 4. 管理台（可选）

管理台只监听服务器本机，用于可视化地新增、编辑、启停服务，以及查看日志：

```bash
uv run nunchaku --config services.json dashboard --port 8787
```

从自己电脑访问需建 SSH 隧道：

```bash
ssh -L 8787:127.0.0.1:8787 admin@nunchaku-server
```

然后打开 [http://127.0.0.1:8787/](http://127.0.0.1:8787/)。

注意：

- 正式环境的服务由 systemd 管理时，**不要在管理台里再启动同一个服务**，会因端口占用而失败。管理台此时主要用于编辑配置和查看日志。
- 管理台没有登录，只能通过 SSH 隧道访问，不要对外开放。

---

## 5. 员工电脑：安装与配置

### 5.1 管理员准备安装包

在服务器或任意一台电脑上构建 wheel：

```bash
cd /opt/nunchaku
uv build --wheel
ls dist/          # nunchaku-0.1.0-py3-none-any.whl
```

把 wheel 放到员工能访问的共享位置，例如 `\\fileserver\tools\nunchaku\`。

**内网完全离线时**，还要把依赖的 wheel 一起放进去。在一台能联网的 **Windows** 电脑上执行（员工电脑的 Python 版本按 3.12 准备）：

```powershell
uv venv --python 3.12 .bundle
uv pip install --python .bundle dist\nunchaku-0.1.0-py3-none-any.whl
uv pip freeze --python .bundle | Select-String -NotMatch nunchaku | Out-File -Encoding ascii deps.txt
uv run --no-project --python 3.12 --with pip python -m pip download -r deps.txt -d wheels `
  --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp
Copy-Item dist\nunchaku-0.1.0-py3-none-any.whl wheels\
```

把整个 `wheels` 目录（约 30 个 wheel）复制到共享位置。有内网 PyPI 镜像时，在 `pip download` 后面加 `--index-url <镜像地址>`。员工电脑还需要 Python 3.12，可以由 IT 统一安装，或者把 `uv python install 3.12` 的结果一起分发。

### 5.2 员工安装 CLI

员工电脑需要先装 `uv`（可由 IT 统一分发）。然后：

```powershell
# 能访问 PyPI 或内网镜像时
uv tool install \\fileserver\tools\nunchaku\nunchaku-0.1.0-py3-none-any.whl

# 完全离线时（wheels 目录见 5.1）
uv tool install --offline --no-index --find-links \\fileserver\tools\nunchaku\wheels --python 3.12 nunchaku

nunchaku --help
```

安装后可执行文件在 `C:\Users\<用户名>\.local\bin\nunchaku.exe`，下文称 **nunchaku 路径**。

### 5.3 安装通用连接 Skill

```powershell
nunchaku install-skill --url https://nunchaku.corp.example.com/mcp
```

按提示选 AIKO（通常是 `C:\Users\<用户名>\.sdw\skills`）。也可以直接指定：

```powershell
nunchaku install-skill --path "$HOME\.sdw\skills"
```

这个 Skill 不含任何服务信息，**一台电脑只需装一次**，以后新增服务也不用重装。

### 5.4 在 AIKO 中添加 MCP

1. 打开 AIKO 的 MCP 面板，点「+」。
2. **名称**填 `nunchaku`（连接 Skill 里就是这样称呼它的，请不要改）。
3. **类型**选 **STDIO**。
4. **配置**填写（把用户名、地址和项目目录换成自己的）：

```json
{
  "command": "C:\\Users\\zhangsan\\.local\\bin\\nunchaku.exe",
  "args": [
    "bridge",
    "--url",
    "https://nunchaku.corp.example.com/mcp",
    "--root",
    "D:\\work\\docs"
  ],
  "env": {}
}
```

5. 点「测试」，应显示服务器上的工具（如 `md2docx`）。通过后点「添加」。

注意事项：

- **JSON 里的路径要写双反斜杠** `\\`，或者全部改用正斜杠 `/`。
- **`command` 写 `.exe` 的完整路径**，不要只写 `nunchaku`，AIKO 拉起子进程时不一定能找到 PATH 里的命令。
- **`--root` 是读写边界**：桥接器只能读写这个目录下的文件，路径一律相对于它。请填你平时存放待处理文档的目录，**不要填盘符根目录或用户主目录**。
- 想处理多个目录时，可以填它们的公共父目录；或者再加一条 MCP（名称如 `nunchaku-proj2`），这时会话里需要点名使用。

### 5.5 一键安装脚本（可选）

管理员可以把下面的脚本放到共享目录，员工双击或在 PowerShell 中执行：

```powershell
# install-nunchaku.ps1
$Share   = "\\fileserver\tools\nunchaku"
$Gateway = "https://nunchaku.corp.example.com/mcp"

$wheel = Get-ChildItem "$Share\nunchaku-*.whl" | Sort-Object Name | Select-Object -Last 1
uv tool install --force $wheel.FullName
if (-not $?) { Write-Error "安装失败。如果提示拒绝访问，请先退出 AIKO 后重试。"; exit 1 }

& "$HOME\.local\bin\nunchaku.exe" install-skill --path "$HOME\.sdw\skills" --url $Gateway --force

Write-Host ""
Write-Host "安装完成。请在 AIKO 的 MCP 面板中添加名为 nunchaku 的 STDIO 服务器："
Write-Host "  command: $HOME\.local\bin\nunchaku.exe"
Write-Host "  args:    bridge --url $Gateway --root <你的文档目录>"
```

---

## 6. 员工使用

### 6.1 直接用自然语言

在 AIKO 会话里正常描述需求即可，AIKO 会根据服务描述自动选择：

```
把 设计方案.md 转成 docx
```

也可以点名服务：

```
用 nunchaku 的 md2docx 把 docs/周报.md 转成 Word
```

一次调用通常需要几十秒到一两分钟，结果文件会直接写进 `--root` 目录。

### 6.2 路径写法

文件路径必须是**相对于 `--root` 的路径**：

| 写法                              | 结果                        |
| --------------------------------- | --------------------------- |
| `设计方案.md`、`docs/周报.md` | ✅ 正确                     |
| `./设计方案.md`                 | ❌ 不能以 `./` 开头       |
| `D:\work\docs\设计方案.md`      | ❌ 不能是绝对路径           |
| `docs\周报.md`                  | ❌ 要用 `/`，不能用 `\` |
| `../其他目录/a.md`              | ❌ 不能跳出 `--root`      |

### 6.3 先看结果再写入

在需求里加一句"**先不要写入，只给我看结果**"。此时文件不会写到项目中，而是放在 `--root` 下的 `.nunchaku\proposed\` 目录，确认后再自行处理。

### 6.4 文件冲突

如果目标文件已存在（比如之前生成过同名的 docx），或者在转换期间被改动过，桥接器**不会覆盖**，会把它列在冲突里交给 AIKO 处理。想重新生成同名文件，先删除旧文件或换个文件名。

### 6.5 使用限制

| 项         | 限制                                                         |
| ---------- | ------------------------------------------------------------ |
| 单次文件数 | 最多 50 个                                                   |
| 单个文件   | 10 MB                                                        |
| 单次总量   | 20 MB                                                        |
| 单次耗时   | 由服务配置决定，默认 180 秒                                  |
| 上下文     | 每次调用独立，不记得上一次。需要继续时，把必要信息重新说一遍 |

---

## 7. 日常运维

### 7.1 新增服务

1. 把私有 Skill 放到服务器，比如 `/srv/private-skills/translate/`。
2. 在 `services.json` 的 `services` 里加一项，端口用新的（如 8766）。
3. 校验并重启服务进程：

   ```bash
   uv run nunchaku --config services.json list
   sudo systemctl restart nunchaku-services
   ```
4. **网关不需要重启**，它会自动发现配置变化。
5. 通知员工：**在 AIKO 中重启 `nunchaku` 这条 MCP 连接**，就能看到新工具。

### 7.2 修改或下线服务

- 修改描述、超时等：改 `services.json` 后 `systemctl restart nunchaku-services`。
- 下线：从 `services.json` 删除后重启。员工下次重连时该工具消失；在此之前调用会得到 `unknown service`。

### 7.3 升级 nunchaku

服务器：

```bash
cd /opt/nunchaku
git pull
uv sync
sudo systemctl restart nunchaku-services nunchaku-gateway
```

员工电脑：重新构建并分发 wheel，员工执行 `uv tool install --force <新 wheel>`。**升级前必须先退出 AIKO**，否则会报"拒绝访问"（见 8.1）。

### 7.4 日志

| 进程         | 查看方式                                                                                 |
| ------------ | ---------------------------------------------------------------------------------------- |
| 服务进程     | `journalctl -u nunchaku-services -f`；用管理台启动时在 `.nunchaku/logs/<服务名>.log` |
| 网关         | `journalctl -u nunchaku-gateway -f`；用管理台启动时在 `.nunchaku/logs/_gateway.log`  |
| 员工端桥接器 | 写到 stderr，在 AIKO 的 MCP 日志中查看                                                   |

一次正常调用在服务端的日志大致如下：

```
10:20:06 INFO    [md2docx] call started: tool=md2docx, 1 file(s), task='把 设计方案.md 转成 docx'
10:20:06 INFO    [md2docx] run started: 1 input file(s), 11292 bytes, task='把 设计方案.md 转成 docx'
10:20:06 INFO    [md2docx] engine started: /usr/local/bin/aiko (timeout 180s)
10:20:54 INFO    [md2docx] engine finished in 48.2s with code 0
10:20:54 INFO    [md2docx] run finished in 48.3s: 2 changed, 0 deleted, answer 544 chars
10:20:54 INFO    [md2docx] call finished in 48.4s: 2 changed, 0 deleted
```

需要更多细节时，在启动命令里加 `--log-level DEBUG`（如 `nunchaku --log-level DEBUG --config services.json gateway`），会逐个列出改动的文件。

日志中会记录任务描述的前 60 个字符。如果任务内容敏感，可用 `--log-level WARNING` 只保留告警和错误。

---

## 8. 常见问题

### 8.1 员工端

**升级时报 `failed to remove directory ...\uv\tools\nunchaku\Scripts: 拒绝访问`**
AIKO 正在运行桥接器，占用了这个目录。完全退出 AIKO（包括托盘图标）后重试。如果之后运行 `nunchaku` 报 `No module named 'nunchaku'`，说明上次安装中途失败，退出 AIKO 后重新执行 `uv tool install --force <wheel>` 即可。

**AIKO 测试连接失败，stderr 里有 `cannot reach gateway`**
连不上网关。先用 3.3 的 `curl` 在本机测试；检查地址、端口、防火墙、代理。如果公司有 HTTP 代理，把网关域名加入 `NO_PROXY`，例如在 MCP 配置的 `env` 里写 `{"NO_PROXY": "nunchaku.corp.example.com"}`。

**`project root is not a directory`**
`--root` 填的目录不存在，常见原因是把示例路径原样留在了配置里。

**`not a regular file: xxx.md`**
文件不在 `--root` 目录下，或者相对路径写错。

**`file path must be a non-empty POSIX relative path`**
路径以 `./` 开头、用了反斜杠或盘符。见 6.2。

**工具列表里看不到新服务**
在 AIKO 里重启 `nunchaku` 这条 MCP 连接。仍然没有的话，请管理员确认该服务已启动。

**调用超时**
服务端单次最长约 180 秒。如果 AIKO 支持设置 MCP 工具超时，请设为 210 秒以上。

**报错里的中文显示为乱码**
是 stderr 编码不一致，不影响功能。

### 8.2 HTTPS 证书

**`certificate verify failed`**
网关用的是公司内部 CA 签发的证书，员工电脑的 Python 不认识。在 AIKO 的 MCP 配置里指定 CA 文件：

```json
"env": {"SSL_CERT_FILE": "C:\\certs\\corp-root-ca.pem"}
```

### 8.3 服务器端

**Nginx 返回 `421 Misdirected Request`**
没有按 3.1 设置 `proxy_set_header Host 127.0.0.1:8750;`。

**Nginx 返回 `403`，网关日志里有 `Invalid Origin header`**
没有按 3.1 设置 `proxy_set_header Origin "";`。

**Nginx 返回 `413 Request Entity Too Large`**
没有调大 `client_max_body_size`。

**大约 60 秒后返回 `504`**
没有调大 `proxy_read_timeout`。

**调用报 `remote skill failed: ... engine timed out after 180s`**
执行引擎没在时限内完成。可以调大该服务的 `timeout_seconds`（同时调大引擎自身的超时、Nginx 的 `proxy_read_timeout`、员工端 AIKO 的工具超时），或优化 Skill。

**调用报 `remote skill failed: ... exited 1`**
执行引擎运行失败。查看服务日志里 `engine exited` 那一行附带的错误输出。常见原因是服务账号下引擎未登录、模型凭据缺失或无权限。

**`engine executable unavailable`**
`command` 填的路径在服务器上不存在，或服务账号没有执行权限。

**`gateway port ... is already used by a service`**
网关端口和某个服务的端口重复了。

---

## 附录：员工快速上手卡

> 可以直接转发给员工。

1. 安装：`uv tool install \\fileserver\tools\nunchaku\nunchaku-0.1.0-py3-none-any.whl`
2. 装 Skill：`nunchaku install-skill --path "$HOME\.sdw\skills"`
3. AIKO → MCP → 「+」→ 名称 `nunchaku`，类型 STDIO，配置：

   ```json
   {
     "command": "C:\\Users\\<你的用户名>\\.local\\bin\\nunchaku.exe",
     "args": ["bridge", "--url", "https://nunchaku.corp.example.com/mcp", "--root", "D:\\<你的文档目录>"],
     "env": {}
   }
   ```
4. 测试 → 添加。
5. 会话中直接说：「把 xxx.md 转成 docx」。文件名写相对路径，不要加 `./`。
6. 管理员通知有新服务时：在 AIKO 中重启 `nunchaku` 连接。
7. 升级前先退出 AIKO。
