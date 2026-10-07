# ADR-3：shell 的现状与边界

## 状态

接受（已知缺口）

## 背景

`map_agent` 要执行 `node scripts/...`。`StateBackend` 没有 `execute`，地图步骤会一直拖到调度超时。换成 `LocalShellBackend` 之后命令才能跑通。工作目录固定在 `amap-lbs-skill/`，和 SKILL.md 里的相对路径一致。

`LocalShellBackend` 不是容器。`virtual_mode` 只约束文件 API，挡不住 shell 访问宿主机任意路径。

## 决策

`inherit_env=False`。子进程只拿到启动 `node` 所需的 `PATH`、`PATHEXT`、`SYSTEMROOT`、`COMSPEC`、`TEMP`、`TMP`，以及 `AMAP_KEY`（没有 `AMAP_KEY` 时用 `AMAP_WEBSERVICE_KEY` 填入）。`OPENAI_API_KEY` 和其他密钥不进入该环境。高德脚本目录对文件工具只读。

## 后果

模型读 `process.env.OPENAI_API_KEY` 这条路被切断。shell 仍能读磁盘上的 `.env` 文件，也能访问网络。要挡住文件和网络，需要换到容器或虚拟机里执行，而不是再包一层 Python 包装。
