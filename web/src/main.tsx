import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import Queue from "./Queue";
import Rules from "./Rules";
import Activity from "./Activity";
import "./styles.css";

type Tab = "queue" | "rules" | "activity";
const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "queue", label: "Queue", icon: "▤" },
  { id: "rules", label: "Rules", icon: "⋔" },
  { id: "activity", label: "Activity", icon: "◷" },
];

function tabFromHash(): Tab {
  const h = window.location.hash.slice(1);
  return TABS.some((t) => t.id === h) ? (h as Tab) : "queue";
}

function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  // a tab stays mounted once opened, so unsaved rule edits survive a look at the queue
  const [opened, setOpened] = useState<Set<Tab>>(() => new Set([tabFromHash()]));

  useEffect(() => {
    setOpened((o) => (o.has(tab) ? o : new Set(o).add(tab)));
  }, [tab]);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = (t: Tab) => {
    if (t !== tab) window.location.hash = t;
  };

  return (
    <div className="app">
      <main className="views">
        {opened.has("queue") && (
          <section className="view" hidden={tab !== "queue"} aria-label="Queue">
            <Queue active={tab === "queue"} />
          </section>
        )}
        {opened.has("rules") && (
          <section className="view view-rules" hidden={tab !== "rules"} aria-label="Rules">
            <Rules active={tab === "rules"} />
          </section>
        )}
        {opened.has("activity") && (
          <section className="view" hidden={tab !== "activity"} aria-label="Activity">
            <Activity active={tab === "activity"} />
          </section>
        )}
      </main>
      <nav className="tabbar" aria-label="Sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className="tab"
            aria-current={tab === t.id ? "page" : undefined}
            onClick={() => go(t.id)}
          >
            <span className="tab-icon" aria-hidden="true">{t.icon}</span>
            <span>{t.label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
