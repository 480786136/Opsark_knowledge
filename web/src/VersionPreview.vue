<script setup>
import { computed } from "vue";
const props = defineProps({
  version: { type: Object, required: true },
  lineStart: Number,
  lineEnd: Number,
});
const lines = computed(() => (props.version.content || "").split("\n"));
</script>

<template>
  <section class="detail-section version-preview">
    <h3>{{ version.title }} · 发布快照 v{{ version.version }}</h3>
    <p>
      {{
        version.is_current
          ? "当前对外发布版本"
          : "历史快照，仅供管理员审核；不代表当前检索结果"
      }}
    </p>
    <p v-if="version.source_record_ids?.length" class="source-reference">
      快照来源：<code v-for="id in version.source_record_ids" :key="id"
        >{{ id }}
      </code>
    </p>
    <div class="version-content" tabindex="0" aria-label="版本正文及行号">
      <div
        v-for="(line, index) in lines"
        :key="index"
        :class="[
          'diff-line',
          {
            'citation-line':
              lineStart && index + 1 >= lineStart && index + 1 <= lineEnd,
          },
        ]"
      >
        <span class="line-number">{{ index + 1 }}</span>
        <pre>{{ line || " " }}</pre>
      </div>
    </div>
  </section>
</template>
