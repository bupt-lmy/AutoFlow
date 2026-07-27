import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Empty, Input, Select, Space, Table, Tag, Typography } from 'antd';
import { ArrowRightOutlined, PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { fetchApi } from '../api/client';

interface WorkflowSummary {
  id: string;
  name: string;
  description?: string;
  agent_count: number;
  mode?: 'flat' | 'supervisor';
  trigger?: {
    type: string;
    cron?: string | null;
    timezone?: string;
  };
  hosting?: { enabled: boolean };
}

export default function Workflows() {
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [triggerType, setTriggerType] = useState<'all' | 'manual' | 'cron'>('all');
  const navigate = useNavigate();

  const loadWorkflows = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setWorkflows(await fetchApi<WorkflowSummary[]>('/api/workflows'));
    } catch (loadError: unknown) {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadWorkflows();
  }, [loadWorkflows]);

  const filteredWorkflows = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return workflows.filter((workflow) => {
      const matchesQuery = !normalizedQuery || [workflow.name, workflow.id, workflow.description]
        .some((value) => value?.toLocaleLowerCase().includes(normalizedQuery));
      const workflowTrigger = workflow.trigger?.type || 'manual';
      return matchesQuery && (triggerType === 'all' || workflowTrigger === triggerType);
    });
  }, [query, triggerType, workflows]);

  const hasFilters = Boolean(query.trim()) || triggerType !== 'all';
  const clearFilters = () => {
    setQuery('');
    setTriggerType('all');
  };

  return (
    <>
      <Space align="start" style={{ width: '100%', justifyContent: 'space-between', marginBottom: 20 }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>工作流</Typography.Title>
          <Typography.Text type="secondary">
            创建、定时运行并监控多 Agent 工作流。
          </Typography.Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/workflows/new')}>
          新建工作流
        </Button>
      </Space>

      {error && (
        <Alert
          showIcon
          type="error"
          message="工作流加载失败"
          description={error}
          action={(
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void loadWorkflows()}>
              重试
            </Button>
          )}
          style={{ marginBottom: 16 }}
        />
      )}

      <Space wrap style={{ width: '100%', marginBottom: 16 }}>
        <Input
          allowClear
          aria-label="搜索工作流"
          prefix={<SearchOutlined />}
          placeholder="按名称、ID 或说明搜索"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          style={{ width: 320, maxWidth: '100%' }}
        />
        <Select
          aria-label="按触发方式筛选"
          value={triggerType}
          onChange={setTriggerType}
          options={[
            { value: 'all', label: '全部触发方式' },
            { value: 'manual', label: '手动运行' },
            { value: 'cron', label: '定时运行' },
          ]}
          style={{ width: 150 }}
        />
        <Typography.Text type="secondary">
          显示 {filteredWorkflows.length} / {workflows.length}
        </Typography.Text>
      </Space>

      <Table
        loading={loading}
        dataSource={filteredWorkflows}
        rowKey="id"
        scroll={{ x: 760 }}
        pagination={workflows.length > 10 ? { pageSize: 10, showSizeChanger: false } : false}
        locale={{
          emptyText: !loading && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={hasFilters ? '没有符合筛选条件的工作流。' : '还没有工作流。'}
            >
              {hasFilters ? (
                <Button onClick={clearFilters}>清除筛选</Button>
              ) : (
                <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/workflows/new')}>
                  创建第一个工作流
                </Button>
              )}
            </Empty>
          ),
        }}
        columns={[
          {
            title: '工作流',
            key: 'workflow',
            width: 330,
            render: (_: unknown, workflow: WorkflowSummary) => (
              <div>
                <Button
                  type="link"
                  onClick={() => navigate(`/workflows/${workflow.id}`)}
                  style={{ height: 'auto', padding: 0, fontWeight: 600 }}
                >
                  {workflow.name}
                </Button>
                <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                  {workflow.id}
                </Typography.Text>
                {workflow.description && (
                  <Typography.Paragraph
                    ellipsis={{ rows: 1, tooltip: workflow.description }}
                    type="secondary"
                    style={{ margin: '3px 0 0', maxWidth: 300 }}
                  >
                    {workflow.description}
                  </Typography.Paragraph>
                )}
              </div>
            ),
          },
          {
            title: 'Agent 数量',
            dataIndex: 'agent_count',
            key: 'agents',
            width: 90,
            align: 'center',
            sorter: (left, right) => left.agent_count - right.agent_count,
            render: (count: number) => count || 0,
          },
          {
            title: '模式',
            dataIndex: 'mode',
            key: 'mode',
            width: 120,
            render: (mode: WorkflowSummary['mode'], workflow: WorkflowSummary) => (
              <Space size={4} wrap>
                <Tag color={mode === 'supervisor' ? 'purple' : 'default'}>
                  {mode === 'supervisor' ? 'Supervisor' : '标准'}
                </Tag>
                {workflow.hosting?.enabled && <Tag color="cyan">托管</Tag>}
              </Space>
            ),
          },
          {
            title: '触发方式',
            key: 'trigger',
            width: 230,
            render: (_: unknown, r: WorkflowSummary) => (
              <Tag color={r.trigger?.type === 'cron' ? 'blue' : 'default'}>
                {r.trigger?.type === 'cron'
                  ? `${r.trigger.cron || '未设置计划'} · ${r.trigger.timezone || 'UTC'}`
                  : '手动'}
              </Tag>
            ),
          },
          {
            title: '操作',
            key: 'actions',
            width: 100,
            fixed: 'right',
            render: (_: unknown, r: WorkflowSummary) => (
              <Button
                size="small"
                icon={<ArrowRightOutlined />}
                onClick={() => navigate(`/workflows/${r.id}`)}
              >
                打开
              </Button>
            ),
          },
        ]}
      />
    </>
  );
}
