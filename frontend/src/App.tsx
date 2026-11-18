import { HashRouter, Route, Routes } from "react-router-dom";
import type { Api } from "./api/client";
import { InsightsLayout } from "./components/InsightsLayout";
import { Layout } from "./components/Layout";
import { AgentsPage } from "./pages/AgentsPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { ChatPage } from "./pages/ChatPage";
import { PlannerPage } from "./pages/PlannerPage";
import { EvaluationPage } from "./pages/EvaluationPage";
import { SettingsPage } from "./pages/SettingsPage";
import { TracesPage } from "./pages/TracesPage";
import { AppProvider } from "./state/AppContext";

export function App({ api }: { api: Api }) {
  return (
    <AppProvider api={api}>
      <HashRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<PlannerPage />} />
            <Route path="assistant" element={<ChatPage />} />
            <Route element={<InsightsLayout />}>
              <Route path="agents" element={<AgentsPage />} />
              <Route path="analytics" element={<AnalyticsPage />} />
              <Route path="evaluation" element={<EvaluationPage />} />
              <Route path="traces" element={<TracesPage />} />
              <Route path="traces/:id" element={<TracesPage />} />
            </Route>
            <Route path="settings" element={<SettingsPage />} />
            <Route path="*" element={<p className="p-6">Page not found.</p>} />
          </Route>
        </Routes>
      </HashRouter>
    </AppProvider>
  );
}
