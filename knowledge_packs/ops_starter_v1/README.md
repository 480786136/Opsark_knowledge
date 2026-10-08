# 运维基础参考知识包 v1

准备日期：2026-10-02。共 20 篇参考知识，来源为项目官方文档，均为待审核资料，不是本机历史任务，也没有执行其中的命令。

直接阅读 [完整知识正文](KNOWLEDGE.md)。机器可读内容在 [catalog.json](catalog.json)，检索问题在 [queries.json](queries.json)，首次实测在 [baseline-2026-10-02.json](baseline-2026-10-02.json)。

## 内容覆盖

| 分类 | 知识条目 |
| --- | --- |
| Linux | 磁盘容量与 inode；目录占用与 du/df 差异 |
| systemd | 服务启动失败；journal 用量与日志窗口 |
| Nginx | 配置检查；上游连接与读取超时 |
| Docker | 退出与 OOM；有限日志采集；镜像/容器/卷占用 |
| Kubernetes | Pending；CrashLoopBackOff；ImagePullBackOff；Service 端点；DiskPressure |
| PostgreSQL | 活跃会话与长事务；阻塞与锁等待 |
| Redis | 内存上限与淘汰策略 |
| TLS / curl | 证书有效期；证书验证与 CA 信任 |
| Python | macOS/Linux 和 Windows 的虚拟环境路径 |

每篇包含适用条件、问题、诊断解释、低影响检查示例、验收要求、风险及官方来源。NS、POD、CONTAINER、UNIT.service、CERT.pem 等是占位符；使用前必须核对目标、版本、权限和业务影响。只读检查也可能产生 I/O 或外部请求。

版本范围是参考文档范围，不是目标机器的实际版本，因此不伪填环境或精确软件版本。PostgreSQL 参考 17，OpenSSL 参考 3.0，systemd 使用上游手册，其他链接可能持续更新。使用前复核现场版本，尤其是 Kubernetes 运行时行为。

## 离线检查与隔离验收

在 `Opsark_knowledge` 根目录执行（macOS/Linux，已安装项目及 test 依赖）：

```sh
.venv/bin/python scripts/knowledge_pack.py
.venv/bin/python scripts/evaluate_knowledge_pack.py
.venv/bin/python -m pytest -q tests/test_knowledge_pack.py
```

第一条只验证本地文件，不登录服务、不写数据库。第二条新建临时 SQLite，运行真实迁移、HTTP 处理器、发布 Worker、公开检索和引用接口；结束删除该临时数据库。不连接正在运行的服务，不使用真实模型或 Embedding，不执行知识中的命令。

可用 `--output /private/tmp/knowledge-evaluation-new.json` 保存新的报告；拒绝覆盖已有文件。Windows 将解释器改为 `.venv/Scripts/python.exe`，报告路径改为可写的本机路径。

## 导入本机独立测试库：只创建草稿

先在管理界面新建独立知识库，例如“运维参考知识（待审核）”，取得其 ID，然后执行：

```sh
.venv/bin/python scripts/knowledge_pack.py --apply --base-id TEST_BASE_ID
.venv/bin/python scripts/knowledge_pack.py \
  --url http://127.0.0.1:8002 \
  --apply \
  --base-id fc6f16fb309f409594313f967304576f
```

将 `TEST_BASE_ID` 换成真实 ID；默认地址为 `http://127.0.0.1:8002`、用户名为 `admin`，密码交互输入且不保存。可用 `--username` 指定其他本机知识管理员。仅接受 `127.0.0.1` / `[::1]`，不跟随重定向、不使用代理环境变量。

工具通过管理员 API 创建手工草稿，不创建伪造来源记录，不入 AI 提炼队列，不发布、不修改已有知识。管理员需审核后逐篇发布，未发布草稿不能被 Core 检索。

串行重跑通过固定条目标记识别相同内容并跳过；发现人工修改、同标记不同内容或重复条目时停止，并在首次创建前检查全部已有冲突。网络中断可能部分完成，已创建草稿不会回滚；检查后串行重跑。当前 API 没有手工建文档的服务端幂等键，请勿并行导入同一知识包，也不要删除条目标记后重跑。

正式使用前另行审核来源许可、脱敏和组织知识保留要求。没有全自动发布开关是有意保留的审核边界。

## 首次检索结果与限制

2026-10-02：20 篇资料，50 个固定问题，SQLite 关键词模式，模型关闭。

- 库内及适用过滤问题：42 个，Top-1 命中 40 个，Top-5 命中 41 个。
- 预设检查标记出现在目标文档返回片段中：31/42。该指标只检查字符串，不代表人工确认答案正确。
- 纯库外问题：6 个，4 个仍返回无关资料。另 2 个不匹配路径/软件约束问题均返回空。
- 功能边界通过：草稿不可检索、重复导入不重复建文档、手工导入不调用 AI、Worker 发布、输出预算、引用行号、下架撤回引用。

这是一批小型起始验收资料，不是客户问题分布或生产容量压测。分数包含明显软件名称的提问，同义改写难度有限；后续应加入真实问法、长问题、混合语言、跨版本和负例。当前同分切片可能受随机 ID 影响，报告保存的是此次实测，不保证每次排名完全一致。

不要为了追求满分改写问题；先保留失败样本，再优化检索。产品补强顺序见 [完整性审查与增强计划](../../docs/知识库完整性审查与增强计划.md)。
