<script setup>
import {
  ref,
  reactive,
  computed,
  onMounted,
  onBeforeUnmount,
  nextTick,
  watch,
} from "vue";
import WorkDrawer from "./WorkDrawer.vue";
import DocumentDetails from "./DocumentDetails.vue";
import SourceDetails from "./SourceDetails.vue";
import SourceEvidence from "./SourceEvidence.vue";
import VersionPreview from "./VersionPreview.vue";
import OverviewPanel from "./OverviewPanel.vue";
const documentQuery = ref(""),
  documentStatus = ref("");
const documentBase = ref("");
const overview = ref(null);
const selections = ref({});
const batchDialog = ref(null),
  batchSnapshot = ref([]),
  batchReviewed = ref(false),
  batchResult = ref(null);
const sourceNames = {
  core_record: "Core 任务记录",
  reference_pack: "参考知识包",
  manual: "手工创建 / 文件导入",
};
const baseName = (id) => bases.value.find((base) => base.id === id)?.name || id;
const ownership = (doc) => ({
  base: doc.knowledge_base_name || baseName(doc.knowledge_base_id),
  source:
    sourceNames[doc.source_kind] ||
    (doc.source_record_id ? "Core 任务记录" : "手工创建 / 文件导入"),
  creator: doc.created_by || "历史记录未留存",
});
const editorDialog = ref(null);
const baseDrawer = ref(null);
const activeDocumentId = ref(""),
  activeRecordId = ref("");
const documentDrawers = new Map(),
  recordDrawers = new Map();
const documentPanels = new Map();
const citationDialog = ref(null),
  citationVersion = ref(null),
  citationHit = ref(null);
const citationSources = ref([]);
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
  superseded: "已被新修订替代",
  deleted: "已删除",
};
const statusText = (value) => statusNames[value] || value;
const user = ref(null),
  csrf = ref(""),
  busy = ref(false),
  error = ref(""),
  syncWarning = ref(""),
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
const selectedDocuments = computed(() => Object.values(selections.value));
const eligibleDocs = computed(() =>
  visibleDocs.value.filter(
    (doc) => doc.status === "draft" && doc.knowledge_base_enabled !== false,
  ),
);
const allVisibleSelected = computed(
  () =>
    eligibleDocs.value.length > 0 &&
    eligibleDocs.value.slice(0, 100).every((doc) => selections.value[doc.id]),
);
function toggleSelection(doc, checked) {
  if (checked && selectedDocuments.value.length < 100)
    selections.value = { ...selections.value, [doc.id]: { ...doc } };
  else if (!checked) {
    const next = { ...selections.value };
    delete next[doc.id];
    selections.value = next;
  }
}
function selectVisible(event) {
  for (const doc of eligibleDocs.value)
    toggleSelection(doc, event.target.checked);
}
async function showDocuments(baseId = "", status = "") {
  documentBase.value = baseId;
  documentStatus.value = status;
  documentQuery.value = "";
  selections.value = {};
  batchResult.value = null;
  offsets.documents = 0;
  tab.value = "documents";
  await refresh();
}
async function beginBatch() {
  batchSnapshot.value = selectedDocuments.value.map((doc) => ({ ...doc }));
  batchReviewed.value = false;
  batchResult.value = null;
  await nextTick();
  batchDialog.value?.showModal();
}
async function submitBatch() {
  if (!batchReviewed.value || !batchSnapshot.value.length) return;
  const result = await api("knowledge/documents/batch-publish", "POST", {
    documents: batchSnapshot.value.map((doc) => ({
      document_id: doc.id,
      revision: doc.revision,
    })),
  });
  batchResult.value = {
    ...result,
    titles: Object.fromEntries(
      batchSnapshot.value.map((doc) => [doc.id, doc.title]),
    ),
  };
  selections.value = {};
  batchDialog.value?.close();
  await refresh();
}
async function changePage(delta) {
  const p = pageResource.value;
  if (!p) return;
  const previous = offsets[p];
  offsets[p] = Math.max(0, offsets[p] + delta * 200);
  try {
    await refresh();
    selections.value = {};
  } catch (e) {
    offsets[p] = previous;
    throw e;
  }
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
  searchMode = ref(""),
  searchWarnings = ref([]),
  searchTruncated = ref(false),
  hasSearched = ref(false),
  searchEmbedding = ref(""),
  searchCorpus = ref(null);
const selectedBaseSummary = computed(() =>
  overview.value?.bases?.find((base) => base.id === selectedBase.value),
);
const selectedSearchCorpus = computed(
  () => selectedBaseSummary.value || searchCorpus.value,
);
const emptySearchMessage = computed(() => {
  const corpus = selectedSearchCorpus.value;
  if (corpus && corpus.searchable === 0) {
    if (corpus.indexing)
      return "该库的发布索引仍在构建。请稍后刷新，索引成功后即可检索。";
    if (corpus.drafts)
      return `该库有 ${corpus.drafts} 篇待审核草稿，目前没有可检索知识。请先审核并发布。`;
    return "该库目前没有可检索知识。请先添加文档并完成发布。";
  }
  return "没有匹配的已发布知识。请尝试正文中的问题、命令或错误码；知识库名称本身不一定出现在文档中。";
});
watch([query, selectedBase], () => {
  hits.value = [];
  hasSearched.value = false;
  searchMode.value = "";
  searchWarnings.value = [];
  searchCorpus.value = null;
  searchEmbedding.value = "";
  searchTruncated.value = false;
});
watch([documentQuery, documentStatus], () => {
  selections.value = {};
});
async function api(path, method = "GET", body) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch("/api/admin/v1/" + path, {
      method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf.value,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && path !== "session") user.value = null;
      throw new Error(
        result.error?.message || result.error?.code || "请求失败，请稍后重试",
      );
    }
    return result;
  } catch (e) {
    if (e.name === "AbortError")
      throw new Error("请求超时。请刷新确认操作结果后再重试。");
    if (e instanceof TypeError)
      throw new Error("暂时无法连接知识服务，请检查连接后重试。");
    throw e;
  } finally {
    window.clearTimeout(timeout);
  }
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
let refreshSequence = 0;
async function refresh() {
  const sequence = ++refreshSequence;
  const requestedOffsets = { ...offsets },
    requestedBase = documentBase.value;
  try {
    const nextConfig = await api("config");
    if (sequence !== refreshSequence) return;
    if (!nextConfig.knowledge_connected) {
      config.value = nextConfig;
      return;
    }
    const [result, nextOverview] = await Promise.all([
      Promise.all(
        [
          "knowledge-bases",
          "documents",
          "records",
          "knowledge-keys",
          "jobs",
        ].map((p) =>
          api(
            "knowledge/" +
              p +
              "?offset=" +
              requestedOffsets[p] +
              "&limit=200" +
              (p === "documents" && requestedBase
                ? "&knowledge_base_id=" + encodeURIComponent(requestedBase)
                : ""),
          ),
        ),
      ),
      api(
        "knowledge/overview?offset=" +
          requestedOffsets["knowledge-bases"] +
          "&limit=200",
      ),
    ]);
    if (sequence !== refreshSequence) return;
    config.value = nextConfig;
    [bases.value, docs.value, records.value, keys.value, jobs.value] = result;
    overview.value = nextOverview;
    if (!selectedBase.value && bases.value.length)
      selectedBase.value = bases.value.find((base) => base.enabled)?.id || "";
    syncWarning.value = "";
  } catch (error) {
    if (sequence === refreshSequence) throw error;
  }
}
let refreshTimer;
let backgroundRefreshing = false;
async function refreshInBackground() {
  if (!user.value || busy.value || backgroundRefreshing || document.hidden)
    return;
  backgroundRefreshing = true;
  try {
    await refresh();
  } catch {
    syncWarning.value =
      "自动刷新暂时失败，当前显示的是上次加载的数据。请刷新数据后再操作。";
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
  refreshSequence++;
  user.value = null;
  freshKey.value = "";
  csrf.value = "";
  records.value = [];
  docs.value = [];
  keys.value = [];
  hits.value = [];
  editor.value = null;
  activeDocumentId.value = "";
  activeRecordId.value = "";
  citationVersion.value = null;
  citationSources.value = [];
  searchWarnings.value = [];
  syncWarning.value = "";
  searchTruncated.value = false;
  hasSearched.value = false;
  selections.value = {};
  overview.value = null;
  batchResult.value = null;
  searchCorpus.value = null;
  searchEmbedding.value = "";
  searchMode.value = "";
  selectedBase.value = "";
  documentBase.value = "";
  bases.value = [];
  jobs.value = [];
}
async function edit(doc) {
  editor.value = doc
    ? { ...doc }
    : {
        knowledge_base_id: documentBase.value || selectedBase.value,
        title: "",
        content: "",
        tags: [],
        environment: "",
        software_names: [],
        context: {},
      };
  tab.value = "documents";
  await nextTick();
  editorDialog.value?.showModal();
  document
    .querySelector("#document-editor input")
    ?.focus({ preventScroll: true });
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
        "context",
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
async function search() {
  hits.value = [];
  searchWarnings.value = [];
  searchMode.value = "";
  searchTruncated.value = false;
  hasSearched.value = false;
  const requestedQuery = query.value.trim(),
    requestedBase = selectedBase.value;
  const r = await api("knowledge/search", "POST", {
    query: requestedQuery,
    knowledge_base_ids: [requestedBase],
    top_k: 5,
    max_content_chars: 6000,
  });
  if (
    requestedQuery !== query.value.trim() ||
    requestedBase !== selectedBase.value
  )
    return;
  hits.value = r.hits;
  searchMode.value = r.retrieval_mode;
  searchWarnings.value = r.warnings || [];
  searchTruncated.value = Boolean(r.truncated);
  searchEmbedding.value = r.embedding_status || "";
  searchCorpus.value = r.corpus || null;
  if (selectedBaseSummary.value && r.corpus)
    Object.assign(selectedBaseSummary.value, r.corpus);
  hasSearched.value = true;
}
async function openCitation(hit) {
  citationVersion.value = null;
  citationSources.value = [];
  citationHit.value = hit;
  await nextTick();
  citationDialog.value.showModal();
  citationVersion.value = await api(
    "knowledge/documents/" +
      encodeURIComponent(hit.document_id) +
      "/versions/" +
      hit.document_version,
  );
}
async function loadCitationSources() {
  const ids = citationVersion.value?.source_record_ids || [];
  citationSources.value = await Promise.all(
    ids.map((id) => api("knowledge/records/" + encodeURIComponent(id))),
  );
}
async function deletedDocument(id) {
  documentDrawers.get(id)?.close();
  activeDocumentId.value = "";
  await refresh();
}
async function deletedRecord(id) {
  recordDrawers.get(id)?.close();
  activeRecordId.value = "";
  await refresh();
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
        <p v-if="syncWarning" class="error" role="status">{{ syncWarning }}</p>

        <template v-if="tab === 'overview'">
          <OverviewPanel
            :overview="overview"
            :config="config"
            :busy="busy"
            @documents="
              (base, status) => action(() => showDocuments(base, status))
            "
            @search="
              (base) => {
                selectedBase = base;
                tab = 'search';
              }
            "
            @jobs="tab = 'jobs'"
          />
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
              >所属知识库<select
                v-model="documentBase"
                :disabled="busy"
                @change="
                  action(() => showDocuments(documentBase, documentStatus))
                "
              >
                <option value="">全部知识库</option>
                <option v-for="b in bases" :key="b.id" :value="b.id">
                  {{ b.name }}
                </option>
              </select></label
            >
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
          <div class="batch-toolbar">
            <label class="check-label"
              ><input
                type="checkbox"
                :checked="allVisibleSelected"
                :indeterminate="
                  selectedDocuments.length > 0 && !allVisibleSelected
                "
                :disabled="busy || !eligibleDocs.length"
                @change="selectVisible"
              />选择本页待审核（最多 100 篇）</label
            >
            <span>已选择 {{ selectedDocuments.length }} 篇</span>
            <button
              :disabled="busy || !!syncWarning || !selectedDocuments.length"
              @click="beginBatch"
            >
              批量发布
            </button>
            <button
              v-if="selectedDocuments.length"
              class="secondary"
              :disabled="busy"
              @click="selections = {}"
            >
              清空选择
            </button>
          </div>
          <section
            v-if="batchResult"
            class="batch-result"
            role="status"
            aria-label="批量发布结果"
          >
            <strong
              >{{ batchResult.queued }} 篇已提交索引，{{
                batchResult.failed
              }}
              篇未提交</strong
            >
            <p>
              提交不等于发布完成。Worker
              索引成功后才进入检索；旧发布版本在此期间继续可用。
            </p>
            <ul v-if="batchResult.failed">
              <li
                v-for="item in batchResult.results.filter(
                  (row) => row.status === 'failed',
                )"
                :key="item.document_id"
              >
                {{ batchResult.titles[item.document_id] }}：{{
                  item.error?.message
                }}（{{ item.error?.code }}）
              </li>
            </ul>
            <button class="secondary" @click="tab = 'jobs'">
              查看处理任务
            </button>
          </section>
          <dialog
            ref="batchDialog"
            class="batch-dialog"
            aria-labelledby="batch-title"
            @cancel="busy && $event.preventDefault()"
          >
            <h2 id="batch-title">确认批量发布</h2>
            <p>
              将提交以下
              {{ batchSnapshot.length }}
              篇草稿。仅发布此处所列修订，审核后发生变化的文档会被拒绝。
            </p>
            <p v-if="error" class="error" role="alert">{{ error }}</p>
            <ul class="batch-review-list">
              <li v-for="doc in batchSnapshot" :key="doc.id">
                <strong>{{ doc.title }}</strong
                ><small
                  >{{ ownership(doc).base }} · {{ ownership(doc).source }} ·
                  修订 {{ doc.revision
                  }}{{
                    doc.published_version
                      ? " · 替换已发布版本 v" + doc.published_version
                      : " · 首次发布"
                  }}</small
                >
              </li>
            </ul>
            <label class="check-label"
              ><input
                v-model="batchReviewed"
                type="checkbox"
                :disabled="busy"
              />我已逐篇审核内容、来源和适用范围</label
            >
            <div class="batch-dialog-actions">
              <button
                class="secondary"
                :disabled="busy"
                @click="batchDialog.close()"
              >
                取消</button
              ><button
                :disabled="busy || !batchReviewed"
                @click="action(submitBatch)"
              >
                {{
                  busy
                    ? "正在提交…"
                    : "确认发布 " + batchSnapshot.length + " 篇"
                }}
              </button>
            </div>
          </dialog>
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
            :ref="
              (drawer) =>
                drawer
                  ? documentDrawers.set(d.id, drawer)
                  : documentDrawers.delete(d.id)
            "
            :title="d.title"
            :ownership="ownership(d)"
            selectable
            :selected="!!selections[d.id]"
            :selection-disabled="
              busy ||
              d.status !== 'draft' ||
              d.knowledge_base_enabled === false ||
              (!selections[d.id] && selectedDocuments.length >= 100)
            "
            @select="(checked) => toggleSelection(d, checked)"
            :summary="
              '草稿修订 ' +
              d.revision +
              ' · 发布版本 ' +
              (d.published_version ?? '未发布')
            "
            :status="statusText(d.status)"
            :before-close="() => documentPanels.get(d.id)?.canClose() !== false"
            wide
            @open="activeDocumentId = d.id"
            @close="activeDocumentId = ''"
          >
            <DocumentDetails
              v-if="activeDocumentId === d.id"
              :ref="
                (panel) =>
                  panel
                    ? documentPanels.set(d.id, panel)
                    : documentPanels.delete(d.id)
              "
              :document="d"
              :api="api"
              :ai-ready="config.ai_refinement_ready"
              @changed="action(refresh)"
              @deleted="action(() => deletedDocument(d.id))"
            />
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
            :ref="
              (drawer) =>
                drawer
                  ? recordDrawers.set(r.id, drawer)
                  : recordDrawers.delete(r.id)
            "
            :title="r.payload?.title || r.source_record_id"
            :summary="r.installation_id + ' · 修订 ' + r.source_revision"
            :status="statusText(r.status)"
            @open="activeRecordId = r.id"
            @close="activeRecordId = ''"
          >
            <SourceDetails
              v-if="activeRecordId === r.id"
              :record-id="r.id"
              :api="api"
              @changed="action(refresh)"
              @deleted="action(() => deletedRecord(r.id))"
            /> </WorkDrawer
        ></template>
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
          ><article class="search-panel">
            <div class="section-heading">
              <div>
                <h2>检索已发布知识</h2>
                <p>选择知识库，使用问题、命令或错误码验证命中结果。</p>
              </div>
            </div>
            <form class="search-form" @submit.prevent="action(search)">
              <label
                >知识库<select
                  v-model="selectedBase"
                  aria-label="知识库"
                  required
                  :disabled="busy"
                >
                  <option value="" disabled>请选择知识库</option>
                  <option
                    v-for="b in bases"
                    :value="b.id"
                    :disabled="!b.enabled"
                  >
                    {{ b.name }}{{ b.enabled ? "" : "（停用）" }}
                  </option>
                </select></label
              ><label
                >查询<input
                  v-model="query"
                  required
                  :disabled="busy"
                  maxlength="2000"
                  placeholder="输入问题、命令、路径或错误码" /></label
              ><button :disabled="busy || !selectedBase || !query.trim()">
                {{ busy ? "正在检索…" : "检索已发布知识" }}
              </button>
            </form>
            <div
              v-if="selectedSearchCorpus"
              class="search-corpus"
              aria-label="所选知识库状态"
            >
              <span
                ><strong>{{ selectedSearchCorpus.searchable }}</strong>
                篇可检索</span
              ><span>{{ selectedSearchCorpus.drafts }} 篇待审核</span
              ><span>{{ selectedSearchCorpus.indexing }} 篇索引中</span
              ><button
                v-if="selectedSearchCorpus.drafts"
                class="secondary"
                :disabled="busy"
                @click="action(() => showDocuments(selectedBase, 'draft'))"
              >
                查看待审核文档
              </button>
            </div>
            <p
              v-if="!hasSearched && config.embedding_enabled === false"
              class="search-info"
            >
              当前使用关键词检索，无需配置
              Embedding。只有已发布且索引完成的文档参与检索。
            </p>
            <p v-if="searchMode" class="search-mode" role="status">
              检索完成 · {{ hits.length }} 个片段 · 检索模式：{{
                { keyword_only: "关键词检索", hybrid: "关键词 + 向量混合检索" }[
                  searchMode
                ] || searchMode
              }}
            </p>
            <p v-if="searchEmbedding === 'disabled'" class="search-info">
              未配置向量服务，关键词检索正常可用；这不是检索错误。
            </p>
            <p
              v-for="warning in searchWarnings"
              :key="warning"
              class="search-warning"
              role="status"
            >
              {{ warning }}
            </p>
            <p v-if="searchTruncated" role="status">
              结果达到内容长度限制，部分正文已截断。请打开引用版本查看完整内容。
            </p>
            <p v-if="hasSearched && !hits.length" class="empty-state">
              {{ emptySearchMessage }}
            </p>
          </article>
          <WorkDrawer
            :title="h.title"
            :summary="
              baseName(h.knowledge_base_id || selectedBase) +
              ' · 版本 ' +
              h.document_version +
              ' · ' +
              h.citation.label
            "
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
            <button
              class="secondary"
              :disabled="busy"
              @click="action(() => openCitation(h))"
            >
              打开引用版本
            </button>
          </WorkDrawer>
          <dialog
            ref="citationDialog"
            class="work-drawer wide"
            aria-label="引用版本详情"
          >
            <header>
              <h2>引用版本详情</h2>
              <button class="secondary" @click="citationDialog.close()">
                关闭
              </button>
            </header>
            <div class="drawer-content">
              <p v-if="error" class="error" role="alert">{{ error }}</p>
              <p v-if="!citationVersion && busy" role="status">
                正在加载引用版本…
              </p>
              <VersionPreview
                v-if="citationVersion"
                :version="citationVersion"
                :line-start="citationHit?.citation.line_start"
                :line-end="citationHit?.citation.line_end"
              />
              <button
                v-if="citationVersion?.source_record_ids?.length"
                class="secondary"
                :disabled="busy"
                @click="action(loadCitationSources)"
              >
                查看引用来源快照
              </button>
              <SourceEvidence
                v-for="source in citationSources"
                :key="source.id"
                :record="source"
              />
            </div></dialog
        ></template>
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
            <p v-if="j.error">
              任务未完成。请使用任务 ID
              <code>{{ j.id }}</code> 查看服务器日志；AI
              提炼请打开对应文档，核对当前草稿后重新提炼。
            </p>
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
          >{{ tab === "overview" ? "知识库列表" : "当前列表" }}第
          {{ offsets[pageResource] / 200 + 1 }} 页 · 本页
          {{ pageRows.length }} 条{{
            tab === "overview" ? "" : "（非总量）"
          }}</span
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
