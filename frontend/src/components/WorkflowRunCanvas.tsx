import { useMemo } from 'react';
import {
  CheckCircleFilled,
  ClockCircleOutlined,
  CloseCircleFilled,
  LoadingOutlined,
  MinusCircleOutlined,
  RedoOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
} from 'reactflow';
import type { Edge, Node, NodeProps } from 'reactflow';
import 'reactflow/dist/style.css';
import './WorkflowRunCanvas.css';
import type { PlatformEdge, PlatformNode } from './WorkflowBuilder';
import LoopbackEdge from './LoopbackEdge';

export type RunNodeStatus =
  | 'pending'
  | 'queued'
  | 'running'
  | 'reviewing'
  | 'retrying'
  | 'completed'
  | 'skipped'
  | 'error';

export interface RunNodeState {
  node_id: string;
  status: RunNodeStatus;
  attempts: number;
  error?: string | null;
  lastEventAt?: string;
}

interface Props {
  nodes: PlatformNode[];
  edges: PlatformEdge[];
  nodeRuns: RunNodeState[];
}

interface RunNodeData {
  label: string;
  agentId: string;
  status: RunNodeStatus;
  attempts: number;
  error?: string | null;
}

const presentation: Record<RunNodeStatus, {
  label: string;
  icon: React.ReactNode;
}> = {
  pending: { label: '等待执行', icon: <ClockCircleOutlined /> },
  queued: { label: '已进入队列', icon: <ClockCircleOutlined /> },
  running: { label: '正在工作', icon: <LoadingOutlined spin /> },
  reviewing: { label: '正在审核', icon: <SafetyCertificateOutlined /> },
  retrying: { label: '重新处理中', icon: <RedoOutlined spin /> },
  completed: { label: '已完成', icon: <CheckCircleFilled /> },
  skipped: { label: '未参与', icon: <MinusCircleOutlined /> },
  error: { label: '执行失败', icon: <CloseCircleFilled /> },
};

const connectorStyle = {
  width: 8,
  height: 8,
  border: '2px solid #fff',
  background: '#8c8c8c',
};

function RunNode({ data }: NodeProps<RunNodeData>) {
  const state = presentation[data.status];
  const active = ['running', 'reviewing', 'retrying'].includes(data.status);
  const issue = data.error?.replace(/\s+/g, ' ').trim();
  return (
    <div className={`run-node run-node--${data.status}${active ? ' run-node--active' : ''}`}>
      <Handle type="target" position={Position.Left} style={connectorStyle} />
      <div className="run-node__status">
        <span className="run-node__icon">{state.icon}</span>
        <span>{state.label}</span>
        {data.attempts > 1 && <span className="run-node__attempt">第 {data.attempts} 轮</span>}
      </div>
      <div className="run-node__body">
        <div className="run-node__label">{data.label}</div>
        <div className="run-node__agent">{data.agentId || '运行时动态发现'}</div>
        {issue && <div className="run-node__error" title={issue}>{issue}</div>}
      </div>
      {active && <div className="run-node__activity" />}
      <Handle type="source" position={Position.Right} style={connectorStyle} />
    </div>
  );
}

const edgeTypes = { loopback: LoopbackEdge };
const nodeTypes = { runNode: RunNode };

export default function WorkflowRunCanvas({ nodes, edges, nodeRuns }: Props) {
  const statusByNode = useMemo(
    () => new Map(nodeRuns.map((run) => [run.node_id, run])),
    [nodeRuns],
  );
  const displayPositions = useMemo(() => {
    const xs = nodes.map((node) => node.position.x);
    const ys = nodes.map((node) => node.position.y);
    const minX = Math.min(...xs, 0);
    const maxX = Math.max(...xs, 0);
    const minY = Math.min(...ys, 0);
    const maxY = Math.max(...ys, 0);
    const rangeX = Math.max(maxX - minX, 1);
    const rangeY = Math.max(maxY - minY, 1);
    const horizontalSpan = Math.max(560, Math.min(820, nodes.length * 190));
    const verticalSpan = rangeY > 1 ? 210 : 0;
    return new Map(nodes.map((node) => [node.id, {
      x: ((node.position.x - minX) / rangeX) * horizontalSpan,
      y: ((node.position.y - minY) / rangeY) * verticalSpan,
    }]));
  }, [nodes]);
  const flowNodes = useMemo<Node<RunNodeData>[]>(() => nodes.map((node) => {
    const state = statusByNode.get(node.id) || {
      node_id: node.id,
      status: 'pending' as const,
      attempts: 0,
    };
    return {
      id: node.id,
      type: 'runNode',
      position: displayPositions.get(node.id) || node.position,
      data: {
        label: node.label,
        agentId: node.agent_id || '',
        status: state.status,
        attempts: state.attempts,
        error: state.error,
      },
    };
  }), [displayPositions, nodes, statusByNode]);
  const flowEdges = useMemo<Edge[]>(() => edges.map((edge) => {
    const sourceStatus = statusByNode.get(edge.source)?.status || 'pending';
    const targetStatus = statusByNode.get(edge.target)?.status || 'pending';
    const active = ['queued', 'running', 'reviewing', 'retrying'].includes(targetStatus);
    const skipped = targetStatus === 'skipped';
    const stroke = skipped
      ? '#bfbfbf'
      : targetStatus === 'error'
      ? '#ff4d4f'
      : targetStatus === 'retrying'
        ? '#fa8c16'
        : sourceStatus === 'completed' || targetStatus === 'completed'
          ? '#52c41a'
          : active
            ? '#1677ff'
            : '#bfbfbf';
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: edge.routing === 'loopback' ? 'loopback' : undefined,
      label: edge.condition
        ? `${edge.condition.field} ${edge.condition.operator} ${String(edge.condition.value)}`
        : undefined,
      animated: active && !skipped,
      style: {
        stroke,
        strokeWidth: active ? 2.5 : 1.5,
        strokeDasharray: skipped ? '5 5' : undefined,
      },
      labelStyle: { fill: '#595959', fontSize: 11 },
      labelBgStyle: { fill: '#fff', fillOpacity: 0.9 },
      markerEnd: { type: MarkerType.ArrowClosed, color: stroke },
    };
  }), [edges, statusByNode]);

  return (
    <div className="workflow-run-canvas">
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        edgeTypes={edgeTypes}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.25 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
      >
        <Background gap={18} size={1} color="#e8e8e8" />
        <Controls showInteractive={false} />
      </ReactFlow>
      <div className="workflow-run-legend" aria-label="节点状态图例">
        {(['running', 'reviewing', 'retrying', 'completed', 'skipped', 'error'] as RunNodeStatus[]).map((status) => (
          <span key={status} className={`workflow-run-legend__item workflow-run-legend__item--${status}`}>
            <i />{presentation[status].label}
          </span>
        ))}
      </div>
    </div>
  );
}
