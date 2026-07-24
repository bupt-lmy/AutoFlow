import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import type { Locale } from 'antd/es/locale';
import MainLayout from './layouts/MainLayout';
import Dashboard from './pages/Dashboard';
import Workflows from './pages/Workflows';
import WorkflowDetail from './pages/WorkflowDetail';
import WorkflowCreate from './pages/WorkflowCreate';
import WorkflowRun from './pages/WorkflowRun';
import Agents from './pages/Agents';
import AgentDetail from './pages/AgentDetail';
import Logs from './pages/Logs';
import Settings from './pages/Settings';
import Observability from './pages/Observability';
import Skills from './pages/Skills';

const zhCNLocale = (zhCN as Locale & { default?: Locale }).default || zhCN;

export default function App() {
  return (
    <ConfigProvider locale={zhCNLocale} theme={{ algorithm: theme.defaultAlgorithm }}>
      <BrowserRouter>
        <Routes>
          <Route element={<MainLayout />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/workflows" element={<Workflows />} />
            <Route path="/workflows/new" element={<WorkflowCreate />} />
            <Route path="/workflows/:id" element={<WorkflowDetail />} />
            <Route path="/workflows/:id/runs/:runId" element={<WorkflowRun />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/agents/:id" element={<AgentDetail />} />
            <Route path="/skills" element={<Skills />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/observability" element={<Observability />} />
            <Route path="/settings" element={<Settings />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ConfigProvider>
  );
}
