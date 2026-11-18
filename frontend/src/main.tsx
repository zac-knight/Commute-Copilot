import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { HttpApi } from "./api/client";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App api={new HttpApi()} />
  </StrictMode>,
);
