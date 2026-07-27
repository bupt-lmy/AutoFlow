import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Tag,
  Typography,
} from 'antd';
import {
  ArrowLeftOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  RedoOutlined,
  RobotOutlined,
} from '@ant-design/icons';
import WorkflowRunCanvas from '../components/WorkflowRunCanvas';
import type { RunNodeState, RunNodeStatus } from '../components/WorkflowRunCanvas';
import RunActivityTimeline from '../components/RunActivityTimeline';
import type { PlatformEdge, PlatformNode } from '../components/WorkflowBuilder';
import LiveEventLog from '../components/LiveEventLog';
import { fetchApi } from '../api/client';
import { useWebSocket } from '../api/ws';
import type { WsEvent } from '../api/ws';

interface WorkflowDefinition {
  id: string;
  name: string;
  nodes: PlatformNode[];
  edges: PlatformEdge[];
}

interface PersistedNodeRun {
  node_id: string;
  agent_id?: string;
  status: string;
  error?: string | null;
}

interface RunDetail {
  status: string;
  started_at?: string;
  completed_at?: string | null;
  input_data?: string;
  workflow_snapshot?: WorkflowDefinition;
  node_runs: PersistedNodeRun[];
  events: WsEvent[];
}

const activeStatuses: RunNodeStatus[] = ['queued', 'running', 'reviewing', 'retrying'];
const terminalStatuses = new Set([
  'completed',
  'error',
  'failed',
  'timeout',
  'max_iterations_reached',
  'aborted',
]);

function normalizeStatus(status: string): RunNodeStatus {
  if (status === 'completed') return 'completed';
  if (status === 'error' || status === 'failed') return 'error';
  if (status === 'running') return 'running';
  if (status === 'queued') return 'queued';
  return 'pending';
}

function errorFromEvent(event: WsEvent): string | undefined {
  const value = event.data?.error || event.data?.payload?.error || event.data?.payload?.failures;
  if (!value) return undefined;
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return text.replace(/\s+/g, ' ').trim();
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    completed: '已完成',
    error: '执行失败',
    failed: '执行失败',
    timeout: '执行超时',
    max_iterations_reached: '达到最大迭代次数',
    running: '运行中',
    stalled: '等待人工处理',
    aborted: '已终止',
  };
  return labels[status] || status;
}

function resolveEventNodeId(workflow: WorkflowDefinition, event: WsEvent): string | undefined {
  const explicitNodeId = event.data?.node_id;
  if (explicitNodeId && workflow.nodes.some((node) => node.id === explicitNodeId)) {
    return explicitNodeId;
  }
  const agentId = event.data?.agent_id || event.data?.supervisor_agent_id;
  if (typeof agentId !== 'string') return undefined;
  const exact = workflow.nodes.find((node) => node.agent_id === agentId);
  if (exact) return exact.id;
  return workflow.nodes.find((node) => (
    agentId === `${workflow.id}--${node.id}`
      || agentId.endsWith(`--${workflow.id}--${node.id}`)
  ))?.id;
}

export default function WorkflowRun() {
  const { id, runId } = useParams<{ id: string; runId: string }>();
  const navigate = useNavigate();
  const [workflow, setWorkflow] = useState<WorkflowDefinition | null>(null);
  const [run, setRun] = useState<RunDetail | null>(null);
  const { events: liveEvents, connected } = useWebSocket(runId || null);

  useEffect(() => {
    if (!id || !runId) return;
    Promise.all([
      fetchApi<WorkflowDefinition>(`/api/workflows/${id}`),
      fetchApi<RunDetail>(`/api/workflows/${id}/runs/${runId}`),
    ]).then(([definition, detail]) => {
      setWorkflow(definition);
      setRun(detail);
    }).catch(console.error);
  }, [id, runId]);

  const events = useMemo(() => {
    const existing = run?.events || [];
    const seen = new Set(existing.map((event) => `${event.timestamp || ''}:${event.type}:${JSON.stringify(event.data)}`));
    return [...existing, ...liveEvents.filter((event) => {
      const key = `${event.timestamp || ''}:${event.type}:${JSON.stringify(event.data)}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })];
  }, [run, liveEvents]);

  const displayWorkflow = run?.workflow_snapshot || workflow;
  const latestStatus = useMemo(() => {
    const workflowEvent = [...events].reverse().find((event) => event.type.startsWith('workflow.'));
    if (workflowEvent?.type === 'workflow.completed') return 'completed';
    if (workflowEvent?.type === 'workflow.failed') return 'error';
    return run?.status || 'running';
  }, [events, run]);

  const nodeRuns = useMemo<RunNodeState[]>(() => {
    if (!displayWorkflow) return [];
    const states = new Map<string, RunNodeState>(displayWorkflow.nodes.map((node) => [node.id, {
      node_id: node.id,
      status: 'pending',
      attempts: 0,
    }]));
    (run?.node_runs || []).forEach((nodeRun) => {
      states.set(nodeRun.node_id, {
        node_id: nodeRun.node_id,
        status: normalizeStatus(nodeRun.status),
        attempts: 0,
        error: nodeRun.error,
      });
    });
    events.forEach((event) => {
      const nodeId = resolveEventNodeId(displayWorkflow, event);
      if (!nodeId || !states.has(nodeId)) return;
      const current = states.get(nodeId)!;
      if (event.type === 'node.task_assigned') {
        current.status = 'queued';
      } else if (event.type === 'node.task_started') {
        current.attempts += 1;
        current.status = current.attempts > 1 ? 'retrying' : 'running';
      } else if (event.type === 'node.result_ready') {
        current.status = 'completed';
        current.error = null;
      } else if (event.type === 'node.error') {
        current.status = 'error';
        current.error = errorFromEvent(event) || 'Agent 返回错误，但未提供具体原因。';
      } else if (event.type === 'execution.agent_retry') {
        current.attempts = Math.max(
          current.attempts,
          Number(event.data?.arguments?.next_attempt || current.attempts + 1),
        );
        current.status = 'retrying';
        current.error = errorFromEvent(event) || 'Agent 调用失败，正在自动重试。';
      } else if (event.type === 'supervisor.review_started') {
        current.status = 'reviewing';
      } else if (event.type === 'supervisor.decision_ready' && current.status === 'reviewing') {
        current.status = 'completed';
      }
      current.lastEventAt = event.timestamp;
    });
    states.forEach((state) => {
      if (state.attempts === 0 && ['running', 'completed', 'error'].includes(state.status)) {
        state.attempts = 1;
      }
      if (terminalStatuses.has(latestStatus) && ['pending', 'queued'].includes(state.status)) {
        state.status = 'skipped';
        state.error = null;
      }
    });
    return Array.from(states.values());
  }, [displayWorkflow, events, latestStatus, run]);
  const activeNode = useMemo(() => [...nodeRuns]
    .reverse()
    .find((node) => activeStatuses.includes(node.status)), [nodeRuns]);
  const activeDefinition = displayWorkflow?.nodes.find((node) => node.id === activeNode?.node_id);
  const completedCount = nodeRuns.filter((node) => node.status === 'completed').length;
  const skippedCount = nodeRuns.filter((node) => node.status === 'skipped').length;
  const participatedCount = nodeRuns.length - skippedCount;
  const retryCount = nodeRuns.reduce((total, node) => total + Math.max(0, node.attempts - 1), 0);
  const errorNodes = nodeRuns.filter((node) => node.status === 'error');
  const progress = participatedCount
    ? Math.round((completedCount / participatedCount) * 100)
    : 0;
  const latestIssueEvent = useMemo(() => [...events].reverse().find((event) => (
    event.type === 'node.error'
      || event.type === 'workflow.failed'
      || event.type === 'execution.agent_retry'
      || event.type === 'execution.tool_error'
      || event.type === 'execution.llm_error'
      || event.type === 'execution.skill_error'
  )), [events]);
  const latestIssue = latestIssueEvent ? errorFromEvent(latestIssueEvent) : undefined;
  const latestIssueNodeId = latestIssueEvent?.data?.node_id
    || (displayWorkflow && latestIssueEvent
      ? resolveEventNodeId(displayWorkflow, latestIssueEvent)
      : undefined);
  const latestIssueNode = nodeRuns.find((node) => node.node_id === latestIssueNodeId);
  const issueResolved = latestIssueEvent?.type === 'execution.agent_retry'
    || (latestIssueNode ? latestIssueNode.status !== 'error' : latestStatus === 'completed');

  const currentTitle = latestStatus === 'completed'
    ? '工作流已经完成'
    : latestStatus === 'error'
      ? '工作流执行失败'
      : activeNode?.status === 'retrying'
        ? `正在重新处理：${activeDefinition?.label || activeNode.node_id}`
        : activeNode?.status === 'reviewing'
          ? `Supervisor 正在审核：${activeDefinition?.label || activeNode.node_id}`
          : activeNode
            ? `正在处理：${activeDefinition?.label || activeNode.node_id}`
            : '正在等待下一步调度';
  const currentDescription = activeNode?.status === 'retrying'
    ? `第 ${activeNode.attempts} 轮处理${activeNode.error ? `；上一次问题：${activeNode.error}` : ''}`
    : activeNode?.status === 'reviewing'
      ? 'Supervisor 正在读取本轮输出，并判断继续、返工、改派还是结束。'
      : activeDefinition?.agent_id
        ? `当前执行 Agent：${activeDefinition.agent_id}`
        : latestStatus === 'completed'
          ? '所有必要步骤已经结束，可在下方查看关键交互和最终结果。'
          : '等待新的节点事件。';

  if (!displayWorkflow || !run) return <Spin size="large" style={{ display: 'block', margin: '100px auto' }} />;

  return (
    <>
      <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, gap: 16 }}>
        <Space wrap>
          <Button
            type="text"
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate(`/workflows/${id}`)}
            aria-label="返回工作流"
          >
            返回工作流
          </Button>
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>{displayWorkflow.name || displayWorkflow.id}</Typography.Title>
            <Typography.Text type="secondary">运行 ID：{runId}</Typography.Text>
          </div>
        </Space>
        <Space wrap>
          <Tag
            icon={connected && latestStatus === 'running' ? <LoadingOutlined /> : undefined}
            color={connected && latestStatus === 'running' ? 'processing' : 'default'}
          >
            {latestStatus === 'completed' || latestStatus === 'error'
              ? '执行记录'
              : connected
                ? '实时更新中'
                : '等待重新连接'}
          </Tag>
          <Tag color={latestStatus === 'completed' ? 'green' : latestStatus === 'error' ? 'red' : 'blue'}>
            {statusLabel(latestStatus)}
          </Tag>
        </Space>
      </div>

      <Card style={{ marginBottom: 16, borderRadius: 14 }} styles={{ body: { padding: 18 } }}>
        <Row gutter={[20, 18]} align="middle">
          <Col xs={24} lg={12}>
            <Space align="start" size={12}>
              <div style={{
                display: 'grid',
                placeItems: 'center',
                width: 42,
                height: 42,
                borderRadius: 12,
                color: latestStatus === 'error' ? '#cf1322' : latestStatus === 'completed' ? '#237804' : '#0958d9',
                background: latestStatus === 'error' ? '#fff2f0' : latestStatus === 'completed' ? '#f6ffed' : '#e6f4ff',
                fontSize: 20,
              }}>
                {latestStatus === 'error'
                  ? <CloseCircleOutlined />
                  : latestStatus === 'completed'
                    ? <CheckCircleOutlined />
                    : activeNode?.status === 'retrying'
                      ? <RedoOutlined spin />
                      : <RobotOutlined />}
              </div>
              <div>
                <Typography.Title level={4} style={{ margin: 0 }}>{currentTitle}</Typography.Title>
                <Typography.Paragraph type="secondary" style={{ margin: '5px 0 0', maxWidth: 620 }}>
                  {currentDescription}
                </Typography.Paragraph>
              </div>
            </Space>
          </Col>
          <Col xs={24} lg={12}>
            <Progress
              percent={progress}
              status={latestStatus === 'error' ? 'exception' : latestStatus === 'completed' ? 'success' : 'active'}
              strokeColor={latestStatus === 'completed' ? '#52c41a' : undefined}
              format={() => `${completedCount}/${participatedCount} 参与节点完成${skippedCount ? ` · ${skippedCount} 未参与` : ''}`}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {run.started_at ? `开始于 ${new Date(run.started_at).toLocaleString()}` : '开始时间未记录'}
            </Typography.Text>
          </Col>
        </Row>
        <Row gutter={[12, 12]} style={{ marginTop: 14 }}>
          <Col xs={12} sm={6}><Statistic title="已完成节点" value={completedCount} prefix={<CheckCircleOutlined />} /></Col>
          <Col xs={12} sm={6}><Statistic title="当前活跃" value={activeNode ? 1 : 0} prefix={<LoadingOutlined />} /></Col>
          <Col xs={12} sm={6}><Statistic title="重试 / 返工" value={retryCount} prefix={<RedoOutlined />} /></Col>
          <Col xs={12} sm={6}><Statistic title="未解决问题" value={errorNodes.length} prefix={<CloseCircleOutlined />} /></Col>
        </Row>
      </Card>

      {latestIssue && (
        <Alert
          style={{ marginBottom: 16, borderRadius: 10 }}
          type={issueResolved ? 'warning' : 'error'}
          showIcon
          message={latestIssueEvent?.type === 'execution.agent_retry'
            ? 'Agent 调用失败，正在自动重试'
            : latestStatus === 'completed' && issueResolved
              ? '执行期间曾遇到问题，已恢复且不影响最终完成'
            : issueResolved
              ? '最近发生过问题，当前已继续执行'
              : '当前阻塞问题'}
          description={latestIssue}
          action={latestIssueNodeId && (
            <Tag>{displayWorkflow.nodes.find((node) => node.id === latestIssueNodeId)?.label}</Tag>
          )}
        />
      )}

      <WorkflowRunCanvas nodes={displayWorkflow.nodes} edges={displayWorkflow.edges} nodeRuns={nodeRuns} />

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col span={24}>
          <Card
            title="关键交互进展"
            extra={<Typography.Text type="secondary">最新动态优先，无需阅读原始 JSON</Typography.Text>}
            style={{ borderRadius: 14 }}
          >
            <RunActivityTimeline events={events} nodes={displayWorkflow.nodes} />
          </Card>
        </Col>
        <Col span={24}>
          <Collapse
            ghost
            items={[{
              key: 'event-log',
              label: `技术详情（事件日志，共 ${events.length} 条）`,
              children: <LiveEventLog events={events} />,
            }]}
          />
        </Col>
      </Row>
    </>
  );
}
