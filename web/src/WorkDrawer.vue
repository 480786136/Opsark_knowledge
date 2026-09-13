<script setup>
import { ref } from "vue";
defineProps({
  title: String,
  summary: String,
  status: String,
  create: Boolean,
  wide: Boolean,
  error: String,
});
const dialog = ref(null);
const emit = defineEmits(["open", "close"]);
defineExpose({ close: () => dialog.value?.close() });
function open() {
  emit("open");
  dialog.value.showModal();
}
</script>
<template>
  <button v-if="create" class="secondary" @click="open">{{ title }}</button>
  <div v-else class="compact-record">
    <button class="record-title" :title="title" @click="open">
      {{ title }}
    </button>
    <span class="record-summary" :title="summary">{{ summary }}</span>
    <span class="badge">{{ status }}</span>
    <button class="secondary" @click="open">详情</button>
  </div>
  <dialog
    ref="dialog"
    :class="['work-drawer', { wide }]"
    :aria-label="title"
    @close="emit('close')"
  >
    <header>
      <h2>{{ title }}</h2>
      <button class="secondary" @click="dialog.close()">关闭</button>
    </header>
    <div class="drawer-content">
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <slot />
    </div>
  </dialog>
</template>
