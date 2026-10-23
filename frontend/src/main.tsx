import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { loadSession, post, saveSession, type Session } from "./api";
import { Shell } from "./components/Shell";
import Backtests from "./pages/Backtests";
import Connection from "./pages/Connection";
import Dashboard from "./pages/Dashboard";
import History from "./pages/History";
import Login from "./pages/Login";
import Logs from "./pages/Logs";
import Market from "./pages/Market";
import Positions from "./pages/Positions";
import Risk from "./pages/Risk";
import Signals from "./pages/Signals";
import Strategy from "./pages/Strategy";
import "./styles.css";

function App() {
  const [session, setSession] = useState<Session | null>(loadSession());

  useEffect(() => {
    const expire = () => setSession(null);
    window.addEventListener("session-expired", expire);
    return () => window.removeEventListener("session-expired", expire);
  }, []);

  if (!session) {
    return <Login onLogin={(s) => { saveSession(s); setSession(s); }} />;
  }

  async function logout() {
    try {
      await post("/auth/logout/", {});
    } finally {
      saveSession(null);
      setSession(null);
    }
  }

  return (
    <BrowserRouter>
      <Shell session={session} onLogout={logout}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/market" element={<Market />} />
          <Route path="/signals" element={<Signals />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/history" element={<History />} />
          <Route path="/risk" element={<Risk />} />
          <Route path="/strategy" element={<Strategy />} />
          <Route path="/backtests" element={<Backtests />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/connection" element={<Connection />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
