<script setup>
import { ref } from "vue";
const props = defineProps({
  title: String,
  summary: String,
  status: String,
  create: Boolean,
  wide: Boolean,
  error: String,
  beforeClose: Function,
  ownership: Object,
  selectable: Boolean,
  selected: Boolean,
  selectionDisabled: Boolean,
});
const dialog = ref(null);
const emit = defineEmits(["open", "close", "select"]);
function close() {
  if (props.beforeClose?.() === false) return;
  dialog.value?.close();
}
defineExpose({ close });
function open() {
  emit("open");
  dialog.value.showModal();
}
</script>
<template>
  <button v-if="create" class="secondary" @click="open">{{ title }}</button>
  <div v-else :class="['compact-record', { 'document-record': ownership }]">
    <input
      v-if="selectable"
      type="checkbox"
      class="document-check"
      :aria-label="'选择 ' + title"
      :checked="selected"
      :disabled="selectionDisabled"
      @change="emit('select', $event.target.checked)"
    />
    <div class="record-main">
      <button class="record-title" :title="title" @click="open">
        {{ title }}
      </button>
      <span v-if="ownership" class="record-version">{{ summary }}</span>
    </div>
    <div v-if="ownership" class="record-ownership">
      <span class="base-name">{{ ownership.base }}</span>
      <small>{{ ownership.source }} · 创建者：{{ ownership.creator }}</small>
    </div>
    <span v-else class="record-summary" :title="summary">{{ summary }}</span>
    <span class="badge">{{ status }}</span>
    <button class="secondary" @click="open">详情</button>
  </div>
  <dialog
    ref="dialog"
    :class="['work-drawer', { wide }]"
    :aria-label="title"
    @close="emit('close')"
    @cancel.prevent="close"
  >
    <header>
      <h2>{{ title }}</h2>
      <button class="secondary" @click="close">关闭</button>
    </header>
    <div class="drawer-content">
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <slot />
    </div>
  </dialog>
</template>
