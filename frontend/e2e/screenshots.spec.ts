import { test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { rejectAllPending } from "./helpers";

// README screenshots, from DEMO mode only (frozen data, fake calendar; never real data).
//   SCREENSHOTS=1 npx playwright test screenshots
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), "../../docs/screenshots");
test.skip(!process.env.SCREENSHOTS, "set SCREENSHOTS=1 to regenerate the README screenshots");
test.use({ viewport: { width: 1600, height: 950 }, deviceScaleFactor: 1 });

const ask = async (page: import("@playwright/test").Page, nth: number) =>
  page.getByRole("group", { name: "Example questions" }).getByRole("button").nth(nth).click();

test("screenshots", async ({ page }) => {
  mkdirSync(OUT, { recursive: true });
  await rejectAllPending(page);
  await page.goto("/#/assistant");
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${OUT}/01-workspace.png` });

  await ask(page, 1); // free slot + trip to Chermside
  await page.getByTestId("trip-card").waitFor();
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/02-trip-map.png` });
  await page.getByTestId("trip-map").scrollIntoViewIfNeeded();
  await page.getByTestId("trip-map").screenshot({ path: `${OUT}/03-map-detail.png` });
  await page.getByTestId("tab-flow").click();
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${OUT}/04-agent-flow.png` });

  await page.goto("/#/assistant");
  await ask(page, 3); // Dentist: proposal + approval card
  await page.getByTestId("approval-card").waitFor();
  await page.getByTestId("answer").waitFor();
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/05-approval.png` });
  await rejectAllPending(page);

  for (const [route, file, wait] of [["agents", "06-agents", 800], ["evaluation", "07-evaluation", 900], ["analytics", "08-analytics", 700]] as const) {
    await page.goto(`/#/${route}`);
    await page.waitForTimeout(wait);
    await page.screenshot({ path: `${OUT}/${file}.png`, fullPage: route !== "analytics" });
  }

  await page.goto("/#/assistant");
  await ask(page, 2);
  await page.getByTestId("answer").waitFor();
  await page.getByRole("link", { name: "View trace" }).last().click();
  await page.getByTestId("span-tree").waitFor();
  await page.screenshot({ path: `${OUT}/09-trace.png` });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/assistant");
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/10-mobile.png` });
});
