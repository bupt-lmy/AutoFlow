import { useEffect, useState } from 'react';
import { Row, Col, Typography, Table, Tag, Spin, Button, message } from 'antd';
import { ReloadOutlined, RobotOutlined, ThunderboltOutlined } from '@ant-design/icons';
import StatusCard from '../components/StatusCard';
import { fetchApi } from '../api/client';

interface SystemStatus {
  running: boolean;
  agents: Record<string, string>;
  agent_health?: Record<string, AgentHealth>;
  tools: string[];
  token_usage: Record<string, unknown>;
}

interface AgentHealth {
  state: 'unknown' | 'checking' | 'healthy' | 'unhealthy';
  ready: boolean;
  last_checked_at?: string | null;
  latency_ms?: number | null;
  error?: string | null;
}

const agentStatePresentation: Record<string, { label: string; color: string }> = {
  idle: { label: '空闲', color: 'default' },
  running: { label: '监听中', color: 'blue' },
  working: { label: '工作中', color: 'blue' },
  error: { label: '异常', color: 'red' },
  stopped: { label: '已停止', color: 'default' },
};

const healthPresentation: Record<string, { label: string; color: string }> = {
  unknown: { label: '未知', color: 'default' },
  checking: { label: '检查中', color: 'processing' },
  healthy: { label: '可用', color: 'green' },
  unhealthy: { label: '不可用', color: 'red' },
};

export default function Dashboard() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    let mounted = true;
    const loadStatus = async () => {
      try {
        const nextStatus = await fetchApi<SystemStatus>('/api/system/status');
        if (mounted) setStatus(nextStatus);
      } catch (error) {
        console.error(error);
      } finally {
        if (mounted) setLoading(false);
      }
    };

    void loadStatus();
    const intervalId = window.setInterval(() => { void loadStatus(); }, 2000);
    return () => {
      mounted = false;
      window.clearInterval(intervalId);
    };
  }, []);

  const refreshAgentHealth = async () => {
    setRefreshing(true);
    try {
      await fetchApi<Record<string, AgentHealth>>('/api/agents/health-check', { method: 'POST' });
      const nextStatus = await fetchApi<SystemStatus>('/api/system/status');
      setStatus(nextStatus);
      message.success('Agent 状态已刷新');
    } catch (error) {
      console.error(error);
      message.error(error instanceof Error ? error.message : '无法刷新 Agent 状态');
    } finally {
      setRefreshing(false);
    }
  };

  if (loading) return <Spin size="large" style={{ display: 'block', margin: '100px auto' }} />;

  const agentStates = Object.values(status?.agents || {});
  const healthStates = Object.values(status?.agent_health || {});
  const registeredAgentCount = agentStates.length;
  const workingAgentCount = agentStates.filter((state) => state === 'working').length;
  const readyAgentCount = healthStates.filter((health) => health.ready).length;
  const unavailableAgentCount = healthStates.filter(
    (health) => health.state === 'unhealthy',
  ).length;
  const toolCount = status?.tools?.length || 0;

  const agentData = status
    ? Object.entries(status.agents).map(([id, state]) => ({
      id,
      state,
      health: status.agent_health?.[id],
    }))
    : [];

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>系统概览</Typography.Title>
        <Button
          type="primary"
          shape="circle"
          icon={<ReloadOutlined />}
          loading={refreshing}
          disabled={refreshing}
          aria-label="刷新 Agent 状态"
          title="重新检查全部 Agent"
          onClick={() => { void refreshAgentHealth(); }}
        />
      </div>
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={12} lg={6}>
          <StatusCard title="可用 Agent" value={readyAgentCount} color="#52c41a" icon={<RobotOutlined />} />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatusCard title="不可用 Agent" value={unavailableAgentCount} color="#ff4d4f" icon={<RobotOutlined />} />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatusCard title="工作中 Agent" value={workingAgentCount} color="#1677ff" icon={<RobotOutlined />} />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatusCard title="已注册工具" value={toolCount} color="#722ed1" icon={<ThunderboltOutlined />} />
        </Col>
      </Row>

      <Typography.Title level={5}>Agent 状态（共 {registeredAgentCount} 个）</Typography.Title>
      <Table
        dataSource={agentData}
        rowKey="id"
        pagination={false}
        columns={[
          { title: 'Agent ID', dataIndex: 'id', key: 'id' },
          {
            title: '活动状态',
            dataIndex: 'state',
            key: 'state',
            render: (state: string) => {
              const presentation = agentStatePresentation[state] || agentStatePresentation.idle;
              return <Tag color={presentation.color}>{presentation.label}</Tag>;
            },
          },
          {
            title: '健康状态',
            key: 'health',
            render: (_, agent) => {
              const health = agent.health;
              const presentation = healthPresentation[health?.state || 'unknown'];
              return <Tag color={presentation.color}>{presentation.label}</Tag>;
            },
          },
          {
            title: '最近检查',
            key: 'last_checked_at',
            render: (_, agent) => agent.health?.last_checked_at
              ? `${new Date(agent.health.last_checked_at).toLocaleTimeString()} · ${agent.health.latency_ms ?? '-'} ms`
              : '-',
          },
          {
            title: '异常原因',
            key: 'health_error',
            ellipsis: true,
            render: (_, agent) => agent.health?.error || '-',
          },
        ]}
      />
    </>
  );
}
