<script setup>
import { computed } from "vue";
const props = defineProps({ overview: Object, config: Object, busy: Boolean });
defineEmits(["documents", "search", "jobs"]);
const totals = computed(() => props.overview?.totals || {});
const worker = computed(() => props.overview?.worker || {});
</script>

<template>
  <section class="overview-panel" aria-label="知识工作概览">
    <div v-if="!overview" class="overview-loading" role="status">
      正在汇总知识库与发布状态…
    </div>
    <template v-else>
      <div class="overview-intro">
        <div>
          <h2>知识资产与待办</h2>
          <p>先审核，再发布。只有索引完成的已发布版本可被检索。</p>
        </div>
        <span class="overview-update"
          >更新于
          {{
            new Date(overview.generated_at * 1000).toLocaleTimeString()
          }}</span
        >
      </div>
      <dl class="overview-metrics">
        <div>
          <dt>知识库</dt>
          <dd>{{ totals.knowledge_bases }}</dd>
          <small>独立组织知识</small>
        </div>
        <div>
          <dt>可检索文档</dt>
          <dd>{{ totals.searchable }}</dd>
          <small>已发布且索引就绪</small>
        </div>
        <div class="metric-attention">
          <dt>待审核草稿</dt>
          <dd>{{ totals.drafts }}</dd>
          <small>含已发布文档的新修订</small>
        </div>
        <div>
          <dt>发布索引中</dt>
          <dd>{{ totals.indexing }}</dd>
          <small>完成后进入检索</small>
        </div>
      </dl>
      <div class="overview-workline">
        <div>
          <strong>{{
            totals.drafts
              ? `有 ${totals.drafts} 篇草稿等待审核`
              : "当前没有待审核草稿"
          }}</strong>
          <p>
            {{
              totals.drafts
                ? "进入知识文档可筛选知识库、核对来源并批量发布。"
                : "可以检查已发布知识的检索效果，或添加新的知识。"
            }}
          </p>
        </div>
        <button :disabled="busy" @click="$emit('documents', '', 'draft')">
          查看待审核草稿
        </button>
      </div>
      <div class="overview-columns">
        <section class="library-section" aria-label="知识库资产">
          <div class="section-heading">
            <div>
              <h2>知识库</h2>
              <p>每个库的完整统计，不受文档列表分页影响。</p>
            </div>
          </div>
          <p v-if="!overview.bases.length" class="empty-state">
            还没有知识库。先新建一个库，再创建或导入知识草稿。
          </p>
          <article
            v-for="base in overview.bases"
            :key="base.id"
            class="library-row"
          >
            <div class="library-heading">
              <h3>{{ base.name }}</h3>
              <span :class="['badge', { 'badge-muted': !base.enabled }]">{{
                base.enabled ? "启用" : "停用"
              }}</span>
            </div>
            <p v-if="base.description" class="library-description">
              {{ base.description }}
            </p>
            <div class="library-counts">
              <span
                ><strong>{{ base.searchable }}</strong> 可检索</span
              ><span
                ><strong>{{ base.drafts }}</strong> 待审核</span
              ><span
                ><strong>{{ base.indexing }}</strong> 索引中</span
              ><span>{{ base.documents }} 篇文档</span>
            </div>
            <div class="library-footer">
              <label class="library-id"
                >知识库 ID<input
                  :value="base.id"
                  readonly
                  :aria-label="base.name + ' 的知识库 ID'"
                  @focus="$event.target.select()"
              /></label>
              <div class="library-actions">
                <button
                  class="secondary"
                  :disabled="busy"
                  @click="$emit('documents', base.id, '')"
                >
                  查看文档</button
                ><button
                  class="secondary"
                  :disabled="busy || !base.enabled"
                  @click="$emit('search', base.id)"
                >
                  检索
                </button>
              </div>
            </div>
          </article>
        </section>
        <section class="service-panel" aria-label="服务状态">
          <h2>服务状态</h2>
          <dl>
            <div>
              <dt>后台处理</dt>
              <dd :class="{ 'state-warning': !worker.healthy }">
                {{
                  worker.healthy ? "Worker 正常" : "未检测到近期 Worker 心跳"
                }}
              </dd>
            </div>
            <div>
              <dt>处理队列</dt>
              <dd>{{ worker.pending }} 排队 · {{ worker.running }} 处理中</dd>
            </div>
            <div>
              <dt>检索能力</dt>
              <dd>
                {{
                  config.embedding_enabled
                    ? "关键词 + 可选向量"
                    : "关键词检索可用"
                }}
              </dd>
            </div>
            <div>
              <dt>AI 提炼</dt>
              <dd>
                {{
                  config.ai_refinement_ready ? "已配置" : "未启用，规则草稿可用"
                }}
              </dd>
            </div>
          </dl>
          <p v-if="!config.embedding_enabled" class="service-note">
            向量检索是可选增强，未配置不影响关键词检索与发布。
          </p>
          <p v-if="!worker.healthy" class="service-note state-warning">
            新提交的发布任务可能等待处理，请检查 Worker 进程。
          </p>
          <button
            class="secondary service-jobs"
            :disabled="busy"
            @click="$emit('jobs')"
          >
            查看处理任务<span v-if="worker.failed">
              · {{ worker.failed }} 个历史失败</span
            >
          </button>
        </section>
      </div>
    </template>
  </section>
</template>
