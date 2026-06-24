import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Issues from "./pages/Issues";
import Investigations from "./pages/Investigations";
import TestUsers from "./pages/TestUsers";
import SettingsPage from "./pages/Settings";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="issues" element={<Issues />} />
        <Route path="investigations" element={<Investigations />} />
        <Route path="test-users" element={<TestUsers />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}
