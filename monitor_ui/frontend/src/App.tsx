import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Issues from "./pages/Issues";
import Investigations from "./pages/Investigations";
import TestUsers from "./pages/TestUsers";
import SettingsPage from "./pages/Settings";
import Placeholder from "./pages/Placeholder";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="issues" element={<Issues />} />
        <Route path="investigations" element={<Investigations />} />
        <Route path="test-users" element={<TestUsers />} />
        <Route path="settings" element={<SettingsPage />} />

        <Route
          path="behavior-twin"
          element={
            <Placeholder
              title="Behavior Twin"
              tagline="Live behavioural model of the agent fleet — services, queues, calls, drift."
              bullets={[
                "Render every service + its expected behaviour fingerprint as a node-link graph driven by the same captured events Investigations runs on.",
                "Highlight anomalous edges in red — a node drifts when its observed behaviour diverges from its declared expected behaviour.",
                "Lets the operator see the system the way AEGIS sees it, before the deviation becomes a customer-facing incident.",
              ]}
            />
          }
        />
        <Route
          path="root-cause"
          element={
            <Placeholder
              title="Root Cause"
              tagline="LLM-assisted causal chain reconstruction from deviation back to source."
              bullets={[
                "Take a deviation, walk the correlation graph, and propose the single change (commit, config edit, upstream contract drift) that produced it.",
                "Render the 5-Whys timeline + cite the captured events that prove each link.",
                "Outputs a structured RCA dossier you can paste into a ticket or hand to the Self-Heal layer.",
              ]}
            />
          }
        />
        <Route
          path="self-heal"
          element={
            <Placeholder
              title="Self Heal"
              tagline="Generate the fix, gate it, deploy, verify — close the loop."
              bullets={[
                "Read the playbook entry from the issue analysis, generate a candidate patch via a Claude tool-use loop.",
                "Run the test suite + static checks before any merge attempt.",
                "Deploy to staging behind a feature flag, watch the same rule, only roll forward when the deviation stays quiet.",
              ]}
            />
          }
        />
        <Route
          path="deployments"
          element={
            <Placeholder
              title="Deployments"
              tagline="Every autonomous (and manual) deployment, with verification status."
              bullets={[
                "Lists every release the self-heal layer has shipped, the deviation it targeted, and the verification verdict.",
                "Links to the original issue + the post-deploy monitor window so you can audit any decision after the fact.",
                "One-click rollback when verification fails.",
              ]}
            />
          }
        />
        <Route
          path="verification"
          element={
            <Placeholder
              title="Verification"
              tagline="Post-deploy behavioural regression checks."
              bullets={[
                "After every deploy, run the deviation rule that triggered the heal for N minutes.",
                "If the rule stays silent + the related KPIs hold, mark verified.",
                "If anything regresses, auto-rollback + raise an incident.",
              ]}
            />
          }
        />
        <Route
          path="knowledge-graph"
          element={
            <Placeholder
              title="Knowledge Graph"
              tagline="Every fix, every incident, indexed by signature for instant recall."
              bullets={[
                "Embeds every captured deviation + its resolved fix into a vector store.",
                "When a new deviation appears, retrieve the closest prior incident and the patch that fixed it.",
                "Turns the AEGIS install into an asset that gets smarter the longer it runs.",
              ]}
            />
          }
        />
        <Route
          path="agents"
          element={
            <Placeholder
              title="Agents"
              tagline="Inventory + health of every AI agent under AEGIS governance."
              bullets={[
                "Lists every workflow / sub-agent / tool agent the platform watches.",
                "Per-agent SLOs, recent incident history, last verification verdict.",
                "Drill in to see the agent's behaviour fingerprint and the rules tuned for it.",
              ]}
            />
          }
        />
      </Route>
    </Routes>
  );
}
