<script setup>
import { computed, onMounted, ref, watch } from "vue";
import SourceEvidence from "./SourceEvidence.vue";
import VersionPreview from "./VersionPreview.vue";

const props = defineProps({
  document: Object,
  api: Function,
  aiReady: Boolean,
});
const emit = defineEmits(["changed", "deleted"]);
const doc = ref(null),
  sources = ref([]),
  versions = ref([]),
  indexBuilds = ref([]),
  comparison = ref(null);
const versionOffset = ref(0);
const sourceOffset = ref(0),
  sourceDetails = ref({});
const editor = ref(null),
  selectedVersion = ref(null),
  pendingSource = ref("");
const busy = ref(false),
  loading = ref(false),
  error = ref(""),
  loadWarning = ref("");
let loadSequence = 0;
const path = (suffix = "") =>
  "knowledge/documents/" + encodeURIComponent(props.document.id) + suffix;
const labels = {
  draft: "待审核",
  indexing: "建立索引中",
  published: "已发布",
  rejected: "已拒绝",
  pending: "排队中",
  running: "处理中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  ready: "已完成",
  ready_for_review: "待审核",
  processed: "已处理",
  superseded: "已被新来源替代",
  received: "已接收",
  deleted: "已删除",
};
const statusText = (status) => labels[status] || status;
const pendingSources = computed(() =>
  (doc.value?.pending_source_record_ids || []).map(
    (id) =>
      sourceDetails.value[id] ||
      sources.value.find((record) => record.id === id) || { id },
  ),
);
const latestJobs = computed(() => {
  const kinds = new Set();
  return (doc.value?.latest_jobs || []).filter((job) => {
    if (kinds.has(job.kind)) return false;
    kinds.add(job.kind);
    return true;
  });
});
const refineJob = computed(() =>
  latestJobs.value.find((job) => job.kind === "refine"),
);
const publishJob = computed(() =>
  latestJobs.value.find((job) => job.kind === "publish"),
);
const activeRefinement = computed(() =>
  ["pending", "running"].includes(refineJob.value?.status),
);
const reindexJob = computed(() =>
  latestJobs.value.find((job) => job.kind === "reindex"),
);
const activeReindex = computed(() =>
  ["pending", "running"].includes(reindexJob.value?.status),
);
const source = computed(() => sourceDetails.value[doc.value?.source_record_id]);
const editable = computed(
  () =>
    doc.value &&
    !["indexing", "deleted", "rejected"].includes(doc.value.status),
);
const errorMessages = {
  AI_INCOMPLETE_OUTPUT:
    "模型输出不完整，原草稿保留。可调整模型配置后重新提炼。",
  AI_SCHEMA_INVALID: "模型输出未通过结构校验，原草稿保留。",
  AI_RESPONSE_INVALID: "模型响应格式无效，原草稿保留。",
  AI_INVALID_REFERENCE:
    "提炼引用不存在的证据，或事实缺少引用。请核对来源后重试。",
  AI_EVIDENCE_TYPE_MISMATCH:
    "结论与证据类型不匹配，原草稿保留。请核对来源或人工整理。",
  AI_TIMEOUT: "模型请求超时，原草稿保留。",
  AI_HTTP_ERROR: "模型服务返回失败，请检查服务配置后重试。",
  AI_CONNECTION_ERROR: "无法连接模型服务，请检查服务后重试。",
  AI_DATABASE_ERROR: "提炼结果保存失败，原草稿保留。",
  AI_SENSITIVE_CONTENT: "提炼触发敏感信息检查，请检查脱敏来源。",
  AI_STALE_DRAFT: "草稿已更新，旧提炼结果未覆盖当前草稿。",
  AI_REFINEMENT_FAILED: "提炼未成功，原草稿保留。可凭任务 ID 查看服务器日志。",
};
function jobError(job) {
  const code = String(job?.error || "").split(":")[0];
  return (
    errorMessages[code] || "任务未完成，可凭任务 ID 查看服务器日志并重试。"
  );
}
const comparedLines = computed(() => {
  const before = (comparison.value?.before_content || "").split("\n");
  const after = (comparison.value?.after_content || "").split("\n");
  const left = new Set(before),
    right = new Set(after);
  return {
    before: before.map((text, index) => ({
      text,
      number: index + 1,
      changed: !right.has(text),
    })),
    after: after.map((text, index) => ({
      text,
      number: index + 1,
      changed: !left.has(text),
    })),
  };
});
async function load() {
  const sequence = ++loadSequence;
  loading.value = true;
  try {
    const results = await Promise.allSettled([
      props.api(path()),
      props.api(path("/sources?offset=" + sourceOffset.value + "&limit=50")),
      props.api(path("/versions?offset=" + versionOffset.value + "&limit=50")),
      props.api(path("/comparison")),
      props.api(path("/index-builds")),
    ]);
    if (sequence !== loadSequence) return;
    if (results[0].status === "rejected") throw results[0].reason;
    doc.value = results[0].value;
    const failures = [];
    if (
      doc.value.source_record_id &&
      !sourceDetails.value[doc.value.source_record_id]
    ) {
      try {
        await loadSource(doc.value.source_record_id);
      } catch {
        failures.push("当前来源正文");
      }
      if (sequence !== loadSequence) return;
    }
    for (const [index, name, target] of [
      [1, "来源", sources],
      [2, "版本历史", versions],
      [3, "提炼对照", comparison],
      [4, "索引构建记录", indexBuilds],
    ]) {
      const result = results[index];
      if (result.status === "rejected") {
        failures.push(name);
        continue;
      }
      target.value =
        index === 3
          ? result.value.comparison
          : Array.isArray(result.value)
            ? result.value
            : result.value[index === 1 ? "sources" : "versions"] || [];
    }
    loadWarning.value = failures.length
      ? `${failures.join("、")}加载失败，请刷新详情后重试。`
      : "";
    if (
      !pendingSources.value.some((record) => record.id === pendingSource.value)
    )
      pendingSource.value = pendingSources.value[0]?.id || "";
    if (selectedVersion.value) {
      selectedVersion.value = {
        ...selectedVersion.value,
        is_current:
          selectedVersion.value.version === doc.value.published_version,
      };
    }
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}
async function run(operation) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await operation();
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function changed() {
  await load();
  emit("changed");
}
async function save() {
  const body = Object.fromEntries(
    [
      "knowledge_base_id",
      "title",
      "content",
      "tags",
      "environment",
      "software_names",
      "context",
      "revision",
    ].map((key) => [key, editor.value[key]]),
  );
  await props.api(path("/draft"), "PATCH", body);
  editor.value = null;
  await changed();
}
async function refine() {
  if (
    !confirm(
      "将把脱敏来源发送到配置的模型，提炼结果会替换当前草稿，仍需人工审核后发布。已有发布版本继续可用。是否继续？",
    )
  )
    return;
  await props.api(path("/refine"), "POST", { revision: doc.value.revision });
  await changed();
}
async function publish() {
  if (
    !confirm(
      "确认已审核正文、适用条件和来源证据？索引成功后此草稿将成为当前发布版本，原发布版本停止出现在检索中。",
    )
  )
    return;
  await props.api(path("/publish"), "POST", { revision: doc.value.revision });
  await changed();
}
async function unpublish() {
  if (
    !confirm(
      "下架将立即停止此文档的检索和客户端引用，并取消正在进行的发布或提炼任务。草稿和历史快照保留，是否继续？",
    )
  )
    return;
  await props.api(path("/unpublish"), "POST");
  await changed();
}
async function replaceSource() {
  if (
    !pendingSource.value ||
    !confirm(
      "使用选中的新来源重新生成规则草稿？这将替换当前草稿及人工修改，已有发布版本保持可用。请确认已保存需要保留的内容。",
    )
  )
    return;
  await props.api(path("/source"), "POST", {
    revision: doc.value.revision,
    source_record_id: pendingSource.value,
  });
  editor.value = null;
  await changed();
}
async function remove() {
  if (
    !confirm(
      "永久删除此文档的正文、全部发布版本和索引，并取消相关任务？检索和客户端引用将立即失效；来源记录单独保留。此操作不能恢复。",
    )
  )
    return;
  await props.api(path(), "DELETE");
  emit("deleted");
}
async function preview(version) {
  selectedVersion.value = await props.api(path("/versions/" + version));
}
async function retryPublish() {
  await props.api(
    "knowledge/jobs/" + encodeURIComponent(publishJob.value.id) + "/retry",
    "POST",
  );
  await changed();
}
async function reindex() {
  await props.api(path("/reindex"), "POST", { revision: doc.value.revision });
  await changed();
}
async function changeVersionPage(delta) {
  versionOffset.value = Math.max(0, versionOffset.value + delta * 50);
  await load();
}
async function loadSource(id) {
  const record = await props.api("knowledge/records/" + encodeURIComponent(id));
  sourceDetails.value = { ...sourceDetails.value, [id]: record };
}
async function changeSourcePage(delta) {
  const nextOffset = Math.max(0, sourceOffset.value + delta * 50);
  const result = await props.api(
    path("/sources?offset=" + nextOffset + "&limit=50"),
  );
  sources.value = result;
  sourceOffset.value = nextOffset;
}
// The list poll also catches job completion/failure where the draft did not change.
watch(
  () => props.document,
  () => {
    if (!busy.value) run(load);
  },
);
defineExpose({
  canClose: () =>
    !editor.value ||
    ["title", "content", "environment"].every(
      (key) => editor.value[key] === doc.value[key],
    ) ||
    confirm("草稿还有未保存的修改，确认放弃修改并关闭？"),
});
onMounted(() => run(load));
</script>

<template>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <p v-if="loadWarning" class="error" role="status">{{ loadWarning }}</p>
  <p v-if="!doc && loading" role="status">正在加载文档、来源与版本…</p>
  <template v-if="doc">
    <div class="row">
      <h2>{{ doc.title }}</h2>
      <span :class="['badge', doc.status]">{{ statusText(doc.status) }}</span>
    </div>
    <p>
      草稿修订 {{ doc.revision }} · 当前发布版本
      {{
        doc.published_version == null ? "未发布" : "v" + doc.published_version
      }}
    </p>
    <dl class="document-provenance">
      <div>
        <dt>所属知识库</dt>
        <dd>
          {{
            doc.knowledge_base_name ||
            document.knowledge_base_name ||
            doc.knowledge_base_id
          }}
        </dd>
      </div>
      <div>
        <dt>来源类型</dt>
        <dd>
          {{
            {
              core_record: "Core 任务记录",
              reference_pack: "参考知识包",
              manual: "手工创建 / 文件导入",
            }[doc.source_kind] || "来源未标记"
          }}
        </dd>
      </div>
      <div>
        <dt>创建者</dt>
        <dd>{{ doc.created_by || "历史记录未留存" }}</dd>
      </div>
    </dl>
    <p v-if="doc.created_by_kind === 'client'" class="source-reference">
      来源记录只提供上传客户端标识，未记录实际操作人员。
    </p>
    <p
      v-if="doc.published_version != null && doc.status !== 'published'"
      role="status"
    >
      当前草稿尚未发布。检索与客户端引用仍使用 v{{
        doc.published_version
      }}，新版本索引成功后才会切换。
    </p>
    <p v-else-if="doc.status === 'indexing'" role="status">
      发布索引正在构建，成功后才能检索到此文档。
    </p>

    <form v-if="editor" class="inline-detail-form" @submit.prevent="run(save)">
      <h3>编辑草稿</h3>
      <p v-if="editor.revision !== doc.revision" class="error" role="alert">
        服务器草稿已更新。当前输入保留，保存可能发生版本冲突；请先复制需要保留的内容再取消并重新编辑。
      </p>
      <div class="detail-grid">
        <label class="full"
          >标题<input v-model="editor.title" required maxlength="200"
        /></label>
        <label class="full"
          >环境<input
            v-model="editor.environment"
            placeholder="例如 production；未知时留空"
        /></label>
        <label class="full"
          >正文（纯文本 / Markdown）<textarea
            v-model="editor.content"
            rows="18"
            required
          />
        </label>
      </div>
      <div class="form-actions">
        <button :disabled="busy || editor.revision !== doc.revision">
          保存草稿</button
        ><button
          type="button"
          class="secondary"
          :disabled="busy"
          @click="editor = null"
        >
          取消编辑
        </button>
      </div>
    </form>
    <div v-else class="form-actions document-actions">
      <button
        class="secondary"
        :disabled="busy || !editable"
        @click="editor = { ...doc }"
      >
        编辑草稿
      </button>
      <button
        :disabled="
          busy || !editable || doc.status === 'published' || activeRefinement
        "
        @click="run(publish)"
      >
        审核并发布
      </button>
      <button
        v-if="doc.source_record_id"
        class="secondary"
        :disabled="
          busy || !aiReady || doc.status !== 'draft' || activeRefinement
        "
        @click="run(refine)"
      >
        AI 重新提炼
      </button>
      <button
        v-if="doc.published_version != null || doc.status === 'indexing'"
        class="secondary"
        :disabled="busy"
        @click="run(unpublish)"
      >
        下架 / 取消发布
      </button>
      <button class="secondary danger" :disabled="busy" @click="run(remove)">
        删除文档
      </button>
    </div>

    <section
      v-if="latestJobs.length"
      class="detail-section"
      aria-label="文档处理状态"
    >
      <h3>处理状态</h3>
      <div v-for="job in latestJobs" :key="job.id">
        <p>
          {{
            job.kind === "refine"
              ? "AI 提炼"
              : job.kind === "publish"
                ? "发布索引"
                : job.kind === "reindex"
                  ? "重建索引"
                  : "生成草稿"
          }}：{{ statusText(job.status) }} · 任务 ID：<code>{{ job.id }}</code>
        </p>
        <p v-if="job.error" class="error" role="status">{{ jobError(job) }}</p>
      </div>
      <button
        v-if="publishJob?.status === 'failed'"
        class="secondary"
        :disabled="busy"
        @click="run(retryPublish)"
      >
        重试发布索引
      </button>
    </section>

    <section v-if="pendingSources.length" class="detail-section pending-source">
      <h3>待处理的新来源</h3>
      <p>新来源已接收，当前人工草稿保留。选择来源后可重新生成规则草稿。</p>
      <label
        >替换草稿的来源<select
          v-model="pendingSource"
          :disabled="busy || !editable || !!editor"
        >
          <option
            v-for="record in pendingSources"
            :key="record.id"
            :value="record.id"
          >
            修订 {{ record.source_revision ?? "—" }} ·
            {{
              record.title ||
              record.payload?.title ||
              record.source_record_id ||
              "未展开来源"
            }}
            ·
            {{ record.id }}
          </option>
        </select></label
      >
      <button
        class="secondary"
        :disabled="busy || !editable || !!editor || !pendingSource"
        @click="run(replaceSource)"
      >
        采用新来源生成草稿
      </button>
      <button
        class="secondary"
        :disabled="busy || !pendingSource"
        @click="run(() => loadSource(pendingSource))"
      >
        查看待处理来源
      </button>
      <SourceEvidence
        v-if="sourceDetails[pendingSource]"
        :record="sourceDetails[pendingSource]"
      />
    </section>

    <section class="detail-section" aria-label="来源证据">
      <h3>来源证据</h3>
      <p v-if="!doc.source_record_id">手工创建的文档，没有关联任务来源。</p>
      <p v-else-if="!source && !loading">
        当前来源未能加载，请刷新详情后重试。
      </p>
      <details v-if="source" open>
        <summary>
          当前草稿来源 · 修订 {{ source.source_revision }} ·
          {{ source.payload?.title || source.source_record_id }}
        </summary>
        <SourceEvidence :record="source" />
      </details>
      <p>关联来源摘要 · 第 {{ sourceOffset / 50 + 1 }} 页，正文按需加载。</p>
      <details
        v-for="record in sources.filter(
          (item) => item.id !== doc.source_record_id,
        )"
        :key="record.id"
      >
        <summary>
          关联来源 · 修订 {{ record.source_revision }} ·
          {{ record.title || record.source_record_id }}
        </summary>
        <p class="source-reference">
          来源 ID：<code>{{ record.id }}</code> ·
          {{ statusText(record.status) }}
        </p>
        <SourceEvidence
          v-if="sourceDetails[record.id]"
          :record="sourceDetails[record.id]"
        />
        <button
          v-else
          class="secondary"
          :disabled="busy"
          @click="run(() => loadSource(record.id))"
        >
          加载来源正文
        </button>
      </details>
      <div v-if="sourceOffset || sources.length >= 50" class="form-actions">
        <button
          class="secondary"
          :disabled="busy || !sourceOffset"
          @click="run(() => changeSourcePage(-1))"
        >
          较新来源
        </button>
        <button
          class="secondary"
          :disabled="busy || sources.length < 50"
          @click="run(() => changeSourcePage(1))"
        >
          较早来源
        </button>
      </div>
    </section>

    <section v-if="!editor" class="inline-comparison detail-section">
      <h3>提炼前后对照</h3>
      <p v-if="comparison">
        对照结果修订 {{ comparison.applied_revision }} · 当前草稿修订
        {{ doc.revision }}。高亮为本侧独有行；历史提炼结果不代表当前草稿。
      </p>
      <div v-if="comparison" class="comparison-grid">
        <section
          v-for="side in ['before', 'after']"
          :key="side"
          :class="['comparison-pane', side]"
        >
          <h3>
            {{ side === "before" ? "提炼前 · 历史草稿" : "提炼后 · AI 结果" }}
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
    <section v-if="!editor" class="detail-section">
      <h3>当前草稿 · 修订 {{ doc.revision }}</h3>
      <pre>{{ doc.content }}</pre>
    </section>

    <section class="detail-section" aria-label="发布版本历史">
      <h3>发布版本历史</h3>
      <p>
        草稿修订号记录编辑变化；发布版本号标识审核时保存的不可变快照。只有当前发布版本对外可读。
      </p>
      <p v-if="!versions.length">尚无发布快照。</p>
      <div
        v-for="version in versions"
        :key="version.version"
        class="version-row"
      >
        <span
          >v{{ version.version }} ·
          {{ version.is_current ? "当前发布" : "历史快照（非当前）" }}</span
        >
        <span v-if="version.created_at" class="source-reference">{{
          new Date(version.created_at * 1000).toLocaleString()
        }}</span>
        <button
          class="secondary"
          :disabled="busy"
          @click="run(() => preview(version.version))"
        >
          查看 v{{ version.version }}
        </button>
      </div>
      <div v-if="versionOffset || versions.length >= 50" class="form-actions">
        <button
          class="secondary"
          :disabled="busy || !versionOffset"
          @click="run(() => changeVersionPage(-1))"
        >
          较新版本
        </button>
        <span>第 {{ versionOffset / 50 + 1 }} 页</span>
        <button
          class="secondary"
          :disabled="busy || versions.length < 50"
          @click="run(() => changeVersionPage(1))"
        >
          较早版本
        </button>
      </div>
      <VersionPreview v-if="selectedVersion" :version="selectedVersion" />
    </section>
    <section class="detail-section" aria-label="发布索引维护">
      <h3>发布索引维护</h3>
      <p>
        重建使用当前服务的索引配置和当前发布正文。构建成功后切换索引，构建期间现有检索继续可用。
      </p>
      <button
        class="secondary"
        :disabled="
          busy ||
          doc.published_version == null ||
          doc.status === 'indexing' ||
          activeReindex
        "
        @click="run(reindex)"
      >
        {{ activeReindex ? "正在重建索引…" : "重建当前发布索引" }}
      </button>
      <p v-if="!indexBuilds.length">暂无索引构建记录。</p>
      <p v-for="build in indexBuilds.slice(0, 5)" :key="build.id">
        {{ statusText(build.status) }} · {{ build.chunk_count ?? 0 }} 个片段 ·
        构建 ID：<code>{{ build.id }}</code>
      </p>
      <p v-if="indexBuilds.length > 5" class="source-reference">
        显示最近 5 次构建。
      </p>
    </section>
    <section v-if="doc.legacy_documents?.length" class="detail-section">
      <h3>合并前的历史文档</h3>
      <p>这些文档已归入当前逻辑文档，保留标识用于审计。</p>
      <p v-for="legacy in doc.legacy_documents" :key="legacy.id">
        {{ legacy.title }} · <code>{{ legacy.id }}</code> · 原发布版本
        {{ legacy.published_version ?? "未发布" }}
      </p>
    </section>
  </template>
  <button class="secondary" :disabled="busy" @click="run(load)">
    {{ loading ? "加载中…" : "刷新详情" }}
  </button>
</template>
