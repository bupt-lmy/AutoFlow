import {
  CheckCircleFilled,
  CloseCircleFilled,
  LoadingOutlined,
  RedoOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import { Empty, Tag, Timeline, Typography } from 'antd';
import type { PlatformNode } from './WorkflowBuilder';
import type { WsEvent } from '../api/ws';

interface Props {
  events: WsEvent[];
  nodes: PlatformNode[];
  limit?: number;
}

interface Activity {
  key: string;
  time?: string;
  title: string;
  detail?: string;
  color: string;
  icon: React.ReactNode;
  tag?: string;
}

function compact(value: unknown, limit = 220): string | undefined {
  if (value === null || value === undefined || value === '') return undefined;
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  const normalized = text.replace(/\s+/g, ' ').trim();
  return normalized.length > limit ? `${normalized.slice(0, limit)}…` : normalized;
}

function readableSummary(value: unknown, depth = 0): string | undefined {
  if (depth > 4 || value === null || value === undefined || value === '') return undefined;
  if (typeof value === 'string') {
    const text = value.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '');
    if (text.startsWith('{') || text.startsWith('[')) {
      try {
        return readableSummary(JSON.parse(text), depth + 1) || compact(value);
      } catch {
        return compact(value);
      }
    }
    return compact(value);
  }
  if (typeof value === 'object') {
    const object = value as Record<string, unknown>;
    for (const key of ['summary', 'structured', 'content', 'output', 'result', 'error']) {
      const summary = readableSummary(object[key], depth + 1);
      if (summary) return summary;
    }
  }
  return compact(value);
}

function eventError(event: WsEvent): string | undefined {
  return compact(event.data?.error || event.data?.payload?.error || event.data?.payload?.failures);
}

export default function RunActivityTimeline({ events, nodes, limit = 12 }: Props) {
  const labelByNode = new Map(nodes.map((node) => [node.id, node.label]));
  const labelByAgent = new Map(nodes
    .filter((node) => node.agent_id)
    .map((node) => [node.agent_id as string, node.label]));
  const starts = new Map<string, number>();
  const lastError = new Map<string, string>();
  const activities: Activity[] = [];

  const nodeLabel = (event: WsEvent) => labelByNode.get(event.data?.node_id || '')
    || labelByAgent.get(event.data?.agent_id || '')
    || event.data?.agent_id
    || 'Agent';
  const nodeKey = (event: WsEvent) => event.data?.node_id || event.data?.agent_id || 'unknown';

  events.forEach((event, index) => {
    const key = `${event.timestamp || index}-${event.type}-${index}`;
    const base = { key, time: event.timestamp };
    if (event.type === 'workflow.started') {
      activities.push({
        ...base,
        title: '工作流已启动',
        detail: compact(event.data?.input),
        color: '#1677ff',
        icon: <LoadingOutlined />,
        tag: '开始',
      });
      return;
    }
    if (event.type === 'node.task_started') {
      const id = nodeKey(event);
      const attempt = (starts.get(id) || 0) + 1;
      starts.set(id, attempt);
      const retry = attempt > 1;
      activities.push({
        ...base,
        title: retry
          ? `${nodeLabel(event)} 正在重新处理（第 ${attempt} 轮）`
          : `${nodeLabel(event)} 开始处理任务`,
        detail: retry
          ? `再次执行原因：${lastError.get(id) || 'Supervisor 要求返工，或工作流按环形链路再次进入该节点。'}`
          : compact(event.data?.payload?.task || event.data?.payload?.content),
        color: retry ? '#fa8c16' : '#1677ff',
        icon: retry ? <RedoOutlined /> : <LoadingOutlined />,
        tag: retry ? '重试/返工' : '处理中',
      });
      return;
    }
    if (event.type === 'node.error') {
      const error = eventError(event) || 'Agent 返回错误，但没有提供更具体的错误信息。';
      lastError.set(nodeKey(event), error);
      activities.push({
        ...base,
        title: `${nodeLabel(event)} 遇到问题`,
        detail: error,
        color: '#ff4d4f',
        icon: <CloseCircleFilled />,
        tag: '问题',
      });
      return;
    }
    if (event.type === 'execution.agent_retry') {
      const error = eventError(event) || 'Agent 调用失败，正在自动重试。';
      lastError.set(nodeKey(event), error);
      const nextAttempt = Number(event.data?.arguments?.next_attempt || 2);
      starts.set(nodeKey(event), Math.max(starts.get(nodeKey(event)) || 0, nextAttempt));
      activities.push({
        ...base,
        title: `${nodeLabel(event)} 正在自动重试（第 ${nextAttempt} 次尝试）`,
        detail: `重试原因：${error}`,
        color: '#fa8c16',
        icon: <RedoOutlined />,
        tag: '自动重试',
      });
      return;
    }
    if (['execution.tool_error', 'execution.llm_error', 'execution.skill_error'].includes(event.type)) {
      activities.push({
        ...base,
        title: `${nodeLabel(event)} 遇到可恢复问题`,
        detail: eventError(event) || '执行过程中出现问题，Agent 将根据当前策略决定继续或失败。',
        color: '#fa8c16',
        icon: <CloseCircleFilled />,
        tag: '执行问题',
      });
      return;
    }
    if (event.type === 'node.result_ready') {
      const summary = readableSummary(event.data?.payload);
      activities.push({
        ...base,
        title: `${nodeLabel(event)} 已完成`,
        detail: summary,
        color: '#52c41a',
        icon: <CheckCircleFilled />,
        tag: '完成',
      });
      return;
    }
    if (event.type === 'supervisor.review_started') {
      const label = labelByAgent.get(event.data?.supervisor_agent_id || '')
        || event.data?.supervisor_agent_id
        || 'Supervisor';
      activities.push({
        ...base,
        title: `${label} 正在审核本轮结果`,
        detail: event.data?.terminal_candidate
          ? '当前结果已满足静态终止条件，仍需 Supervisor 做最终质量判断。'
          : 'Supervisor 正在判断继续、返工、改派还是结束。',
        color: '#722ed1',
        icon: <SafetyCertificateOutlined />,
        tag: '审核',
      });
      return;
    }
    if (event.type === 'supervisor.decision_ready') {
      const next = (event.data?.next_agents || [])
        .map((agentId: string) => labelByAgent.get(agentId) || agentId);
      activities.push({
        ...base,
        title: next.length
          ? `Supervisor 决定继续到：${next.join('、')}`
          : 'Supervisor 已完成本轮决策',
        detail: next.length
          ? '目标节点将收到当前链路累计的结果和 Supervisor 指令。'
          : `流程结果：${event.data?.outcome || '等待最终汇总'}`,
        color: '#722ed1',
        icon: <SafetyCertificateOutlined />,
        tag: next.length ? '继续' : '决策',
      });
      return;
    }
    if (event.type === 'workflow.completed') {
      activities.push({
        ...base,
        title: '工作流已完成',
        detail: readableSummary(event.data),
        color: '#52c41a',
        icon: <CheckCircleFilled />,
        tag: '完成',
      });
      return;
    }
    if (event.type === 'workflow.failed') {
      activities.push({
        ...base,
        title: '工作流执行失败',
        detail: eventError(event) || compact(event.data),
        color: '#ff4d4f',
        icon: <CloseCircleFilled />,
        tag: '失败',
      });
    }
  });

  const visible = activities.slice(-limit).reverse();
  if (!visible.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="等待工作流产生执行动态" />;
  }
  return (
    <Timeline
      items={visible.map((activity) => ({
        color: activity.color,
        dot: activity.icon,
        children: (
          <div style={{ paddingBottom: 5 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <Typography.Text strong>{activity.title}</Typography.Text>
              {activity.tag && <Tag color={activity.color}>{activity.tag}</Tag>}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {activity.time ? new Date(activity.time).toLocaleTimeString() : ''}
              </Typography.Text>
            </div>
            {activity.detail && (
              <Typography.Paragraph
                type="secondary"
                style={{ margin: '4px 0 0', maxWidth: 820, whiteSpace: 'pre-wrap' }}
              >
                {activity.detail}
              </Typography.Paragraph>
            )}
          </div>
        ),
      }))}
    />
  );
}
