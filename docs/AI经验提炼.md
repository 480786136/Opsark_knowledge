# AI 经验提炼

Knowledge Worker 可通过 Admin 的 Chat Completions 兼容接口，将脱敏任务来源提炼成经验草稿。默认关闭；启用意味着允许将来源发给配置的模型，并可能产生模型调用费用。不会自动发布，不执行生成命令。

## 配置

在 Knowledge 自己的 `.env` 中配置（不要放进 Core，不要提交实际 Key）：

```dotenv
AI_REFINEMENT_ENABLED=true
AI_BASE_URL=http://127.0.0.1:8001/v1
AI_API_KEY=填入Admin签发的模型用户Key
AI_MODEL=填入Admin已配置的对外模型别名
```

这里使用模型 Key，不是知识客户端 Key、管理员密码或内部服务 Token。Admin 必须已能用该别名完成模型调用。

远程接口要求 HTTPS；HTTP 只允许 localhost、127.0.0.1、::1 和 Docker Desktop 的 host.docker.internal。容器内访问宿主机可用 `http://host.docker.internal:8001/v1`，宿主机服务仍须实际可达。Compose 已传入这四个配置变量。需要模型支持 JSON 对象输出。

前后对照功能新增 `knowledge_0003` 迁移，保存最近一次成功提炼的前后快照。升级前停止 API 和 Worker，按现有流程备份数据库，在 Knowledge 根目录执行迁移，再构建页面并重启：

```bash
.venv/bin/python -m alembic upgrade head
cd web
npm run build
cd ..
.venv/bin/python -m uvicorn knowledge.main:app --host 127.0.0.1 --port 8002 --workers 1
```

另一个终端在 Knowledge 根目录运行：

```bash
.venv/bin/python -m knowledge.processing
```

## 使用

启用后，新上传记录先产生规则草稿，再排队执行 AI 提炼。知识文档页面显示任务状态，点击“刷新数据”查看结果。

已有未发布、有任务来源的草稿可点击“AI 重新提炼”。确认后基于原始来源重新生成，会替换当前草稿正文，因此先保存需要保留的人工内容。已发布文档需先下架；纯手工文档无任务来源，暂不支持。

提炼结果按适用条件、操作方法、验收标准、失败原因、注意事项组织；区分事实、建议、未确认。事实必须有 `step_id/evidence_id` 形式的来源引用。程序校验引用存在，但不能证明模型的语义推理正确，仍须人工审核。草稿末尾保留完整规则证据对照，编辑页也可查看当前收件箱分页内的来源 JSON。

审核并修改草稿后手动发布，索引完成才可检索。来源记录的 `processed` 不等于 AI 结论已经自动验证。

“前后对照”以双栏展示最近一次成功提炼的输入草稿与 AI 输出，可仅显示本侧独有行；窄屏为上下排列。差异高亮不是语义事实校验，也不标记行移动。后续人工编辑不会更改已保存的 AI 快照。失败或被并发修改取消的提炼不产生新对照。升级前的历史提炼未保存前稿，页面会显示暂无对照；再次成功提炼后即可查看。删除知识文档时同时清理对照快照。

## 失败与并发

- 超时、HTTP 错误、无效 JSON、过长响应、未知引用、敏感内容均使 AI 任务失败，保留原草稿。
- AI 失败不自动反复重试；在文档页显式重新提炼。处理任务页显示 `AI_REFINEMENT_FAILED`，不会暴露上游正文或 Key。
- 提炼期间编辑、删除、拒绝、发布或重新排队，会通过文档修订与任务租约检查阻止旧结果覆盖。
- 每次模型输出最多 5000 tokens，来源和响应各限 256 KiB；读超时 45 秒，并检查总用时。不要把未设置 AI 配置的 Worker 与已启用 Worker 混用。

自动化测试使用模拟模型，不产生真实费用。上线前用一条脱敏记录核验模型别名、JSON 输出兼容性、引用与实际提炼质量。
