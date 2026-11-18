import { expect, test } from "@playwright/test";
import { rejectAllPending } from "./helpers";

test.use({ viewport: { width: 390, height: 844 } });
test.afterEach(async ({ page }) => { await rejectAllPending(page); });

test("phone width: no horizontal scroll, approvals panel above the chat, everything reachable", async ({ page }) => {
  await page.goto("/#/assistant");
  await page.getByLabel("Ask about buses, trains, weather or your calendar").fill("Add 'Dentist' Thursday 2pm at Chermside and warn me if I can't get there on time.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("approval-card")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  const panel = await page.getByTestId("approvals-panel").boundingBox();
  const composer = await page.getByLabel("Ask about buses, trains, weather or your calendar").boundingBox();
  expect(panel!.y).toBeLessThan(composer!.y); // approvals are above the composer on a phone
  await expect(page.getByRole("button", { name: "Approve" })).toBeVisible();
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Insights" }).click();
  await page.getByRole("navigation", { name: "Insights" }).getByRole("link", { name: "Traces" }).click();
  await expect(page.getByRole("heading", { name: "Requests" })).toBeVisible();
});
