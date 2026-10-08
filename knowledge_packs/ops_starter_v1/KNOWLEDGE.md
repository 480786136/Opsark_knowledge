# 运维参考知识包 v1

# Linux 磁盘空间与 inode 耗尽排查

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:linux-disk；来源核对：2026-10-02。

## 适用范围

GNU Coreutils；以下 -T 参数不适用于 macOS BSD df。

## 问题与诊断

写入报 No space left on device，但文件系统剩余字节与 inode 可能是两类问题。先确认报错文件实际所在挂载点。

比较同一挂载点的容量使用率和 inode 使用率。容量充足而 inode 接近耗尽时，调查大量小文件；不要仅凭应用错误判定磁盘已满。

## 低影响检查示例（未执行）

```text
df -hT /var/log
df -i /var/log
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录挂载点、采集时间、容量和 inode 两组结果；变更后重新检查并验证原业务写入。本文未执行这些检查。

## 风险与禁止自动操作

只读取统计；网络挂载可能阻塞。不要据此自动删除日志、扩大权限或格式化文件系统。

## 官方参考

- [来源 1](https://www.gnu.org/software/coreutils/manual/html_node/df-invocation.html)

---

# Linux 目录占用定位与 du、df 差异

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:linux-du；来源核对：2026-10-02。

## 适用范围

GNU du；只在已获授权的目标目录内调查。

## 问题与诊断

文件系统使用率高，需要定位 /var/log 下的目录占用；du 与 df 统计对象不同，不应要求数值严格相同。

-x 不跨入其他文件系统，深度 1 只限制展示层级而不限制遍历深度。权限拒绝会造成统计不完整；快照、共享块等也可能影响空间解释。

## 低影响检查示例（未执行）

```text
du -x -h --max-depth=1 /var/log
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

保留扫描范围、错误输出和大目录列表；进一步调查实际文件归属与保留要求，不能仅凭目录大小判定可删除。

## 风险与禁止自动操作

遍历可产生大量 I/O，业务高峰先缩小目录范围；不直接对根目录做全盘扫描，不自动清理。

## 官方参考

- [来源 1](https://www.gnu.org/s/coreutils/manual/html_node/du-invocation.html)

---

# systemd 服务启动失败与状态证据

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:systemd-failed；来源核对：2026-10-02。

## 适用范围

采用 systemd 的 Linux；UNIT.service 须替换为确认过的单个服务名。

## 问题与诊断

服务显示 failed 或启动后退出，先区分当前状态、最近退出状态与历史日志。

status 适合人工阅读；show 适合结构化读取。当前 active 不能证明历史启动都成功，历史问题需要匹配相应时间窗口的日志。

## 低影响检查示例（未执行）

```text
systemctl status UNIT.service --no-pager --full --lines=30
systemctl show UNIT.service --property=ActiveState,SubState,Result,ExecMainStatus
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

收集 ActiveState、SubState、Result、退出状态与故障时间；修复验收还需业务健康检查，不能把进程存在当作业务恢复。

## 风险与禁止自动操作

不执行 restart、reset-failed 或修改单元。输出可能包含内部路径与日志，上传前脱敏；权限不足不等于服务无故障。

## 官方参考

- [来源 1](https://raw.githubusercontent.com/systemd/systemd/main/man/systemctl.xml)

---

# systemd journal 日志占用与时间窗口采集

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:journal-space；来源核对：2026-10-02。

## 适用范围

systemd journal；可见范围由当前用户权限决定。

## 问题与诊断

需要判断 journal 日志占用，或提取单个服务最近十分钟的错误上下文。

磁盘用量包含活动和归档 journal。输出为空也可能是权限、过滤时间或单元名不匹配。清理归档不能保证活动文件立即缩小。

## 低影响检查示例（未执行）

```text
journalctl --disk-usage
journalctl -u UNIT.service --since '10 minutes ago' -n 200 --no-pager
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录用量、时间范围、服务名和读取权限；日志只提供当时证据，不代表故障已修复。

## 风险与禁止自动操作

不执行 vacuum、rotate 或清空日志。保留事故证据和审计留存要求，日志摘录需脱敏。

## 官方参考

- [来源 1](https://raw.githubusercontent.com/systemd/systemd/main/man/journalctl.xml)

---

# Nginx 配置语法与引用文件检查

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:nginx-config；来源核对：2026-10-02。

## 适用范围

现场 Nginx 版本；需确认实际二进制、配置入口和读取权限。

## 问题与诊断

变更准备阶段或启动失败时，先检查配置解析与引用文件是否可访问，不立即重载。

-t 会检查配置语法并尝试打开引用的文件。非零退出先看具体报错；权限问题不能误判成语法错误。检查成功不代表上游可达或新配置已经生效。

## 低影响检查示例（未执行）

```text
nginx -v
nginx -t -c /etc/nginx/nginx.conf
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录命令、退出码、错误位置和实际配置入口。后续获批重载后还需验证请求、上游和日志。

## 风险与禁止自动操作

配置测试可能访问日志、证书等文件；不自动提权或重载。避免直接上传 nginx -T 的完整配置转储。

## 官方参考

- [来源 1](https://nginx.org/en/docs/switches.html)

---

# Nginx 上游连接与读取超时排查

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:nginx-timeout；来源核对：2026-10-02。

## 适用范围

ngx_http_proxy_module；以现场有效配置为准。

## 问题与诊断

请求出现 upstream timed out 或 504，需区分建立上游连接与等待读取响应的阶段。

对齐错误日志时间、请求关联标识和上游服务日志，再人工核对对应 location 的 proxy_connect_timeout 与 proxy_read_timeout。读取超时是两次连续读取之间的等待限制，不是整段响应的总耗时。

## 低影响检查示例（未执行）

```text
nginx -t -c /etc/nginx/nginx.conf
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

确认超时阶段、目标上游和请求范围；修复后检查业务响应及尾延迟，不以单次配置测试代替请求验收。

## 风险与禁止自动操作

不能统一调大超时掩盖容量或网络问题。重载配置、探测生产接口都需确认授权与请求副作用；日志须脱敏。

## 官方参考

- [来源 1](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)

---

# Docker 容器异常退出与 OOM 状态

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:docker-exit；来源核对：2026-10-02。

## 适用范围

Docker Engine CLI；CONTAINER 替换为确认过的容器 ID。

## 问题与诊断

容器 Exited 或反复重启，先记录退出码、OOM 标志、错误与重启次数，不靠重启消除现场。

将 State.ExitCode、State.OOMKilled、State.Error 与日志时间关联。单独的 137 退出码不能排他证明是内存不足；OOMKilled 字段也不解释具体内存增长原因。

## 低影响检查示例（未执行）

```text
docker inspect --type=container --format '{{json .State}}' CONTAINER
docker inspect --type=container --format '{{.RestartCount}}' CONTAINER
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

收集退出状态、时间与受限日志；定位后需要观察持续运行和业务健康，而非只看到 running。

## 风险与禁止自动操作

不执行 restart、rm 或修改内存限制；完整 inspect 可能含环境变量凭据，优先限定输出字段。

## 官方参考

- [来源 1](https://docs.docker.com/reference/cli/docker/inspect/)
- [来源 2](https://docs.docker.com/reference/cli/docker/container/run/)

---

# Docker 容器日志的有限窗口采集

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:docker-logs；来源核对：2026-10-02。

## 适用范围

支持读取日志的 Docker 日志驱动；确认容器身份和故障时区。

## 问题与诊断

需要收集故障前后标准输出和标准错误，避免一次拉取全部历史日志。

结合时间戳和容器生命周期判断错误顺序。无输出不能证明无错误，需核对日志驱动及应用是否输出到 stdout/stderr。

## 低影响检查示例（未执行）

```text
docker logs --since 10m --tail 200 --timestamps CONTAINER
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

保留容器 ID、采集时间窗口和截断说明；200 行只是样本，必要时经授权扩大窗口。

## 风险与禁止自动操作

日志可能含口令、请求参数和客户数据，必须脱敏。不要使用 --details 扩大敏感属性暴露，不开启无限跟随作为批处理。

## 官方参考

- [来源 1](https://docs.docker.com/reference/cli/docker/container/logs/)

---

# Docker 镜像、容器与卷的磁盘占用盘点

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:docker-space；来源核对：2026-10-02。

## 适用范围

Docker Engine；读取 daemon 信息需要受控的 Docker 访问权限。

## 问题与诊断

Docker 数据目录占用增大，先区分镜像、容器、卷和构建缓存，不直接清理。

比较共享与独占大小，核对可回收项的实际归属。可回收统计不等于业务授权删除，也不能单凭汇总识别全部宿主机磁盘占用。

## 低影响检查示例（未执行）

```text
docker system df
docker system df -v
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录类别、资源标识及归属；清理方案另行审核，验收同时检查文件系统容量和业务资源完整性。

## 风险与禁止自动操作

-v 可能引发较多文件系统扫描；繁忙 daemon 应先看摘要。严禁自动执行 prune、删卷或删除 Docker 数据目录。

## 官方参考

- [来源 1](https://docs.docker.com/reference/cli/docker/system/df/)

---

# Kubernetes Pod Pending 与调度事件

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:k8s-pending；来源核对：2026-10-02。

## 适用范围

kubectl 与集群兼容；NS、POD 为占位符，先确认当前 context。

## 问题与诊断

Pod 长时间 Pending，先区分尚未调度与调度后初始化问题。

检查 PodScheduled 条件和 Events。FailedScheduling 时根据明确事件调查资源请求、约束或存储；不要把所有 Pending 都归为 CPU 不足。

## 低影响检查示例（未执行）

```text
kubectl config current-context
kubectl -n NS get pod POD -o wide
kubectl -n NS describe pod POD
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录调度条件、节点分配及最新事件；解决后既要检查 Ready，也要验证工作负载可用。

## 风险与禁止自动操作

describe 可能包含敏感的明文环境值与内部地址，上传前脱敏。不自动降低 requests、删除 PVC 或绕过节点约束。

## 官方参考

- [来源 1](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)

---

# Kubernetes CrashLoopBackOff 与上次容器日志

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:k8s-crashloop；来源核对：2026-10-02。

## 适用范围

明确 NS、POD、CONTAINER；多容器 Pod 必须指定目标容器。

## 问题与诊断

容器反复退出并出现 CrashLoopBackOff，重点收集上次实例的退出信息。

CrashLoopBackOff 描述重启退避，不是 Pod phase。关联 Last State、Reason、Exit Code、重启次数和上次日志；--previous 没有可用实例时可能报错。

## 低影响检查示例（未执行）

```text
kubectl -n NS describe pod POD
kubectl -n NS logs POD -c CONTAINER --previous --tail=200 --timestamps
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

依据证据区分应用异常、探针问题和资源限制；恢复后观察一段约定窗口的重启次数、Ready 与业务检查。

## 风险与禁止自动操作

不靠反复删除 Pod 掩盖原因，不直接放宽探针或权限。日志与 describe 输出须脱敏。

## 官方参考

- [来源 1](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)
- [来源 2](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)

---

# Kubernetes ImagePullBackOff 镜像拉取失败

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:k8s-imagepull；来源核对：2026-10-02。

## 适用范围

确认命名空间、镜像引用、仓库可达性与凭据归属。

## 问题与诊断

Pod 显示 ErrImagePull 或 ImagePullBackOff，需要从事件区分镜像不存在、权限失败和连接问题。

先读取拉取错误原文，再核对仓库地址、标签或摘要。imagePullSecrets 必须位于同一命名空间；名称存在不代表凭据有效。

## 低影响检查示例（未执行）

```text
kubectl -n NS describe pod POD
kubectl -n NS get pod POD -o jsonpath='{.spec.imagePullSecrets[*].name}'
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录错误类别与镜像标识；后续验证镜像已拉取且容器 Ready，避免仅看到重试次数增加。

## 风险与禁止自动操作

不输出 Secret 内容，不把仓库密码放命令行或上传；不切换未知镜像仓库、不关闭 TLS 校验。

## 官方参考

- [来源 1](https://kubernetes.io/docs/concepts/containers/images/)

---

# Kubernetes Service 无端点与 targetPort 排查

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:k8s-service；来源核对：2026-10-02。

## 适用范围

针对有 selector 的常规 Service；无 selector、ExternalName 需另行分析。

## 问题与诊断

Service 无法连通，先确认匹配的 Pod、EndpointSlice 和端口映射。

核对 Service selector 与 Pod labels，再核对 targetPort 名称或数字与容器监听端口。端点为空时检查选择器和 Pod 就绪状况；端点存在不证明网络策略允许通信。

## 低影响检查示例（未执行）

```text
kubectl -n NS get service SERVICE -o yaml
kubectl -n NS get endpointslices -l kubernetes.io/service-name=SERVICE
kubectl -n NS describe service SERVICE
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

保留 Service、端点和 Ready 证据；主动请求测试需要选择无副作用且获授权的业务探针。

## 风险与禁止自动操作

不自动改 selector 或端口；输出中的注解和内部地址可能敏感。不要把跨命名空间对象混为同一服务。

## 官方参考

- [来源 1](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/)

---

# Kubernetes 节点 DiskPressure 与 inode 压力

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:k8s-pressure；来源核对：2026-10-02。

## 适用范围

有节点状态读取权限；不同运行时和版本的文件系统布局可能不同。

## 问题与诊断

节点出现 DiskPressure 或驱逐，需区分磁盘容量不足和 inode 压力。

检查 Conditions、污点与驱逐事件。DiskPressure 可涉及 nodefs、imagefs、containerfs 的空间或 inode 信号，实际支持取决于版本与运行时；再由获授权人员核对节点文件系统。

## 低影响检查示例（未执行）

```text
kubectl describe node NODE
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录压力信号和时间，与被驱逐 Pod 关联；恢复后验证压力状态、节点可调度性和业务副本。

## 风险与禁止自动操作

不直接删容器运行时目录、prune、drain 或调整驱逐阈值；这些变更可能影响多个工作负载。

## 官方参考

- [来源 1](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/)

---

# PostgreSQL 活跃会话与长事务观察

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:pg-sessions；来源核对：2026-10-02。

## 适用范围

参考 PostgreSQL 17；其他大版本需复核字段与权限。

## 问题与诊断

连接堆积或事务长期不结束时，先读取会话状态和等待信息，不输出 SQL 正文。

state 为 active 不表示一定在占用 CPU，还应结合 wait_event。普通账号对其他会话的可见字段可能受限；长时间持有的事务快照也会影响观察。

## 低影响检查示例（未执行）

```text
SELECT pid, state, wait_event_type, wait_event, xact_start, query_start
FROM pg_stat_activity
WHERE backend_type = 'client backend'
ORDER BY xact_start NULLS LAST LIMIT 30;
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录观察时间、会话状态与等待类型；idle in transaction 需要核对应用事务边界，不直接推断根因。

## 风险与禁止自动操作

不执行 pg_terminate_backend，不输出 query 文本或授予超级用户权限；必要时申请最小监控权限。

## 官方参考

- [来源 1](https://www.postgresql.org/docs/17/monitoring-stats.html)

---

# PostgreSQL 阻塞会话与锁等待定位

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:pg-locks；来源核对：2026-10-02。

## 适用范围

参考 PostgreSQL 17；受控监控账号，只观察，不终止会话。

## 问题与诊断

请求等待锁时，需要识别被阻塞 PID 与阻塞者，避免靠猜测结束事务。

pg_blocking_pids 有助于识别阻塞关系，再结合 pg_locks 与事务时间分析。一次快照可能错过短暂等待，重复采样应控制频率。

## 低影响检查示例（未执行）

```text
SELECT pid, pg_blocking_pids(pid) AS blocking_pids
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0
LIMIT 30;
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录阻塞双方、等待时间范围和应用归属；处理后确认等待解除及事务一致性，不能把查询返回空行当作永远无锁问题。

## 风险与禁止自动操作

不自动 kill 会话、取消查询或修改锁超时。监控也有开销，大量连接时避免高频全库轮询。

## 官方参考

- [来源 1](https://www.postgresql.org/docs/17/view-pg-locks.html)

---

# Redis 内存上限、淘汰策略与写入拒绝

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:redis-memory；来源核对：2026-10-02。

## 适用范围

Redis 现场版本与 ACL 允许的只读命令；已有安全认证连接，不把密码写入命令。

## 问题与诊断

Redis 写入报内存限制相关错误，需要区分内存占用、上限配置和淘汰策略。

核对 used_memory、maxmemory、maxmemory_policy 和 evicted_keys。noeviction 达到适用限制时可能拒绝需要分配内存的写操作；淘汰不是数据安全的替代方案。

## 低影响检查示例（未执行）

```text
INFO memory
INFO stats
CONFIG GET maxmemory
CONFIG GET maxmemory-policy
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录内存、配置和错误时间，结合业务数据保留要求制定方案；修复验收需观察写入与淘汰趋势。

## 风险与禁止自动操作

CONFIG GET 可能被 ACL 禁止，禁止即申请权限而非绕过。不执行 KEYS *、FLUSHALL、CONFIG SET 或自动扩容。

## 官方参考

- [来源 1](https://redis.io/docs/latest/develop/reference/eviction/)
- [来源 2](https://redis.io/docs/latest/commands/info/)
- [来源 3](https://redis.io/docs/latest/commands/config-get/)

---

# TLS 本地证书到期时间与预警检查

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:tls-expiry；来源核对：2026-10-02。

## 适用范围

参考 OpenSSL 3.0 x509；CERT.pem 是证书文件，不是私钥。

## 问题与诊断

需要检查证书起止时间及未来一天是否到期，先确认读取的确是部署所用证书。

-checkend 判断有效期是否覆盖指定秒数。非零结果需区分即将到期与文件解析等执行错误；本地有效期检查不验证证书链、主机名或远端实际部署。

## 低影响检查示例（未执行）

```text
openssl x509 -in CERT.pem -noout -dates
openssl x509 -in CERT.pem -noout -checkend 86400
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

记录证书文件来源、有效期、退出码和检查时间；更新后还需独立验证服务端实际提供的证书。

## 风险与禁止自动操作

不读取或上传私钥，不自动替换证书或重启服务。证书未到期不代表 TLS 信任验证通过。

## 官方参考

- [来源 1](https://docs.openssl.org/3.0/man1/openssl-x509/)

---

# curl 证书校验失败与 CA 信任排查

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:curl-tls；来源核对：2026-10-02。

## 适用范围

已授权的 HTTPS 目标；确认现场 curl TLS 后端与信任库。

## 问题与诊断

出现 certificate verify failed 时，先检查目标主机名、证书链和受信 CA，不跳过校验。

示例地址必须换成已获授权的检查目标。curl 默认验证服务端证书；内部 CA 应通过受控信任配置处理，不能把 --insecure 当成修复。

## 低影响检查示例（未执行）

```text
curl --head --connect-timeout 5 --max-time 10 https://example.invalid
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

保留脱敏错误、目标主机名和 CA 配置来源；验收要求在不跳过校验的情况下成功建立受信连接，并单独确认业务状态。

## 风险与禁止自动操作

HEAD 请求仍会访问远端并可能写访问日志，执行前确认权限；部分服务不支持 HEAD。不得携带生产凭据调试未知地址。

## 官方参考

- [来源 1](https://curl.se/docs/sslcerts.html)

---

# Python 虚拟环境 macOS 与 Windows 路径区别

参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。
资料编号：ops-v1:python-venv；来源核对：2026-10-02。

## 适用范围

Python 3 venv；位于项目根目录，虚拟环境已经创建。

## 问题与诊断

macOS zsh 报 .venv/Scripts/python 不存在时，先确认用了对应平台的解释器路径。

POSIX 使用 bin，Windows 使用 Scripts。提示符出现 (.venv) 也应核对 sys.executable；虚拟环境通常不能从另一平台直接复制使用。

## 低影响检查示例（未执行）

```text
# macOS / Linux
.venv/bin/python -c 'import sys; print(sys.executable)'
.venv/bin/python -m pip --version
# Windows PowerShell
.venv/Scripts/python.exe -c "import sys; print(sys.executable)"
```

占位符必须替换为已确认且获授权的目标；本工具不会执行命令。

## 验收要求（不是执行结果）

解释器路径与 pip 所属环境一致后，再按项目说明启动 API 与 Worker；路径问题解决不代表依赖或数据库迁移已经就绪。

## 风险与禁止自动操作

这里只验证环境，不自动重建或删除 .venv，不安装包，不覆盖已有配置和数据库。

## 官方参考

- [来源 1](https://docs.python.org/3/library/venv.html)
