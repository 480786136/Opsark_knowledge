import { expect, test } from "@playwright/test";

test("isolated real API and worker: review source, publish, cite, and unpublish", async ({
  page,
}, testInfo) => {
  test.skip(
    !process.env.KNOWLEDGE_LIVE_URL,
    "Set the explicit loopback live-acceptance environment to run.",
  );
  const documentId = process.env.KNOWLEDGE_LIVE_DOCUMENT_ID;
  const documentPath =
    "/api/admin/v1/knowledge/documents/" + encodeURIComponent(documentId);
  const documentResponse = async () => {
    const response = await page.request.get(documentPath);
    expect(response.ok()).toBeTruthy();
    return response.json();
  };
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page
    .getByLabel("管理员账号", { exact: true })
    .fill(process.env.KNOWLEDGE_LIVE_USERNAME);
  await page
    .getByLabel("密码", { exact: true })
    .fill(process.env.KNOWLEDGE_LIVE_PASSWORD);
  await page.getByRole("button", { name: "登录知识管理", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "知识文档", exact: true }),
  ).toBeVisible();
  const original = await documentResponse();
  expect(original.status).toBe("draft");
  expect(original.source_record_id).toBeTruthy();
  expect(original.published_version).toBeNull();

  await page.getByRole("button", { name: "记录收件箱", exact: true }).click();
  await page.getByRole("button", { name: "知识文档", exact: true }).click();
  await page.getByRole("button", { name: original.title, exact: true }).click();
  const drawer = page.getByRole("dialog", {
    name: original.title,
    exact: true,
  });
  const evidence = drawer.getByRole("region", {
    name: "来源证据",
    exact: true,
  });
  await expect(evidence).toContainText(original.source_record_id);
  await expect(evidence.locator(".source-evidence").first()).toBeVisible();

  const marker = "browseracceptance" + Date.now();
  await drawer.getByRole("button", { name: "编辑草稿", exact: true }).click();
  await drawer
    .getByLabel("正文（纯文本 / Markdown）", { exact: true })
    .fill(original.content + "\n\n" + marker);
  await drawer.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    drawer.getByRole("button", { name: "审核并发布", exact: true }),
  ).toBeEnabled();
  const edited = await documentResponse();
  expect(edited.context).toEqual(original.context);
  expect(edited.tags).toEqual(original.tags);
  expect(edited.software_names).toEqual(original.software_names);

  page.once("dialog", (dialog) => dialog.accept());
  await drawer.getByRole("button", { name: "审核并发布", exact: true }).click();
  await expect
    .poll(async () => (await documentResponse()).published_version, {
      timeout: 60000,
      intervals: [500, 1000],
    })
    .not.toBeNull();
  const published = await documentResponse();
  await drawer.getByRole("button", { name: "刷新详情", exact: true }).click();
  await drawer
    .getByRole("button", {
      name: "查看 v" + published.published_version,
      exact: true,
    })
    .click();
  await expect(drawer.getByLabel("版本正文及行号")).toContainText(marker);
  await expect(
    drawer.getByText("当前对外发布版本", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("live-published-review.png"),
  });
  await drawer.getByRole("button", { name: "关闭", exact: true }).click();

  await page.getByRole("button", { name: "检索调试", exact: true }).click();
  await page.getByLabel("查询", { exact: true }).fill(marker);
  await page
    .getByRole("button", { name: "检索已发布知识", exact: true })
    .click();
  await page
    .getByRole("button", { name: original.title, exact: true })
    .first()
    .click();
  await page.getByRole("button", { name: "打开引用版本", exact: true }).click();
  const citation = page.getByRole("dialog", {
    name: "引用版本详情",
    exact: true,
  });
  await expect(citation.getByLabel("版本正文及行号")).toContainText(marker);
  await citation
    .getByRole("button", { name: "查看引用来源快照", exact: true })
    .click();
  await expect(citation.locator(".source-evidence")).toContainText(
    original.source_record_id,
  );
  await page.screenshot({
    path: testInfo.outputPath("live-search-citation.png"),
  });
  await citation.getByRole("button", { name: "关闭", exact: true }).click();
  await page
    .getByRole("dialog", { name: original.title, exact: true })
    .getByRole("button", { name: "关闭", exact: true })
    .click();

  await page.getByRole("button", { name: "知识文档", exact: true }).click();
  await page.getByRole("button", { name: original.title, exact: true }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await drawer
    .getByRole("button", { name: "下架 / 取消发布", exact: true })
    .click();
  await expect
    .poll(async () => (await documentResponse()).published_version)
    .toBeNull();
  await expect(
    drawer.getByText("当前发布版本 未发布", { exact: false }),
  ).toBeVisible();
  await drawer.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "检索调试", exact: true }).click();
  await page
    .getByRole("button", { name: "检索已发布知识", exact: true })
    .click();
  await expect(
    page.getByText("目前没有可检索知识。请先审核并发布。", { exact: false }),
  ).toBeVisible();
  // Create two manual drafts through the actual authenticated API, then publish
  // them using the new UI workflow. These writes are confined to the smoke DB.
  const session = await (
    await page.request.get("/api/admin/v1/session")
  ).json();
  const created = [];
  for (const name of ["批量验收：磁盘容量", "批量验收：服务状态"]) {
    const response = await page.request.post(
      "/api/admin/v1/knowledge/documents",
      {
        headers: { "X-CSRF-Token": session.csrf },
        data: {
          knowledge_base_id: original.knowledge_base_id,
          title: name,
          content: name + "\n仅供隔离测试，未执行任何命令。",
        },
      },
    );
    expect(response.status()).toBe(201);
    created.push(await response.json());
  }
  await page.getByRole("button", { name: "刷新数据", exact: true }).click();
  await page.getByRole("button", { name: "知识文档", exact: true }).click();
  for (const doc of created)
    await page.getByLabel("选择 " + doc.title, { exact: true }).check();
  await expect(
    page
      .locator(".record-ownership")
      .filter({ hasText: process.env.KNOWLEDGE_LIVE_USERNAME })
      .first(),
  ).toBeVisible();
  await page.getByRole("button", { name: "批量发布", exact: true }).click();
  await page.getByLabel("我已逐篇审核内容、来源和适用范围").check();
  await page.getByRole("button", { name: "确认发布 2 篇" }).click();
  await expect(
    page.getByRole("status", { name: "批量发布结果" }),
  ).toContainText("2 篇已提交索引，0 篇未提交");
  for (const doc of created) {
    await expect
      .poll(
        async () =>
          (
            await (
              await page.request.get(
                "/api/admin/v1/knowledge/documents/" + doc.id,
              )
            ).json()
          ).status,
      )
      .toBe("published");
  }
  await page.screenshot({
    path: testInfo.outputPath("live-batch-publication.png"),
  });
  expect(errors).toEqual([]);
});
