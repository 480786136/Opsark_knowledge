import { expect, test } from "@playwright/test";

async function setup(page, options = {}) {
  const source = {
    id: "source-outside-current-page",
    source_record_id: "core-task-42",
    source_revision: 1,
    installation_id: "local-core",
    status: "processed",
    payload: {
      title: "真实任务来源",
      execution_output: "nginx configuration is valid",
    },
  };
  const nextSource = {
    ...source,
    id: "source-new-revision",
    source_revision: 2,
    payload: {
      title: "后续来源修订",
      execution_output: "nginx reloaded successfully",
    },
  };
  const originalVersion = {
    id: "version-1",
    document_id: "doc-1",
    version: 1,
    title: "Nginx 配置处理",
    content: "# 已审核知识\nnginx -t\n验证成功",
    source_record_ids: [source.id],
    published: true,
    is_current: true,
    created_at: 1700000000,
  };
  const state = {
    document: {
      id: "doc-1",
      knowledge_base_id: "base-1",
      title: "Nginx 配置处理",
      content: "尚未发布的改进草稿",
      tags: ["keep-tag"],
      environment: "Linux",
      software_names: ["nginx"],
      context: {
        runtime: { os: "Linux", shell: "bash" },
        software: [{ name: "nginx", version: "1.26" }],
      },
      revision: 3,
      status: "draft",
      published_version: 1,
      source_record_id: source.id,
      pending_source_record_ids: [nextSource.id],
      latest_jobs: [],
      ...options.document,
    },
    sources: [source, nextSource],
    versions: [originalVersion],
    mutations: [],
    requests: [],
    deleted: false,
    sourceDeleted: false,
  };
  if (options.manySources) {
    for (let revision = 3; revision <= 62; revision++) {
      state.sources.push({
        ...source,
        id: "source-filler-" + revision,
        source_revision: revision,
        status: "superseded",
        payload: {
          title: "历史来源 " + revision,
          execution_output: "old output " + revision,
        },
      });
    }
  }
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/admin/v1/", "");
    state.requests.push({ path, method: request.method() });
    const reply = (json, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(json),
      });
    if (request.method() !== "GET")
      state.mutations.push({
        path,
        method: request.method(),
        body: request.postDataJSON(),
      });
    if (path === "session")
      return reply({ username: "admin", csrf: "test-csrf" });
    if (path === "config")
      return reply({
        knowledge_connected: true,
        ai_refinement_ready: true,
        knowledge_base_url: "/api/v1",
      });
    if (path === "knowledge/knowledge-bases")
      return reply([{ id: "base-1", name: "测试知识库", enabled: true }]);
    if (path === "knowledge/overview")
      return reply({
        totals: {
          knowledge_bases: 1,
          documents: state.deleted ? 0 : 1,
          drafts: 1,
          indexing: 0,
          searchable: 1,
        },
        bases: [
          {
            id: "base-1",
            name: "测试知识库",
            enabled: true,
            documents: 1,
            drafts: 1,
            indexing: 0,
            searchable: 1,
          },
        ],
        worker: { healthy: true, pending: 0, running: 0, failed: 0 },
        generated_at: 1700000000,
      });
    if (path === "knowledge/documents")
      return reply(state.deleted ? [] : [state.document]);
    // Deliberately omit the current document source from the inbox page.
    if (path === "knowledge/records")
      return reply([
        state.sourceDeleted
          ? { ...nextSource, status: "deleted", payload: {} }
          : nextSource,
      ]);
    if (["knowledge/jobs", "knowledge/knowledge-keys"].includes(path))
      return reply([]);
    if (path === "knowledge/documents/doc-1/sources")
      return reply(
        [...state.sources]
          .sort((a, b) => b.source_revision - a.source_revision)
          .slice(
            Number(url.searchParams.get("offset") || 0),
            Number(url.searchParams.get("offset") || 0) +
              Number(url.searchParams.get("limit") || 50),
          )
          .map(({ payload, ...summary }) => ({
            ...summary,
            title: payload.title,
          })),
      );
    if (path === "knowledge/documents/doc-1/index-builds") return reply([]);
    if (path === "knowledge/documents/doc-1/versions")
      return reply(state.versions);
    if (path === "knowledge/documents/doc-1/comparison")
      return reply({
        comparison: null,
        current_revision: state.document.revision,
      });
    if (path === "knowledge/documents/doc-1/versions/1")
      return reply({
        ...originalVersion,
        is_current: state.document.published_version === 1,
      });
    if (path === "knowledge/documents/doc-1" && request.method() === "GET")
      return reply(state.document);
    if (path === "knowledge/documents/doc-1" && request.method() === "DELETE") {
      state.deleted = true;
      return reply({ deleted: true });
    }
    if (path === "knowledge/documents/doc-1/draft") {
      if (options.conflict)
        return reply(
          {
            error: {
              code: "REVISION_CONFLICT",
              message: "草稿已更新，请刷新后重试",
            },
          },
          409,
        );
      Object.assign(state.document, request.postDataJSON(), {
        revision: state.document.revision + 1,
        status: "draft",
      });
      return reply(state.document);
    }
    if (path === "knowledge/documents/doc-1/refine") {
      state.document.latest_jobs = [
        {
          id: "refine-new",
          kind: "refine",
          status: "pending",
          target_id: "doc-1",
        },
      ];
      return reply(state.document.latest_jobs[0], 202);
    }
    if (path === "knowledge/documents/doc-1/reindex") {
      state.document.latest_jobs = [
        {
          id: "reindex-new",
          kind: "reindex",
          status: "pending",
          target_id: "doc-1",
        },
      ];
      return reply({ job_id: "reindex-new", document: state.document }, 202);
    }
    if (path === "knowledge/documents/doc-1/source") {
      Object.assign(state.document, {
        source_record_id: nextSource.id,
        pending_source_record_ids: [],
        content: "后续来源生成的规则草稿",
        revision: state.document.revision + 1,
      });
      return reply(state.document);
    }
    if (path === "knowledge/documents/doc-1/unpublish") {
      state.document.published_version = null;
      state.document.status = "draft";
      return reply(state.document);
    }
    if (
      path === "knowledge/records/" + nextSource.id &&
      request.method() === "DELETE"
    ) {
      state.sourceDeleted = true;
      return reply({ deleted: true });
    }
    if (path.startsWith("knowledge/records/"))
      return reply(
        state.sources.find((item) => item.id === path.split("/").at(-1)),
      );
    if (path === "knowledge/search")
      return reply({
        retrieval_mode: "keyword_only",
        truncated: true,
        warnings: ["Embedding 不可用，当前使用关键词检索"],
        hits: [
          {
            chunk_id: "chunk-1",
            document_id: "doc-1",
            document_version: 1,
            title: originalVersion.title,
            content: "nginx -t",
            citation: {
              label: "K1",
              line_start: 2,
              line_end: 2,
              source_record_ids: [source.id],
              document_url: "/api/v1/knowledge/documents/doc-1/versions/1",
            },
          },
        ],
      });
    return reply({ error: { code: "UNMOCKED_ENDPOINT", message: path } }, 404);
  });
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "知识文档", exact: true }),
  ).toBeVisible();
  return state;
}

async function openDocument(page) {
  await page.getByRole("button", { name: "知识文档", exact: true }).click();
  await page
    .getByRole("button", { name: "Nginx 配置处理", exact: true })
    .click();
  const drawer = page.getByRole("dialog", {
    name: "Nginx 配置处理",
    exact: true,
  });
  await expect(
    drawer.getByText("当前草稿尚未发布。", { exact: false }),
  ).toBeVisible();
  return drawer;
}

test("source evidence and publication history do not depend on the current inbox page", async ({
  page,
}) => {
  const state = await setup(page, {
    document: {
      latest_jobs: [
        {
          id: "older-refine-job",
          kind: "refine",
          status: "failed",
          error: "AI_TIMEOUT",
        },
      ],
    },
  });
  const drawer = await openDocument(page);
  await expect(drawer.locator(".source-evidence").first()).toContainText(
    "nginx configuration is valid",
  );
  await expect(
    drawer.getByText("模型请求超时，原草稿保留。", { exact: true }),
  ).toBeVisible();
  await expect(
    drawer.getByRole("button", { name: "AI 重新提炼" }),
  ).toBeEnabled();
  await drawer.getByRole("button", { name: "查看 v1", exact: true }).click();
  await expect(drawer.getByLabel("版本正文及行号")).toContainText("已审核知识");
  await expect(
    drawer.getByText("当前对外发布版本", { exact: true }),
  ).toBeVisible();
  expect(
    state.requests.some((request) => request.path.endsWith("/versions/1")),
  ).toBe(true);
  expect(state.mutations).toHaveLength(0);
});

test("editing and refining a new draft preserve the existing publication", async ({
  page,
}) => {
  const state = await setup(page);
  const drawer = await openDocument(page);
  await drawer.getByRole("button", { name: "编辑草稿", exact: true }).click();
  await drawer
    .getByLabel("正文（纯文本 / Markdown）", { exact: true })
    .fill("人工校验后的新草稿");
  await drawer.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    drawer.getByRole("heading", { name: "当前草稿 · 修订 4", exact: true }),
  ).toBeVisible();
  await expect(
    drawer.getByText("人工校验后的新草稿", { exact: true }),
  ).toBeVisible();
  expect(state.document.published_version).toBe(1);
  expect(state.mutations[0].body.tags).toEqual(["keep-tag"]);
  expect(state.mutations[0].body.software_names).toEqual(["nginx"]);
  expect(state.mutations[0].body.context).toEqual({
    runtime: { os: "Linux", shell: "bash" },
    software: [{ name: "nginx", version: "1.26" }],
  });
  page.once("dialog", (dialog) => dialog.accept());
  await drawer
    .getByRole("button", { name: "AI 重新提炼", exact: true })
    .click();
  await expect(
    drawer.getByText("AI 提炼：排队中", { exact: false }),
  ).toBeVisible();
  expect(state.mutations.map((request) => request.path)).toEqual([
    "knowledge/documents/doc-1/draft",
    "knowledge/documents/doc-1/refine",
  ]);
});

test("revision conflict keeps administrator input for recovery", async ({
  page,
}) => {
  await setup(page, { conflict: true });
  const drawer = await openDocument(page);
  await drawer.getByRole("button", { name: "编辑草稿", exact: true }).click();
  const content = drawer.getByLabel("正文（纯文本 / Markdown）", {
    exact: true,
  });
  await content.fill("不可丢失的人工修改");
  await drawer.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(drawer.getByRole("alert")).toContainText("草稿已更新");
  await expect(content).toHaveValue("不可丢失的人工修改");
  page.once("dialog", (dialog) => dialog.dismiss());
  await drawer.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(content).toBeVisible();
});

test("pending source replacement requires explicit acknowledgement and retains publication", async ({
  page,
}) => {
  const state = await setup(page);
  const drawer = await openDocument(page);
  page.once("dialog", (dialog) => dialog.dismiss());
  await drawer
    .getByRole("button", { name: "采用新来源生成草稿", exact: true })
    .click();
  expect(state.mutations).toHaveLength(0);
  page.once("dialog", (dialog) => dialog.accept());
  await drawer
    .getByRole("button", { name: "采用新来源生成草稿", exact: true })
    .click();
  await expect(
    drawer.getByText("后续来源生成的规则草稿", { exact: true }),
  ).toBeVisible();
  expect(state.document.source_record_id).toBe("source-new-revision");
  expect(state.document.published_version).toBe(1);
  expect(state.mutations[0].body).toEqual({
    revision: 3,
    source_record_id: "source-new-revision",
  });
});

test("search shows degraded and truncated results, opens citation through administrator route", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "检索调试", exact: true }).click();
  await page.getByLabel("查询", { exact: true }).fill("nginx 配置验证");
  await page
    .getByRole("button", { name: "检索已发布知识", exact: true })
    .click();
  await expect(
    page.getByText("Embedding 不可用，当前使用关键词检索", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("结果达到内容长度限制", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Nginx 配置处理", exact: true })
    .click();
  await page.getByRole("button", { name: "打开引用版本", exact: true }).click();
  const citation = page.getByRole("dialog", {
    name: "引用版本详情",
    exact: true,
  });
  await expect(citation.locator(".citation-line")).toHaveText("2nginx -t");
  await citation
    .getByRole("button", { name: "查看引用来源快照", exact: true })
    .click();
  await expect(citation.locator(".source-evidence")).toContainText(
    "nginx configuration is valid",
  );
  expect(
    state.requests.some((request) => request.path.startsWith("/api/v1/")),
  ).toBe(false);
});

test("document deletion explains impact and does not delete its source", async ({
  page,
}) => {
  const state = await setup(page);
  const drawer = await openDocument(page);
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toContain("来源记录单独保留");
    return dialog.dismiss();
  });
  await drawer.getByRole("button", { name: "删除文档", exact: true }).click();
  expect(state.deleted).toBe(false);
  page.once("dialog", (dialog) => dialog.accept());
  await drawer.getByRole("button", { name: "删除文档", exact: true }).click();
  await expect(
    page.getByText("尚无文档。创建草稿或从 Core 上传第一份任务记录。", {
      exact: true,
    }),
  ).toBeVisible();
  expect(state.sourceDeleted).toBe(false);
  expect(state.mutations.map((request) => request.path)).toEqual([
    "knowledge/documents/doc-1",
  ]);
});

test("source deletion loads full detail directly and confirms separate deletion", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "记录收件箱", exact: true }).click();
  await page.getByRole("button", { name: "后续来源修订", exact: true }).click();
  const drawer = page.getByRole("dialog", {
    name: "后续来源修订",
    exact: true,
  });
  await expect(drawer.locator(".source-evidence")).toContainText(
    "nginx reloaded successfully",
  );
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toContain("去重标记和审计记录保留");
    return dialog.accept();
  });
  await drawer
    .getByRole("button", { name: "删除来源正文", exact: true })
    .click();
  await expect(drawer).not.toBeVisible();
  expect(state.sourceDeleted).toBe(true);
  expect(state.deleted).toBe(false);
});

test("reindex targets the active publication without publishing the draft", async ({
  page,
}) => {
  const state = await setup(page);
  const drawer = await openDocument(page);
  await drawer
    .getByRole("button", { name: "重建当前发布索引", exact: true })
    .click();
  await expect(
    drawer.getByRole("button", { name: "正在重建索引…", exact: true }),
  ).toBeDisabled();
  expect(state.document.published_version).toBe(1);
  expect(state.document.content).toBe("尚未发布的改进草稿");
  expect(state.mutations.map((request) => request.path)).toEqual([
    "knowledge/documents/doc-1/reindex",
  ]);
});

test("source pagination keeps current evidence and off-page pending source actionable", async ({
  page,
}) => {
  const state = await setup(page, { manySources: true });
  const drawer = await openDocument(page);
  const evidence = drawer.getByRole("region", {
    name: "来源证据",
    exact: true,
  });
  await expect(evidence.locator(".source-evidence").first()).toContainText(
    "nginx configuration is valid",
  );
  await expect(
    drawer.getByRole("button", { name: "采用新来源生成草稿", exact: true }),
  ).toBeEnabled();
  expect(
    state.requests
      .filter((request) => request.path.startsWith("knowledge/records/"))
      .map((request) => request.path),
  ).toEqual(["knowledge/records/source-outside-current-page"]);
  await drawer
    .getByRole("button", { name: "查看待处理来源", exact: true })
    .click();
  await expect(
    drawer.locator(".pending-source .source-evidence"),
  ).toContainText("nginx reloaded successfully");
  await evidence.getByRole("button", { name: "较早来源", exact: true }).click();
  await expect(
    evidence.getByText("关联来源摘要 · 第 2 页", { exact: false }),
  ).toBeVisible();
  await expect(evidence.locator(".source-evidence").first()).toContainText(
    "nginx configuration is valid",
  );
  const historical = evidence
    .locator("details")
    .filter({ hasText: "关联来源 · 修订 12" });
  await historical.locator("summary").click();
  await historical
    .getByRole("button", { name: "加载来源正文", exact: true })
    .click();
  await expect(historical.locator(".source-evidence")).toContainText(
    "old output 12",
  );
});
