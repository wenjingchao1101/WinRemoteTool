# WinAuto

WinAuto 是一个 ADB 风格的 Windows 命令执行工具。一个 Python CLI 同时提供本地执行和远程 Agent，适合自动化脚本、测试机和局域网内的 Windows 设备管理。

## 代码目录

```text
winauto.py              CLI、Agent 和命令分发
winauto_modules/
  protocol.py                 长度前缀 JSON 通信
  file_transfer.py            文件枚举、分块读取和路径安全
tests/                        单元测试
```

## 快速开始

启动本机 Agent（默认只监听 `127.0.0.1`）：

```powershell
python .\winauto.py agent
```

在另一个终端执行本地 CMD 命令：

```powershell
python .\winauto.py cmd ipconfig
python .\winauto.py cmd dir C:\\
python .\winauto.py cmd "set MY_VALUE=hello && echo %MY_VALUE%"
```

`cmd` 子命令会使用 `cmd.exe /d /s /c` 执行，因此支持 CMD 内置命令、管道、重定向和 `&&`。也兼容常见的 `/c` 写法：

```powershell
python .\winauto.py cmd /c "echo hello && whoami"
```

通用 `exec` 命令默认也是 CMD：

```powershell
python .\winauto.py exec -- ipconfig
python .\winauto.py exec --shell cmd -- whoami
python .\winauto.py exec --shell powershell -- Get-Process
python .\winauto.py exec --shell raw -- python -c "print('hello')"
```

从远程 Agent 拉取文件或目录：

```powershell
# 拉取单个文件到当前目录，文件名保持不变
python .\winauto.py -s 127.0.0.1:27889 pull "C:\\Logs\\app.log"

# 指定本地文件名
python .\winauto.py -s 127.0.0.1:27889 pull "C:\\Logs\\app.log" .\downloads\\app.log

# 递归拉取目录
python .\winauto.py -s 127.0.0.1:27889 pull "C:\\Logs" .\downloads\\logs
```

文件传输按 64 KiB 分块进行，下载到 `.winauto.part` 临时文件，完成 SHA-256 和大小校验后再替换目标文件。

向远程 Agent 上传文件或目录：

```powershell
# 上传单个文件到 Agent 当前目录
python .\winauto.py -s 127.0.0.1:27889 push .\config.json

# 上传单个文件并指定远端文件名
python .\winauto.py -s 127.0.0.1:27889 push .\config.json "C:\\App\\config.json"

# 上传单个文件到远端目录，保留本地文件名
python .\winauto.py -s 127.0.0.1:27889 push .\config.json "C:\\App\\"

# 递归上传目录，远端参数作为目标目录
python .\winauto.py -s 127.0.0.1:27889 push .\logs "C:\\App\\logs"
```

上传同样按 64 KiB 分块进行，Agent 会先写入 `.winauto.part`，校验大小和 SHA-256 后再替换远端目标文件。当前 Agent 无认证，上传会覆盖远端同名文件，请只在可信网络使用。

通过 Agent 执行远程命令：

```powershell
python .\winauto.py connect 127.0.0.1:27889
python .\winauto.py -s 127.0.0.1:27889 exec "ipconfig /all"
python .\winauto.py -s 127.0.0.1:27889 cmd "echo hello && whoami"
python .\winauto.py -s 127.0.0.1:27889 exec --shell powershell -- Get-Date
python .\winauto.py devices
```

`connect` 会完成 Agent 握手和 `ping` 检查，并把目标记录到本机配置；它不会保持一个永久 TCP 连接。后续 `-s HOST:PORT` 命令会按目标建立短连接并执行。客户端目标选择统一使用 `-s`。

参数放在 `--` 后面，避免被 WinAuto 自己的参数解析器误读。`--program` 可以直接启动任意程序：

```powershell
python .\winauto.py exec --program C:\Tools\job.exe -- --mode batch
```

## Agent 网络约束

- 默认绑定回环地址，不接受外部机器连接。
- 当前版本不包含 Token 或其他身份认证。
- 传输协议使用长度前缀 JSON；输出数据使用 Base64 保持字节完整。
- 监听 `0.0.0.0` 后，任何能够访问端口的机器都可以执行命令。只能在受信任且有防火墙隔离的网络使用，不能直接暴露到公网。

## 打包 EXE

安装依赖并执行打包：

```powershell
python -m pip install -r .\requirements.txt
python .\build.py
```

构建脚本会清理本项目之前生成的 `build` 目录和旧的 `dist\winauto.exe`，并检查新 EXE 的帮助中不再包含 `--token`。生成文件位于 `dist\winauto.exe`。可以在目标机上运行：

```powershell
.\winauto.exe agent --host 0.0.0.0 --port 27889
```

建议后续把 Agent 注册为 Windows Service。远程部署前应使用 Windows 防火墙限制来源 IP，并增加 TLS、命令授权策略、审计日志和 ConPTY 交互式终端。

## 协议概要

每个请求连接先发送：

```json
{"type":"hello","client_version":"0.2.0"}
```

握手成功后发送：

```json
{"operation":"exec","shell":"cmd","command":["ipconfig"],"timeout_ms":30000}
```

Agent 按顺序返回 `stdout`、`stderr` 和 `exit` 消息。`exit.code` 是目标进程退出码，超时会将 `timed_out` 设置为 `true`，并使用退出码 `124`。

上传文件时请求为：

```json
{"operation":"push","remote_path":"C:\\App\\config.json","kind":"file","name":"config.json"}
```

随后客户端发送 `push_file`、多个 `push_chunk`、`push_file_end`，最后发送 `push_done`；Agent 返回 `push_ready` 和 `push_done`。

拉取文件时请求为：

```json
{"operation":"pull","remote_path":"C:\\Logs\\app.log"}
```

Agent 返回 `pull_start`、多个 `pull_file`/`pull_chunk`/`pull_file_end`，最后返回 `pull_done`。目录会递归传输，客户端会校验每个文件的大小和 SHA-256。
