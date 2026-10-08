<script setup>
import { onMounted, ref } from "vue";
import SourceEvidence from "./SourceEvidence.vue";
const props = defineProps({ recordId: String, api: Function });
const emit = defineEmits(["changed", "deleted"]);
const record = ref(null),
  error = ref(""),
  busy = ref(false);
async function load() {
  record.value = await props.api(
    "knowledge/records/" + encodeURIComponent(props.recordId),
  );
}
async function run(operation) {
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
async function reject() {
  if (
    !confirm(
      "拒绝此来源及关联的未发布草稿？正文保留用于审计；已发布或被版本引用的来源无法直接拒绝。",
    )
  )
    return;
  await props.api(
    "knowledge/records/" + encodeURIComponent(props.recordId) + "/reject",
    "POST",
  );
  await load();
  emit("changed");
}
async function remove() {
  if (
    !confirm(
      "永久清除这条来源的脱敏正文，并取消相关处理任务？去重标记和审计记录保留。若来源仍被文档或版本引用，必须先处理关联文档。此操作不能恢复。",
    )
  )
    return;
  await props.api(
    "knowledge/records/" + encodeURIComponent(props.recordId),
    "DELETE",
  );
  emit("deleted");
}
onMounted(() => run(load));
</script>

<template>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <p v-if="!record && busy" role="status">正在加载完整来源…</p>
  <template v-if="record">
    <SourceEvidence :record="record" />
    <div class="form-actions">
      <button
        v-if="
          ['received', 'ready_for_review', 'failed'].includes(record.status)
        "
        :disabled="busy"
        @click="run(reject)"
      >
        拒绝审核
      </button>
      <button
        v-if="record.status !== 'deleted'"
        class="secondary danger"
        :disabled="busy"
        @click="run(remove)"
      >
        删除来源正文
      </button>
    </div>
    <p v-if="record.status === 'deleted'">
      来源正文已清除，仅保留去重和审计信息。
    </p>
  </template>
  <button v-else-if="!busy" class="secondary" @click="run(load)">
    重试加载来源
  </button>
</template>
