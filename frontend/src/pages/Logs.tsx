import { useEffect, useState } from 'react';
import { Typography, Table, Tag, Input, Select, Space } from 'antd';
import { fetchApi } from '../api/client';

interface LogEntry {
  timestamp: string;
  workflow_id?: string;
  run_id?: string;
  execution_id?: string;
  agent_id?: string;
  action?: string;
  tool_name?: string;
  round?: number;
  result?: unknown;
  error?: string;
}

const actionColors: Record<string, string> = {
  tool_call: 'orange',
  tool_error: 'red',
  llm_error: 'red',
  skill_error: 'volcano',
  routing: 'green',
  llm_summary: 'blue',
  agent_message: 'purple',
};

const actionLabels: Record<string, string> = {
  tool_call: '工具调用',
  tool_error: '工具错误',
  llm_error: '模型错误',
  skill_error: '技能错误',
  routing: '流程路由',
  llm_summary: '模型摘要',
  agent_message: 'Agent 消息',
};

export default function Logs() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<{
    workflow_id?: string;
    run_id?: string;
    execution_id?: string;
    agent_id?: string;
    action?: string;
  }>({});

  const load = () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (filters.workflow_id) params.set('workflow_id', filters.workflow_id);
    if (filters.run_id) params.set('run_id', filters.run_id);
    if (filters.execution_id) params.set('execution_id', filters.execution_id);
    if (filters.agent_id) params.set('agent_id', filters.agent_id);
    if (filters.action) params.set('action', filters.action);
    fetchApi<LogEntry[]>(`/api/logs?${params}`)
      .then(setLogs)
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    // Filtering intentionally triggers a new request and loading state.
    load();
    // load closes over the current filter object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters]);

  return (
    <>
      <Typography.Title level={3}>执行日志</Typography.Title>
      <Space style={{ marginBottom: 16 }}>
        <Input placeholder="工作流 ID" allowClear onChange={e => setFilters(f => ({ ...f, workflow_id: e.target.value || undefined }))} style={{ width: 200 }} />
        <Input placeholder="运行 ID" allowClear onChange={e => setFilters(f => ({ ...f, run_id: e.target.value || undefined }))} style={{ width: 170 }} />
        <Input placeholder="执行 ID" allowClear onChange={e => setFilters(f => ({ ...f, execution_id: e.target.value || undefined }))} style={{ width: 200 }} />
        <Input placeholder="Agent ID" allowClear onChange={e => setFilters(f => ({ ...f, agent_id: e.target.value || undefined }))} style={{ width: 200 }} />
        <Select placeholder="动作类型" allowClear onChange={v => setFilters(f => ({ ...f, action: v }))} style={{ width: 160 }}
          options={['tool_call', 'tool_error', 'llm_error', 'routing', 'llm_summary', 'agent_message'].map(a => ({ label: actionLabels[a], value: a }))}
        />
      </Space>
      <Table
        dataSource={logs}
        rowKey={(_, index) => String(index)}
        loading={loading}
        pagination={{ pageSize: 50 }}
        size="small"
        columns={[
          { title: '时间', dataIndex: 'timestamp', key: 'time', width: 200, render: (t: string) => new Date(t).toLocaleString() },
          { title: '工作流', dataIndex: 'workflow_id', key: 'wf', width: 150, ellipsis: true },
          { title: '运行', dataIndex: 'run_id', key: 'run', width: 140, ellipsis: true },
          { title: '执行', dataIndex: 'execution_id', key: 'execution', width: 160, ellipsis: true },
          { title: 'Agent', dataIndex: 'agent_id', key: 'agent', width: 150 },
          { title: '动作', dataIndex: 'action', key: 'action', width: 120, render: (a: string) => <Tag color={actionColors[a] || 'default'}>{actionLabels[a] || a}</Tag> },
          { title: '工具', dataIndex: 'tool_name', key: 'tool', width: 120 },
          { title: '轮次', dataIndex: 'round', key: 'round', width: 70 },
          { title: '结果', dataIndex: 'result', key: 'result', ellipsis: true },
          { title: '错误', dataIndex: 'error', key: 'error', ellipsis: true, render: (e: string) => e && <Tag color="red">{e}</Tag> },
        ]}
      />
    </>
  );
}
