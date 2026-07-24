import { useEffect, useState } from 'react';
import { Drawer, Input, Select, Space, Switch, Table, Tag, Typography } from 'antd';
import { fetchApi } from '../api/client';

interface LlmSpan {
  id: string;
  run_id?: string;
  workflow_id?: string;
  agent_id?: string;
  trace_kind: string;
  provider: string;
  model: string;
  status: string;
  started_at: string;
  latency_ms?: number;
  input_tokens: number;
  output_tokens: number;
  input_preview?: string;
  output_preview?: string;
  error?: string;
}

const statusLabels: Record<string, string> = {
  completed: '已完成',
  error: '错误',
  running: '运行中',
  pending: '等待中',
};

const traceKindLabels: Record<string, string> = {
  health: '健康检查',
  workflow: '工作流',
  agent: 'Agent',
  llm: '模型调用',
};

export default function Observability() {
  const [spans, setSpans] = useState<LlmSpan[]>([]);
  const [workflowId, setWorkflowId] = useState('');
  const [agentId, setAgentId] = useState('');
  const [includeHealth, setIncludeHealth] = useState(false);
  const [selected, setSelected] = useState<LlmSpan | null>(null);

  useEffect(() => {
    const params = new URLSearchParams();
    if (workflowId) params.set('workflow_id', workflowId);
    if (agentId) params.set('agent_id', agentId);
    params.set('attributed_only', 'true');
    if (!includeHealth) params.set('exclude_trace_kind', 'health');
    fetchApi<LlmSpan[]>(`/api/observability/spans?${params}`).then(setSpans).catch(console.error);
  }, [workflowId, agentId, includeHealth]);

  return <>
    <Typography.Title level={3}>模型追踪</Typography.Title>
    <Space style={{ marginBottom: 16 }}>
      <Input allowClear placeholder="工作流 ID" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)} style={{ width: 220 }} />
      <Input allowClear placeholder="Agent ID" value={agentId} onChange={(event) => setAgentId(event.target.value)} style={{ width: 220 }} />
      <Select value="latest" options={[{ value: 'latest', label: '最近 500 条' }]} style={{ width: 160 }} />
      <Space><Switch checked={includeHealth} onChange={setIncludeHealth} /> 包含健康检查</Space>
    </Space>
    <Table<LlmSpan>
      dataSource={spans}
      rowKey="id"
      size="small"
      onRow={(record) => ({ onClick: () => setSelected(record) })}
      columns={[
        { title: '时间', dataIndex: 'started_at', width: 170, render: (value) => new Date(value).toLocaleString() },
        { title: 'Agent', dataIndex: 'agent_id', width: 160, ellipsis: true },
        { title: '类型', dataIndex: 'trace_kind', width: 100, render: (value) => <Tag>{traceKindLabels[value] || value}</Tag> },
        { title: '模型', dataIndex: 'model', width: 210, ellipsis: true },
        { title: '状态', dataIndex: 'status', width: 100, render: (value) => <Tag color={value === 'completed' ? 'green' : value === 'error' ? 'red' : 'blue'}>{statusLabels[value] || value}</Tag> },
        { title: '耗时', dataIndex: 'latency_ms', width: 100, render: (value) => value === null || value === undefined ? '-' : `${value} 毫秒` },
        { title: '令牌数（输入 / 输出）', key: 'tokens', width: 160, render: (_, span) => `${span.input_tokens} / ${span.output_tokens}` },
        { title: '运行', dataIndex: 'run_id', ellipsis: true },
      ]}
    />
    <Drawer title="模型调用详情" open={Boolean(selected)} onClose={() => setSelected(null)} width={560}>
      {selected && <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <div><strong>{selected.provider} / {selected.model}</strong></div>
        <div><strong>输入</strong><pre style={{ whiteSpace: 'pre-wrap' }}>{selected.input_preview || '当前保留策略未保存此内容'}</pre></div>
        <div><strong>输出</strong><pre style={{ whiteSpace: 'pre-wrap' }}>{selected.output_preview || '当前保留策略未保存此内容'}</pre></div>
        {selected.error && <div><strong>错误</strong><pre style={{ whiteSpace: 'pre-wrap', color: '#cf1322' }}>{selected.error}</pre></div>}
      </Space>}
    </Drawer>
  </>;
}
