<script setup>
import {
  ref,
  reactive,
  computed,
  onMounted,
  onBeforeUnmount,
  nextTick,
} from "vue";
import WorkDrawer from "./WorkDrawer.vue";
const documentQuery = ref(""),
  documentStatus = ref("");
const editorDialog = ref(null);
const baseDrawer = ref(null);
async function loadComparison(d) {
  comparison.value = null;
  comparison.value = await api("knowledge/documents/" + d.id + "/comparison");
}
const comparison = ref(null),
  onlyChanges = ref(false);
const statusNames = {
  draft: "待审核",
  published: "已发布",
  indexing: "建立索引中",
  received: "已接收",
  ready_for_review: "待审核",
  processed: "已处理",
  pending: "排队中",
  running: "处理中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  rejected: "已拒绝",
};
const statusText = (value) => statusNames[value] || value;
const refinementErrors = {
  AI_INCOMPLETE_OUTPUT:
    "模型输出不完整（可能达到 Token 上限），请查看 Admin 调用详情中的结束原因。",
  AI_SCHEMA_INVALID: "模型输出未通过 JSON 结构校验。",
  AI_RESPONSE_INVALID: "模型响应格式无效。",
  AI_INVALID_REFERENCE: "模型引用了不存在的证据，或事实缺少引用。",
  AI_EVIDENCE_TYPE_MISMATCH:
    "结论与证据类型不匹配：预期标准或客户端声明不能作为已验证事实。原草稿保留，请重新提炼或人工整理。",
  AI_TIMEOUT: "模型请求超时。",
  AI_HTTP_ERROR: "模型接口返回失败状态，请查看关联调用。",
  AI_CONNECTION_ERROR: "无法连接模型接口。",
  AI_DATABASE_ERROR: "保存结果失败，请检查数据库迁移及 Worker 日志。",
  AI_SENSITIVE_CONTENT: "输入或输出触发敏感信息检查。",
  AI_REFINEMENT_FAILED:
    "请使用任务 ID 检索 Worker 日志；旧任务未记录详细原因。",
};
const refineJob = (d) =>
  jobs.value.find((j) => j.kind === "refine" && j.target_id === d.id);
const refinementError = (value) =>
  refinementErrors[value?.split(":")[0]] || value;
const comparedLines = computed(() => {
  const before = (comparison.value?.comparison?.before_content || "").split(
    "\n",
  );
  const after = (comparison.value?.comparison?.after_content || "").split("\n");
  const left = new Set(before),
    right = new Set(after);
  return {
    before: before.map((text, i) => ({
      text,
      number: i + 1,
      changed: !right.has(text),
    })),
    after: after.map((text, i) => ({
      text,
      number: i + 1,
      changed: !left.has(text),
    })),
  };
});
const user = ref(null),
  csrf = ref(""),
  busy = ref(false),
  error = ref(""),
  tab = ref("overview");
const config = ref({}),
  bases = ref([]),
  docs = ref([]),
  records = ref([]),
  keys = ref([]),
  jobs = ref([]),
  hits = ref([]);
const offsets = reactive({
  "knowledge-bases": 0,
  documents: 0,
  records: 0,
  "knowledge-keys": 0,
  jobs: 0,
});
const visibleDocs = computed(() =>
  docs.value.filter(
    (d) =>
      (!documentStatus.value || d.status === documentStatus.value) &&
      `${d.title} ${d.source_title || ""} ${d.source_external_id || ""} ${d.source_installation_id || ""} ${d.environment}`
        .toLowerCase()
        .includes(documentQuery.value.toLowerCase()),
  ),
);
const pageResource = computed(
  () =>
    ({
      overview: "knowledge-bases",
      documents: "documents",
      records: "records",
      keys: "knowledge-keys",
      jobs: "jobs",
    })[tab.value],
);
const pageRows = computed(
  () =>
    ({
      "knowledge-bases": bases.value,
      documents: docs.value,
      records: records.value,
      "knowledge-keys": keys.value,
      jobs: jobs.value,
    })[pageResource.value] || [],
);
async function changePage(delta) {
  const p = pageResource.value;
  if (!p) return;
  offsets[p] = Math.max(0, offsets[p] + delta * 200);
  await refresh();
}
const login = reactive({ username: "admin", password: "" }),
  base = reactive({ name: "", description: "" });
const keyForm = reactive({
  name: "",
  installation_id: "",
  knowledge_base_ids: [],
  scopes: ["records:write", "records:read", "knowledge:read"],
  expires_days: 90,
});
const freshKey = ref(""),
  editor = ref(null),
  query = ref(""),
  selectedBase = ref(""),
  searchMode = ref("");
async function api(path, method = "GET", body) {
  const response = await fetch("/api/admin/v1/" + path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf.value },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "session") user.value = null;
    throw new Error(result.error?.message || result.error?.code || "请求失败");
  }
  return result;
}
async function action(fn) {
  busy.value = true;
  error.value = "";
  try {
    await fn();
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function refresh() {
  config.value = await api("config");
  if (!config.value.knowledge_connected) return;
  const result = await Promise.all(
    ["knowledge-bases", "documents", "records", "knowledge-keys", "jobs"].map(
      (p) => api("knowledge/" + p + "?offset=" + offsets[p] + "&limit=200"),
    ),
  );
  [bases.value, docs.value, records.value, keys.value, jobs.value] = result;
  if (!selectedBase.value && bases.value.length)
    selectedBase.value = bases.value[0].id;
}
let refreshTimer;
let backgroundRefreshing = false;
async function refreshInBackground() {
  if (!user.value || busy.value || backgroundRefreshing || document.hidden) return;
  backgroundRefreshing = true;
  try {
    await refresh();
  } catch {
    // Keep the current page usable; explicit actions still surface API errors.
  } finally {
    backgroundRefreshing = false;
  }
}
async function signIn() {
  const s = await api("session", "POST", login);
  user.value = s.username;
  csrf.value = s.csrf;
  login.password = "";
  await refresh();
}
async function signOut() {
  await api("session", "DELETE");
  user.value = null;
  freshKey.value = "";
  csrf.value = "";
  records.value = [];
  docs.value = [];
  keys.value = [];
  hits.value = [];
  editor.value = null;
}
async function edit(doc) {
  editor.value = doc
    ? { ...doc }
    : {
        knowledge_base_id: selectedBase.value,
        title: "",
        content: "",
        tags: [],
        environment: "",
        software_names: [],
      };
  tab.value = "documents";
  await nextTick();
  editorDialog.value?.showModal();
  document
    .querySelector("#document-editor input")
    ?.focus({ preventScroll: true });
}
function editInline(doc) {
  editor.value = { ...doc };
}
function cancelInlineEdit() {
  editor.value = null;
}
async function save() {
  const d = editor.value,
    inline = Boolean(d.id && !editorDialog.value?.open),
    body = Object.fromEntries(
      [
        "knowledge_base_id",
        "title",
        "content",
        "tags",
        "environment",
        "software_names",
      ].map((k) => [k, d[k]]),
    );
  if (d.id) body.revision = d.revision;
  await api(
    "knowledge/documents" + (d.id ? "/" + d.id + "/draft" : ""),
    d.id ? "PATCH" : "POST",
    body,
  );
  if (!inline) editorDialog.value?.close();
  editor.value = null;
  await refresh();
}
async function publish(d) {
  await api("knowledge/documents/" + d.id + "/publish", "POST", {
    revision: d.revision,
  });
  await refresh();
}
async function refine(d) {
  if (
    !confirm(
      "将把脱敏来源发送到配置的模型，提炼结果会替换当前草稿；发布仍需审核。是否继续？",
    )
  )
    return;
  await api("knowledge/documents/" + d.id + "/refine", "POST", {
    revision: d.revision,
  });
  editor.value = null;
  await refresh();
}
async function search() {
  const r = await api("knowledge/search", "POST", {
    query: query.value,
    knowledge_base_ids: [selectedBase.value],
    top_k: 5,
    max_content_chars: 6000,
  });
  hits.value = r.hits;
  searchMode.value = r.retrieval_mode;
}
async function importText(event) {
  const f = event.target.files?.[0];
  if (!f) return;
  if (!/\.(md|txt)$/i.test(f.name) || f.size > 2 * 1024 * 1024)
    throw new Error("仅支持 2 MiB 以内 MD/TXT");
  edit(null);
  editor.value.title = f.name;
  editor.value.content = await f.text();
  event.target.value = "";
}
const menus = [
  ["overview", "工作概览"],
  ["documents", "知识文档"],
  ["records", "记录收件箱"],
  ["search", "检索调试"],
  ["keys", "客户端 Key"],
  ["jobs", "处理任务"],
];
onMounted(() => {
  action(async () => {
    try {
      const s = await api("session");
      user.value = s.username;
      csrf.value = s.csrf;
    } catch {
      return;
    }
    await refresh();
  });
  refreshTimer = window.setInterval(refreshInBackground, 5000);
  document.addEventListener("visibilitychange", refreshInBackground);
});
onBeforeUnmount(() => {
  window.clearInterval(refreshTimer);
  document.removeEventListener("visibilitychange", refreshInBackground);
});
</script>
<template>
  <a v-if="user" class="skip-link" href="#main-content">跳到主要内容</a>
  <main v-if="!user" class="login">
    <div class="brand">O<span>Opsark</span></div>
    <h1>知识管理</h1>
    <p>独立知识管理，不依赖模型平台</p>
    <form @submit.prevent="action(signIn)">
      <label
        >管理员账号<input
          v-model="login.username"
          autocomplete="username"
          required /></label
      ><label
        >密码<input
          v-model="login.password"
          type="password"
          autocomplete="current-password"
          required /></label
      ><button :disabled="busy">登录知识管理</button>
    </form>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <small
      >首次使用：在服务器运行 python -m knowledge.bootstrap 创建管理员。</small
    >
  </main>
  <div v-else class="shell">
    <aside>
      <div class="brand">O<span>Opsark</span></div>
      <div class="subtitle">KNOWLEDGE CONSOLE</div>
      <nav>
        <button
          v-for="[id, label] in menus"
          :key="id"
          :class="{ active: tab === id }"
          @click="
            tab = id;
            freshKey = '';
          "
        >
          {{ label }}
        </button>
      </nav>
      <div class="account">
        {{ user
        }}<button class="secondary" @click="action(signOut)">退出登录</button>
      </div>
    </aside>
    <section id="main-content" class="workspace">
      <header>
        <div>
          <small>Opsark / 知识管理</small>
          <h1>{{ menus.find((m) => m[0] === tab)?.[1] }}</h1>
          <p class="page-description">
            {{
              {
                overview: "从任务记录到可复用经验，让每一次处理都有沉淀。",
                documents: "审核证据、对照 AI 提炼结果，再发布为团队知识。",
                records: "查看 Core 上传的来源记录，追踪整理进度。",
                search: "验证已发布知识的检索效果与来源。",
                keys: "为客户端分配最小必要权限，管理访问凭据。",
                jobs: "追踪草稿生成、AI 提炼和发布索引。",
              }[tab]
            }}
          </p>
        </div>
        <button class="secondary" :disabled="busy" @click="action(refresh)">
          {{ busy ? "处理中…" : "刷新数据" }}
        </button>
      </header>
      <div class="list-scroll">
        <p class="error" role="alert" v-if="error">{{ error }}</p>

        <template v-if="tab === 'overview'"
          ><div class="stats">
            <article>
              <small>知识库</small><strong>{{ bases.length }}</strong>
            </article>
            <article>
              <small>文档</small><strong>{{ docs.length }}</strong>
            </article>
            <article>
              <small>待审核来源</small
              ><strong>{{
                records.filter((r) => r.status === "ready_for_review").length
              }}</strong>
            </article>
          </div>
          <article>
            <h2>本页知识库</h2>
            <p v-if="!bases.length">暂无知识库，请先创建。</p>
            <p v-for="b in bases" :key="b.id">
              {{ b.name }} · {{ b.enabled ? "启用" : "停用" }} ·
              <code>{{ b.id }}</code>
            </p>
            <small
              >此页库列表同时用于文档和 Key
              的库选择，可通过上方分页查看更多。</small
            >
          </article>
          <article>
            <h2>服务边界</h2>
            <p>知识管理与资料处理均在本服务内，模型平台关闭不影响使用。</p>
            <p>
              知识接口：<code>{{ config.knowledge_base_url }}</code>
            </p>

            <p v-if="!config.knowledge_connected" class="error">
              尚未配置服务间凭据，请填写服务器 .env。
            </p>
          </article>
          <WorkDrawer ref="baseDrawer" :error="error" create title="新建知识库">
            <h2>新建知识库</h2>
            <form
              @submit.prevent="
                action(async () => {
                  await api('knowledge/knowledge-bases', 'POST', base);
                  base.name = '';
                  base.description = '';
                  baseDrawer.close();
                  await refresh();
                })
              "
            >
              <label
                >名称<input
                  v-model="base.name"
                  required
                  maxlength="200" /></label
              ><label>描述<input v-model="base.description" /></label
              ><button :disabled="busy">创建知识库</button>
            </form>
          </WorkDrawer></template
        >
        <template v-if="tab === 'documents'"
          ><div class="toolbar">
            <button @click="edit(null)" :disabled="!bases.length">
              新建文档</button
            ><label class="file"
              >导入 MD / TXT<input
                type="file"
                accept=".md,.txt"
                @change="(e) => action(() => importText(e))"
            /></label>
          </div>
          <div class="filter-bar">
            <label
              >搜索本页文档<input
                v-model="documentQuery"
                placeholder="输入标题或环境"
                type="search" /></label
            ><label
              >文档状态<select v-model="documentStatus">
                <option value="">全部状态</option>
                <option value="draft">待审核</option>
                <option value="published">已发布</option>
                <option value="indexing">建立索引中</option>
              </select></label
            ><span>本页匹配 {{ visibleDocs.length }} 篇</span>
          </div>
          <p v-if="!config.ai_refinement_ready">
            AI 提炼未启用或模型配置不完整。请在 Knowledge 的 .env 配置后重启 API
            与 Worker；规则草稿仍可使用。
          </p>
          <dialog ref="editorDialog" class="work-drawer" @close="editor = null">
            <article v-if="editor" id="document-editor">
              <p v-if="error" class="error" role="alert">{{ error }}</p>
              <div class="editor-heading">
                <div>
                  <small>文档工作区</small>
                  <h2>{{ editor.id ? "编辑草稿" : "新建草稿" }}</h2>
                </div>
                <button
                  type="button"
                  class="icon-button"
                  aria-label="关闭编辑器"
                  @click="editorDialog.close()"
                >
                  ×
                </button>
              </div>
              <section v-if="editor.source_record_id" class="detail-section">
                <h3>来源对照 <small>AI 结论需逐条核对证据</small></h3>
                <pre>{{
                  records.find((r) => r.id === editor.source_record_id)
                    ?.payload ||
                  "来源不在当前分页，请到记录收件箱查看；草稿末尾也保留规则证据对照。"
                }}</pre>
              </section>
              <form @submit.prevent="action(save)">
                <label
                  >知识库<select
                    v-model="editor.knowledge_base_id"
                    :disabled="!!editor.id"
                    required
                  >
                    <option v-for="b in bases" :value="b.id">
                      {{ b.name }}
                    </option>
                  </select></label
                ><label
                  >标题<input
                    v-model="editor.title"
                    required
                    maxlength="200" /></label
                ><label
                  >环境<input
                    v-model="editor.environment"
                    placeholder="例如 production" /></label
                ><label
                  >正文（纯文本 / Markdown）<textarea
                    v-model="editor.content"
                    rows="14"
                    required
                  ></textarea></label
                ><button :disabled="busy">保存草稿</button>
                <button
                  type="button"
                  class="secondary"
                  @click="editorDialog.close()"
                >
                  取消
                </button>
              </form>
            </article>
          </dialog>
          <WorkDrawer
            :error="error"
            v-for="d in visibleDocs"
            :key="d.id"
            :title="d.title"
            :summary="
              '修订 ' +
              d.revision +
              ' · 发布版本 ' +
              (d.published_version ?? '未发布')
            "
            :status="statusText(d.status)"
            wide
            @open="action(() => loadComparison(d))"
            @close="editor?.id === d.id && cancelInlineEdit()"
          >
            <form
              v-if="editor?.id === d.id"
              class="inline-detail-form"
              @submit.prevent="action(save)"
            >
              <div class="detail-heading">
                <div>
                  <small>文档工作区</small>
                  <h2>编辑草稿</h2>
                </div>
                <span class="badge draft">修订 {{ d.revision }}</span>
              </div>
              <div class="detail-grid">
                <label
                  >知识库<select v-model="editor.knowledge_base_id" disabled>
                    <option v-for="b in bases" :value="b.id">
                      {{ b.name }}
                    </option>
                  </select></label
                >
                <label
                  >环境<input
                    v-model="editor.environment"
                    placeholder="例如 production"
                /></label>
                <label class="full"
                  >标题<input v-model="editor.title" required maxlength="200"
                /></label>
                <label class="full"
                  >正文（纯文本 / Markdown）<textarea
                    v-model="editor.content"
                    rows="18"
                    required
                  ></textarea>
                </label>
              </div>
              <section v-if="editor.source_record_id" class="detail-section">
                <h3>来源证据</h3>
                <pre>{{
                  records.find((r) => r.id === editor.source_record_id)
                    ?.payload || "来源不在当前分页，请到记录收件箱查看。"
                }}</pre>
              </section>
              <div class="form-actions">
                <button :disabled="busy">保存草稿</button
                ><button
                  type="button"
                  class="secondary"
                  @click="cancelInlineEdit"
                >
                  取消
                </button>
              </div>
            </form>
            <template v-else>
              <div class="row">
                <h2>{{ d.title }}</h2>
                <span :class="['badge', d.status]">{{
                  statusText(d.status)
                }}</span>
              </div>
              <p>
                修订 {{ d.revision }} · 发布版本
                {{ d.published_version ?? "未发布" }}
              </p>
              <p v-if="d.source_record_id" class="source-reference">
                来源：{{ d.source_title || "未命名任务" }} ·
                {{ d.source_installation_id || "未知客户端" }} · 修订
                {{ d.source_revision ?? "—" }}
              </p>
              <button
                class="secondary"
                @click="editInline(d)"
                :disabled="d.status === 'indexing'"
              >
                编辑
              </button>

              <button
                @click="action(() => publish(d))"
                :disabled="busy || d.status === 'indexing'"
              >
                审核并发布
              </button>
              <button
                v-if="d.source_record_id"
                class="secondary"
                :disabled="
                  busy ||
                  !config.ai_refinement_ready ||
                  d.status !== 'draft' ||
                  d.published_version != null
                "
                @click="action(() => refine(d))"
              >
                AI 重新提炼
              </button>
              <p
                v-if="
                  jobs.some((j) => j.kind === 'refine' && j.target_id === d.id)
                "
              >
                AI 提炼：{{
                  jobs.find((j) => j.kind === "refine" && j.target_id === d.id)
                    ?.status
                }}；失败时原草稿保留，可重新提炼。成功结果仍需人工核对。
              </p>
              <div v-if="refineJob(d)?.error" role="status">
                <p>{{ refinementError(refineJob(d).error) }}</p>
                <small
                  >错误：{{ refineJob(d).error.split(":")[0] }} · 任务 ID：{{
                    refineJob(d).id
                  }}</small
                >
                <p v-if="refineJob(d).error.includes(':')">
                  Admin 请求 ID：{{
                    refineJob(d).error.split(":")[1]
                  }}（可在调用监控中查询）
                </p>
              </div>
              <button
                class="secondary"
                @click="
                  action(async () => {
                    await api(
                      'knowledge/documents/' + d.id + '/unpublish',
                      'POST',
                    );
                    await refresh();
                  })
                "
                :disabled="busy"
              >
                下架 / 取消发布
              </button>

              <section class="inline-comparison detail-section">
                <h3>提炼前后对照</h3>
                <p v-if="comparison?.comparison">
                  对照结果修订 {{ comparison.comparison.applied_revision }} ·
                  当前修订
                  {{
                    d.revision
                  }}。高亮为本侧独有行；历史提炼结果不代表当前草稿。
                </p>
                <div v-if="comparison?.comparison" class="comparison-grid">
                  <section
                    v-for="side in ['before', 'after']"
                    :key="side"
                    :class="['comparison-pane', side]"
                  >
                    <h3>
                      {{
                        side === "before"
                          ? "提炼前 · 历史草稿"
                          : "提炼后 · AI 结果"
                      }}
                    </h3>
                    <div class="comparison-content" tabindex="0">
                      <div
                        v-for="line in comparedLines[side]"
                        :key="line.number"
                        :class="['diff-line', { changed: line.changed }]"
                      >
                        <span class="line-number">{{ line.number }}</span>
                        <pre>{{ line.text || " " }}</pre>
                      </div>
                    </div>
                  </section>
                </div>
                <p v-else>暂无成功提炼对照，请查看当前草稿并核对来源。</p>
              </section>
              <section class="detail-section">
                <h3>当前草稿 · 修订 {{ d.revision }}</h3>
                <pre>{{ d.content }}</pre>
              </section>
            </template>
          </WorkDrawer>
          <p v-if="!visibleDocs.length" class="empty-state">
            {{
              docs.length
                ? "没有匹配的文档，请调整搜索或状态筛选。"
                : "尚无文档。创建草稿或从 Core 上传第一份任务记录。"
            }}
          </p></template
        >
        <template v-if="tab === 'records'"
          ><article>
            <p>
              只接收 core 主动选择并脱敏的记录。上传后需 Worker
              整理，再审核发布。
            </p>
          </article>
          <WorkDrawer
            :error="error"
            v-for="r in records"
            :key="r.id"
            :title="r.payload?.title || r.source_record_id"
            :summary="r.installation_id + ' · 修订 ' + r.source_revision"
            :status="statusText(r.status)"
          >
            <div class="row">
              <h2>{{ r.payload?.title || r.source_record_id }}</h2>
              <span :class="['badge', r.status]">{{
                statusText(r.status)
              }}</span>
            </div>
            <p>
              客户端 {{ r.installation_id }} · 来源修订 {{ r.source_revision }}
            </p>
            <section class="detail-section">
              <h3>脱敏来源</h3>
              <pre>{{ JSON.stringify(r.payload, null, 2) }}</pre>
            </section>
            <button
              v-if="
                ['received', 'ready_for_review', 'failed'].includes(r.status)
              "
              :disabled="busy"
              @click="
                action(async () => {
                  if (
                    !confirm(
                      '拒绝此来源及未发布草稿？正文保留用于审计，不会发布或继续处理。',
                    )
                  )
                    return;
                  await api('knowledge/records/' + r.id + '/reject', 'POST');
                  await refresh();
                })
              "
            >
              拒绝审核
            </button>
          </WorkDrawer></template
        >
        <template v-if="tab === 'keys'"
          ><WorkDrawer
            :error="error"
            create
            title="签发知识 Key"
            @close="freshKey = ''"
          >
            <h2>签发知识 Key</h2>
            <p>仅展示一次完整 Key；模型 Key 请在模型控制台独立签发。</p>
            <form
              @submit.prevent="
                action(async () => {
                  const r = await api(
                    'knowledge/knowledge-keys',
                    'POST',
                    keyForm,
                  );
                  freshKey = r.api_key;
                  await refresh();
                })
              "
            >
              <label>名称<input v-model="keyForm.name" required /></label
              ><label
                >core 安装标识<input
                  v-model="keyForm.installation_id"
                  required
                  placeholder="developer-laptop" /></label
              ><label
                >授权知识库<select
                  v-model="keyForm.knowledge_base_ids"
                  multiple
                  required
                >
                  <option v-for="b in bases" :value="b.id">{{ b.name }}</option>
                </select></label
              ><button :disabled="busy">生成 Key</button>
            </form>
            <div v-if="freshKey" class="secret">
              <p>请现在复制到 core 系统钥匙串；离开此页面后不再展示。</p>
              <code>{{ freshKey }}</code
              ><button class="secondary" @click="freshKey = ''">
                已保存，隐藏
              </button>
            </div>
          </WorkDrawer>
          <WorkDrawer
            :error="error"
            v-for="k in keys"
            :key="k.id"
            :title="k.name"
            :summary="k.installation_id"
            :status="k.revoked ? '已撤销' : '有效'"
          >
            <div class="row">
              <h2>{{ k.name }}</h2>
              <span>{{ k.revoked ? "已撤销" : k.prefix + "…" }}</span>
            </div>
            <p>
              {{ k.installation_id }} ·
              {{ new Date(k.expires * 1000).toLocaleDateString() }} 到期
            </p>
            <button
              class="secondary"
              :disabled="k.revoked || busy"
              @click="
                action(async () => {
                  await api('knowledge/knowledge-keys/' + k.id, 'DELETE');
                  await refresh();
                })
              "
            >
              撤销
            </button>
          </WorkDrawer></template
        >
        <template v-if="tab === 'search'"
          ><article>
            <form @submit.prevent="action(search)">
              <label
                >知识库<select v-model="selectedBase" required>
                  <option v-for="b in bases" :value="b.id">{{ b.name }}</option>
                </select></label
              ><label
                >查询<input
                  v-model="query"
                  required
                  placeholder="输入问题、命令、路径或错误码" /></label
              ><button :disabled="busy">检索已发布知识</button>
            </form>
            <p v-if="searchMode">模式：{{ searchMode }}</p>
          </article>
          <WorkDrawer
            :title="h.title"
            :summary="'版本 ' + h.document_version + ' · ' + h.citation.label"
            status="检索结果"
            v-for="h in hits"
            :key="h.chunk_id"
          >
            <h2>[{{ h.citation.label }}] {{ h.title }}</h2>
            <p>
              版本 {{ h.document_version }} · 行 {{ h.citation.line_start }}–{{
                h.citation.line_end
              }}
            </p>
            <pre>{{ h.content }}</pre>
          </WorkDrawer></template
        >
        <template v-if="tab === 'jobs'"
          ><WorkDrawer
            :error="error"
            v-for="j in jobs"
            :key="j.id"
            :title="
              j.kind === 'refine'
                ? 'AI 经验提炼'
                : j.kind === 'draft'
                  ? '生成草稿'
                  : '构建发布索引'
            "
            :summary="j.target_id + ' · 尝试 ' + j.attempts + ' 次'"
            :status="statusText(j.status)"
          >
            <div class="row">
              <h2>
                {{
                  j.kind === "draft"
                    ? "生成草稿"
                    : j.kind === "refine"
                      ? "AI 经验提炼"
                      : "构建发布索引"
                }}
              </h2>
              <span :class="['badge', j.status]">{{
                statusText(j.status)
              }}</span>
            </div>
            <p>{{ j.target_id }} · 已尝试 {{ j.attempts }} 次</p>
            <p v-if="j.error">{{ j.error }}</p>
            <button
              v-if="j.status === 'failed' && j.kind !== 'refine'"
              @click="
                action(async () => {
                  await api('knowledge/jobs/' + j.id + '/retry', 'POST');
                  await refresh();
                })
              "
            >
              重新处理
            </button>
          </WorkDrawer></template
        >
        <p
          v-if="['records', 'keys', 'jobs'].includes(tab) && !pageRows.length"
          class="empty-state"
        >
          本页暂无记录。
        </p>
      </div>
      <footer v-if="pageResource" class="list-pagination">
        <span
          >当前列表第 {{ offsets[pageResource] / 200 + 1 }} 页 · 本页
          {{ pageRows.length }} 条（不是全库统计）</span
        ><button
          :disabled="busy || offsets[pageResource] === 0"
          @click="action(() => changePage(-1))"
        >
          上一页</button
        ><button
          :disabled="busy || pageRows.length < 200"
          @click="action(() => changePage(1))"
        >
          下一页
        </button>
      </footer>
    </section>
  </div>
</template>
