import { expect, test } from "@playwright/test";

test("pick one agent: the router is skipped, only that agent runs, and the graph says so", async ({ page }) => {
  await page.goto("/#/assistant");
  await page.getByTestId("mode-calendar").click();
  await expect(page.getByTestId("mode-calendar")).toHaveAttribute("aria-checked", "true");
  // demo mode offers only recordings that used the calendar agent alone
  const example = page.getByRole("group", { name: "Example questions" }).getByRole("button").first();
  await expect(example).toBeVisible();
  await example.click();
  await expect(page.getByTestId("plan")).toContainText("Plan: Calendar");
  await expect(page.getByTestId("plan")).not.toContainText("Transit");
  await page.getByTestId("tab-flow").click();
  const graph = page.getByTestId("agent-graph");
  await expect(graph).toBeVisible();
  await expect(graph.locator('[data-node="router"]')).toHaveAttribute("data-status", "skipped");
  await expect(graph.locator('[data-node="synth"]')).toHaveAttribute("data-status", "ok");
  await expect(page.getByTestId("answer")).toBeVisible();
});

test("auto mode shows the router planning and every agent node finishing", async ({ page }) => {
  await page.goto("/#/assistant");
  await page.getByRole("group", { name: "Example questions" }).getByRole("button").first().click();
  await page.getByTestId("tab-flow").click();
  const graph = page.getByTestId("agent-graph");
  await expect(graph.locator('[data-node="router"]')).toHaveAttribute("data-status", "ok");
  await expect(graph.locator('[data-node="s1"]')).toHaveAttribute("data-status", "ok");
  await expect(graph.locator('[data-node="synth"]')).toHaveAttribute("data-status", "ok");
});

test("an agent with no recording in demo says so instead of pretending", async ({ page }) => {
  await page.goto("/#/assistant");
  await page.getByTestId("mode-transit").click();
  await page.getByLabel("Ask about buses, trains, weather or your calendar").fill("quantum chromodynamics");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("answer")).toContainText("demo mode");
});

test("agents page: three cards with read-only and needs-approval permissions, and the architecture", async ({ page }) => {
  await page.goto("/#/agents");
  for (const a of ["transit", "weather", "calendar"]) await expect(page.getByTestId(`agent-card-${a}`)).toBeVisible();
  const cal = page.getByTestId("agent-card-calendar");
  await expect(cal).toContainText("propose_create_event");
  await expect(cal).toContainText("proposes · you approve");
  await expect(cal).toContainText("read-only");
  await expect(page.getByRole("img", { name: /Architecture/ })).toBeVisible();
});

test("analytics page is honest when nothing has been collected", async ({ page }) => {
  await page.goto("/#/analytics");
  await expect(page.getByRole("status").filter({ hasText: "No data has been collected yet" })).toBeVisible();
  await expect(page.getByText("make collect").first()).toBeVisible();
  await expect(page.getByText("Not enough data to train and fairly test a model yet")).toBeVisible();
  await expect(page.getByText("0 of 0")).toHaveCount(0); // no fabricated rates
});

test("evaluation page: run the offline suites and see them pass", async ({ page }) => {
  await page.goto("/#/evaluation");
  await page.getByTestId("run-det").click();
  for (const s of ["transit", "advice", "calendar", "safety"]) await expect(page.getByTestId(`suite-${s}`)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("suite-safety")).toContainText("13/13");
  await expect(page.getByText("Read before quoting any number")).toBeVisible();
});

test("navigation reaches every page by keyboard-accessible links", async ({ page }) => {
  await page.goto("/#/assistant");
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Insights" }).click();
  for (const name of ["Agents", "Analytics", "Evaluation", "Traces"]) {
    await page.getByRole("navigation", { name: "Insights" }).getByRole("link", { name }).click();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  }
  for (const name of ["Settings", "Assistant", "Plan"]) {
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name }).click();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  }
});

test("an old backend's /eval/latest payload shows a page, not a blank screen", async ({ page }) => {
  await page.route("**/api/eval/latest", (r) => r.fulfill({ json: { available: false, note: "no evaluation has been run yet (Phase 7)" } }));
  await page.goto("/#/evaluation");
  await expect(page.getByRole("heading", { level: 1, name: "Evaluation" })).toBeVisible();
  await expect(page.getByText("No results yet")).toBeVisible();
});

test("a render crash is caught and explained", async ({ page }) => {
  await page.route("**/api/agents", (r) => r.fulfill({ json: [{ name: "transit" }] })); // malformed: no tools array
  await page.goto("/#/agents");
  await expect(page.getByRole("alert").filter({ hasText: "could not render" })).toBeVisible();
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Insights" }).click();
  await page.getByRole("navigation", { name: "Insights" }).getByRole("link", { name: "Evaluation" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Evaluation" })).toBeVisible(); // navigation recovers
});

test("a trip question shows the map, start and end, distances and every leg in the Result pane", async ({ page }) => {
  await page.goto("/#/assistant");
  await page.getByRole("group", { name: "Example questions" }).getByRole("button").nth(1).click(); // slot + trip to Chermside
  const card = page.getByTestId("trip-card");
  await expect(card).toBeVisible();
  await expect(card).toContainText("North Lakes station");
  await expect(card).toContainText("Chermside Shopping Centre");
  await expect(card).toContainText("distance");
  await expect(card).toContainText("straight line");
  await expect(card).toContainText("km");
  await expect(page.getByTestId("trip-map")).toBeVisible();
  await expect(page.getByTestId("trip-legs").locator("li")).toHaveCount(6); // bus, walk, train, walk, bus, walk
  await expect(page.getByTestId("trip-legs")).toContainText("Train");
  await expect(card).toContainText("stage");
});

test("the Inspector tab exists and explains itself before anything has run", async ({ page }) => {
  await page.goto("/#/assistant");
  await page.getByTestId("tab-inspector").click();
  await expect(page.getByText("each model call appears here")).toBeVisible();
  await expect(page.getByLabel("Reasoning")).toBeVisible();
});

test("the Result pane is never empty after an answer", async ({ page }) => {
  await page.goto("/#/assistant");
  await page.getByRole("group", { name: "Example questions" }).getByRole("button").first().click(); // departures: no journey
  await expect(page.getByTestId("answer")).toBeVisible();
  await expect(page.getByTestId("trip-card").or(page.getByTestId("result-answer"))).toBeVisible();
});
