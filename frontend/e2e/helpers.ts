import type { Page } from "@playwright/test";

export const API = "http://localhost:8011";

/** The demo backend is shared by every test: leave no pending cards behind so tests stay independent. */
export async function rejectAllPending(page: Page): Promise<void> {
  const pending = (await (await page.request.get(`${API}/approvals?status=pending`)).json()) as { id: string }[];
  for (const a of pending) await page.request.post(`${API}/approvals/${a.id}/reject`, { data: { reason: "test cleanup" } });
}
