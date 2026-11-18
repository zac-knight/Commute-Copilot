import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

function initial(): Theme {
  try {
    const saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch { /* storage unavailable (private mode): fall through */ }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light"; // follow the system, light by default like a travel app
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(initial);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    try { localStorage.setItem("theme", theme); } catch { /* ignore */ }
  }, [theme]);
  const toggle = useCallback(() => setTheme((t) => (t === "dark" ? "light" : "dark")), []);
  return [theme, toggle];
}
