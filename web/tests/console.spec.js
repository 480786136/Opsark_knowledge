import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

const pack = JSON.parse(
  readFileSync(
    new URL(
      "../../knowledge_packs/ops_starter_v1/catalog.json",
      import.meta.url,
    ),
    "utf8",
  ),
);

async function setup(page, options = {}) {
  const bases = [
    {
      id: "reference-base",
      name: "运维参考知识",
      description: "基础设施排障与低影响检查，发布前需核对适用范围。",
      enabled: true,
    },
    {
      id: "project-base",
      name: "Opsark",
      description: "从 Core 任务记录沉淀的项目经验。",
      enabled: true,
    },
  ];
  const state = {
    mutations: [],
    queries: [],
    docs: pack.documents.map((item, index) => ({
      id: item.id,
      knowledge_base_id: bases[0].id,
      knowledge_base_name: bases[0].name,
      knowledge_base_enabled: true,
      title: item.title,
      content: item.symptoms,
      revision: 1,
      status: index === 0 && !options.allDraft ? "published" : "draft",
      published_version: index === 0 && !options.allDraft ? 1 : null,
      source_kind: "reference_pack",
      created_by: "admin",
      tags: [],
      environment: "",
      pending_source_record_ids: [],
      latest_jobs: [],
    })),
  };
  state.docs.push({
    ...state.docs[0],
    id: "project-doc",
    knowledge_base_id: bases[1].id,
    knowledge_base_name: bases[1].name,
    title: "应用部署后的健康检查",
    status: "published",
    published_version: 1,
    source_kind: "core_record",
    created_by: "Core 客户端 · workstation",
  });
  const summary = () => {
    const count = (rows) => ({
      documents: rows.length,
      drafts: rows.filter((d) => d.status === "draft").length,
      indexing: rows.filter((d) => d.status === "indexing").length,
      searchable: rows.filter((d) => d.published_version != null).length,
    });
    return {
      totals: { ...count(state.docs), knowledge_bases: bases.length },
      bases: bases.map((b) => ({
        ...b,
        ...count(state.docs.filter((d) => d.knowledge_base_id === b.id)),
      })),
      worker: { healthy: true, pending: 0, running: 0, failed: 3 },
      generated_at: 1790985600,
    };
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request(),
      url = new URL(request.url());
    const path = url.pathname.replace("/api/admin/v1/", "");
    const reply = (data, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    if (request.method() !== "GET")
      state.mutations.push({ path, body: request.postDataJSON() });
    if (path === "session")
      return reply({ username: "admin", csrf: "test-csrf" });
    if (path === "config")
      return reply({
        knowledge_connected: true,
        ai_refinement_ready: true,
        embedding_enabled: false,
      });
    if (path === "knowledge/knowledge-bases") return reply(bases);
    if (path === "knowledge/overview") return reply(summary());
    if (path === "knowledge/documents")
      return reply(
        state.docs.filter(
          (d) =>
            !url.searchParams.get("knowledge_base_id") ||
            d.knowledge_base_id === url.searchParams.get("knowledge_base_id"),
        ),
      );
    if (
      [
        "knowledge/records",
        "knowledge/jobs",
        "knowledge/knowledge-keys",
      ].includes(path)
    )
      return reply([]);
    if (path === "knowledge/documents/batch-publish") {
      const results = request.postDataJSON().documents.map((item, index) => {
        if (options.partial && index === 1)
          return {
            document_id: item.document_id,
            status: "failed",
            error: {
              code: "REVISION_CONFLICT",
              message: "草稿已变化，请重新审核",
            },
          };
        state.docs.find((d) => d.id === item.document_id).status = "indexing";
        return {
          document_id: item.document_id,
          status: "queued",
          job_id: "job-" + item.document_id,
        };
      });
      return reply({
        queued: results.filter((r) => r.status === "queued").length,
        failed: results.filter((r) => r.status === "failed").length,
        results,
      });
    }
    if (path === "knowledge/search") {
      const body = request.postDataJSON();
      state.queries.push(body);
      if (options.searchError)
        return reply(
          {
            error: {
              code: "SEARCH_CAPACITY_EXCEEDED",
              message: "检索请求失败，请缩小范围后重试",
            },
          },
          503,
        );
      if (options.delay)
        await new Promise((resolve) => setTimeout(resolve, 180));
      return reply({
        retrieval_mode: "keyword_only",
        embedding_status: options.degraded ? "unavailable" : "disabled",
        warnings: options.degraded
          ? ["向量服务暂时不可用，已回退到关键词检索"]
          : [],
        corpus: summary().bases.find(
          (b) => b.id === body.knowledge_base_ids[0],
        ),
        truncated: false,
        hits: options.hit
          ? [
              {
                chunk_id: "chunk-example",
                document_id: state.docs[0].id,
                title: state.docs[0].title,
                document_version: 1,
                knowledge_base_id: bases[0].id,
                content: "df -i /var/log",
                citation: { label: "K1", line_start: 1, line_end: 1 },
              },
            ]
          : [],
      });
    }
    const match = path.match(/^knowledge\/documents\/([^/]+)(.*)$/);
    if (match) {
      if (match[2] === "/comparison") return reply({ comparison: null });
      if (match[2]) return reply([]);
      return reply(state.docs.find((d) => d.id === match[1]));
    }
    return reply({ error: { message: "unmocked: " + path } }, 404);
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "知识资产与待办" }),
  ).toBeVisible();
  return state;
}

async function referenceDocuments(page) {
  await page
    .locator(".library-row")
    .filter({
      has: page.getByRole("heading", { name: "运维参考知识", exact: true }),
    })
    .getByRole("button", { name: "查看文档" })
    .click();
  await expect(page.locator(".document-record")).toHaveCount(20);
}

test("overview explains global publication state and remains usable on mobile", async ({
  page,
}, info) => {
  await setup(page);
  await expect(page.getByText("有 19 篇草稿等待审核")).toBeVisible();
  await expect(page.getByText("关键词检索可用", { exact: true })).toBeVisible();
  await expect(page.getByLabel("运维参考知识 的知识库 ID")).toHaveValue(
    "reference-base",
  );
  await page.screenshot({
    path: info.outputPath("overview-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("button", { name: "查看待审核草稿" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: info.outputPath("overview-mobile.png"),
    fullPage: true,
  });
});

test("ownership is visible, base filtering is explicit, batch publication requires review", async ({
  page,
}, info) => {
  const state = await setup(page);
  await referenceDocuments(page);
  await expect(page.locator(".record-ownership").first()).toContainText(
    "运维参考知识",
  );
  await expect(page.locator(".record-ownership").first()).toContainText(
    "参考知识包 · 创建者：admin",
  );
  await expect(
    page.getByLabel("选择 " + pack.documents[0].title, { exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("选择 " + pack.documents[1].title, { exact: true })
    .check();
  await page
    .getByLabel("选择 " + pack.documents[2].title, { exact: true })
    .check();
  await page.screenshot({
    path: info.outputPath("documents-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: info.outputPath("documents-mobile.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "批量发布", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "确认批量发布" });
  await expect(
    dialog.getByRole("button", { name: "确认发布 2 篇" }),
  ).toBeDisabled();
  await dialog.getByLabel("我已逐篇审核内容、来源和适用范围").check();
  await dialog.getByRole("button", { name: "确认发布 2 篇" }).click();
  await expect(
    page.getByRole("status", { name: "批量发布结果" }),
  ).toContainText("2 篇已提交索引，0 篇未提交");
  expect(
    state.mutations.find((m) => m.path.endsWith("batch-publish")).body
      .documents,
  ).toEqual([
    { document_id: pack.documents[1].id, revision: 1 },
    { document_id: pack.documents[2].id, revision: 1 },
  ]);
  await expect(
    page.getByRole("button", { name: "批量发布", exact: true }),
  ).toBeDisabled();
});

test("batch partial failure names the failed document and does not claim publication completed", async ({
  page,
}) => {
  await setup(page, { partial: true });
  await referenceDocuments(page);
  for (const item of pack.documents.slice(1, 3))
    await page.getByLabel("选择 " + item.title, { exact: true }).check();
  await page.getByRole("button", { name: "批量发布", exact: true }).click();
  await page.getByLabel("我已逐篇审核内容、来源和适用范围").check();
  await page.getByRole("button", { name: "确认发布 2 篇" }).click();
  const result = page.getByRole("status", { name: "批量发布结果" });
  await expect(result).toContainText("1 篇已提交索引，1 篇未提交");
  await expect(result).toContainText(pack.documents[2].title);
  await expect(result).toContainText("REVISION_CONFLICT");
});

test("changing filters clears selection instead of silently publishing hidden documents", async ({
  page,
}) => {
  await setup(page);
  await referenceDocuments(page);
  await page
    .getByLabel("选择 " + pack.documents[1].title, { exact: true })
    .check();
  await page.getByLabel("搜索本页文档").fill("Docker");
  await expect(page.getByText("已选择 0 篇", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "批量发布", exact: true }),
  ).toBeDisabled();
});

test("select-all skips published documents and cancel never submits publication", async ({
  page,
}) => {
  const state = await setup(page);
  await referenceDocuments(page);
  await page.getByLabel(/选择本页待审核/).check();
  await expect(page.getByText("已选择 19 篇", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "批量发布", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "确认批量发布" });
  await expect(
    dialog.getByRole("button", { name: "确认发布 19 篇" }),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  expect(
    state.mutations.filter((item) => item.path.endsWith("batch-publish")),
  ).toHaveLength(0);
});

test("new document defaults to the explicitly filtered library", async ({
  page,
}) => {
  await setup(page);
  await page
    .locator(".library-row")
    .filter({ has: page.getByRole("heading", { name: "Opsark", exact: true }) })
    .getByRole("button", { name: "查看文档" })
    .click();
  await page.getByRole("button", { name: "新建文档", exact: true }).click();
  await expect(page.locator("#document-editor select")).toHaveValue(
    "project-base",
  );
});

test("empty draft-only library explains the publish prerequisite, not an Embedding error", async ({
  page,
}, info) => {
  await setup(page, { allDraft: true });
  await page.getByRole("button", { name: "检索调试", exact: true }).click();
  await page.getByLabel("查询", { exact: true }).fill("Opsark");
  await page
    .getByRole("button", { name: "检索已发布知识", exact: true })
    .click();
  await expect(
    page.getByText("未配置向量服务，关键词检索正常可用；这不是检索错误。"),
  ).toBeVisible();
  await expect(
    page.getByText(
      "该库有 20 篇待审核草稿，目前没有可检索知识。请先审核并发布。",
    ),
  ).toBeVisible();
  await expect(page.locator(".search-warning")).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath("search-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: info.outputPath("search-mobile.png"),
    fullPage: true,
  });
});

test("normal no-match, degraded vector service, and real request failure remain distinct", async ({
  page,
}) => {
  await setup(page, { degraded: true });
  await page.getByRole("button", { name: "检索调试", exact: true }).click();
  await page.getByLabel("查询", { exact: true }).fill("Opsark");
  await page
    .getByRole("button", { name: "检索已发布知识", exact: true })
    .click();
  await expect(page.locator(".search-warning")).toContainText(
    "向量服务暂时不可用",
  );
  await expect(
    page.getByText("没有匹配的已发布知识。", { exact: false }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("real search error is shown as an error, not no matches", async ({
  page,
}) => {
  await setup(page, { searchError: true });
  await page.getByRole("button", { name: "检索调试", exact: true }).click();
  await page.getByLabel("查询", { exact: true }).fill("disk");
  await page
    .getByRole("button", { name: "检索已发布知识", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("检索请求失败");
  await expect(page.locator(".search-mode")).toHaveCount(0);
});

test("changing the selected knowledge base clears stale search hits", async ({
  page,
}) => {
  await setup(page, { hit: true });
  await page.getByRole("button", { name: "检索调试", exact: true }).click();
  await page.getByLabel("查询", { exact: true }).fill("inode");
  await page
    .getByRole("button", { name: "检索已发布知识", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: pack.documents[0].title, exact: true }),
  ).toBeVisible();
  await page.getByLabel("知识库", { exact: true }).selectOption("project-base");
  await expect(
    page.getByRole("button", { name: pack.documents[0].title, exact: true }),
  ).toHaveCount(0);
  await expect(page.locator(".search-mode")).toHaveCount(0);
});
