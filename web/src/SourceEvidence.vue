<script setup>
defineProps({ record: { type: Object, required: true } });
</script>

<template>
  <div class="source-evidence">
    <p>{{ record.payload?.title || record.source_record_id || "来源记录" }}</p>
    <p class="source-reference">
      客户端 {{ record.installation_id || "未知" }} · 来源修订
      {{ record.source_revision ?? "—" }}
    </p>
    <p class="source-reference">
      来源 ID：<code>{{ record.id }}</code
      ><br />
      原始任务 ID：<code>{{ record.source_record_id || "—" }}</code>
    </p>
    <pre>{{ JSON.stringify(record.payload, null, 2) }}</pre>
  </div>
</template>
