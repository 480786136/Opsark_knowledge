<script setup>
import { ref, reactive, computed, onMounted } from "vue";
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
function edit(doc) {
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
}
async function save() {
  const d = editor.value,
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
  editor.value = null;
  await refresh();
}
async function publish(d) {
  await api("knowledge/documents/" + d.id + "/publish", "POST", {
    revision: d.revision,
  });
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
onMounted(() =>
  action(async () => {
    try {
      const s = await api("session");
      user.value = s.username;
      csrf.value = s.csrf;
    } catch {
      return;
    }
    await refresh();
  }),
);
</script>
<template>
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
    <section class="workspace">
      <header>
        <div>
          <small>Opsark / 知识管理</small>
          <h1>{{ menus.find((m) => m[0] === tab)?.[1] }}</h1>
        </div>
        <button class="secondary" :disabled="busy" @click="action(refresh)">
          {{ busy ? "处理中…" : "刷新数据" }}
        </button>
      </header>
      <p class="error" role="alert" v-if="error">{{ error }}</p>
      <article v-if="pageResource" class="toolbar">
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
      </article>
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
        <article>
          <h2>新建知识库</h2>
          <form
            @submit.prevent="
              action(async () => {
                await api('knowledge/knowledge-bases', 'POST', base);
                base.name = '';
                base.description = '';
                await refresh();
              })
            "
          >
            <label
              >名称<input v-model="base.name" required maxlength="200" /></label
            ><label>描述<input v-model="base.description" /></label
            ><button :disabled="busy">创建知识库</button>
          </form>
        </article></template
      >
      <template v-if="tab === 'documents'"
        ><div class="toolbar">
          <button @click="edit(null)" :disabled="!bases.length">新建文档</button
          ><label class="file"
            >导入 MD / TXT<input
              type="file"
              accept=".md,.txt"
              @change="(e) => action(() => importText(e))"
          /></label>
        </div>
        <article v-if="editor">
          <h2>{{ editor.id ? "编辑草稿" : "新建草稿" }}</h2>
          <form @submit.prevent="action(save)">
            <label
              >知识库<select
                v-model="editor.knowledge_base_id"
                :disabled="!!editor.id"
                required
              >
                <option v-for="b in bases" :value="b.id">{{ b.name }}</option>
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
            <button type="button" class="secondary" @click="editor = null">
              取消
            </button>
          </form>
        </article>
        <article v-for="d in docs" :key="d.id">
          <div class="row">
            <h2>{{ d.title }}</h2>
            <span class="badge">{{ d.status }}</span>
          </div>
          <p>
            修订 {{ d.revision }} · 发布版本
            {{ d.published_version ?? "未发布" }}
          </p>
          <button
            class="secondary"
            @click="edit(d)"
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
            class="secondary"
            @click="
              action(async () => {
                await api('knowledge/documents/' + d.id + '/unpublish', 'POST');
                await refresh();
              })
            "
            :disabled="busy"
          >
            下架 / 取消发布
          </button>
        </article>
        <p v-if="!docs.length">
          尚无文档。创建草稿或从 core 上传记录开始。
        </p></template
      >
      <template v-if="tab === 'records'"
        ><article>
          <p>
            只接收 core 主动选择并脱敏的记录。上传后需 Worker 整理，再审核发布。
          </p>
        </article>
        <article v-for="r in records" :key="r.id">
          <div class="row">
            <h2>{{ r.payload?.title || r.source_record_id }}</h2>
            <span class="badge">{{ r.status }}</span>
          </div>
          <p>
            客户端 {{ r.installation_id }} · 来源修订 {{ r.source_revision }}
          </p>
          <details>
            <summary>查看脱敏来源</summary>
            <pre>{{ JSON.stringify(r.payload, null, 2) }}</pre>
          </details>
          <button
            v-if="['received', 'ready_for_review', 'failed'].includes(r.status)"
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
        </article></template
      >
      <template v-if="tab === 'keys'"
        ><article>
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
        </article>
        <article v-for="k in keys" :key="k.id">
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
        </article></template
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
        <article v-for="h in hits" :key="h.chunk_id">
          <h2>[{{ h.citation.label }}] {{ h.title }}</h2>
          <p>
            版本 {{ h.document_version }} · 行 {{ h.citation.line_start }}–{{
              h.citation.line_end
            }}
          </p>
          <pre>{{ h.content }}</pre>
        </article></template
      >
      <template v-if="tab === 'jobs'"
        ><article v-for="j in jobs" :key="j.id">
          <div class="row">
            <h2>{{ j.kind === "draft" ? "生成草稿" : "构建发布索引" }}</h2>
            <span class="badge">{{ j.status }}</span>
          </div>
          <p>{{ j.target_id }} · 已尝试 {{ j.attempts }} 次</p>
          <p v-if="j.error">{{ j.error }}</p>
          <button
            v-if="j.status === 'failed'"
            @click="
              action(async () => {
                await api('knowledge/jobs/' + j.id + '/retry', 'POST');
                await refresh();
              })
            "
          >
            重新处理
          </button>
        </article></template
      >
    </section>
  </div>
</template>
