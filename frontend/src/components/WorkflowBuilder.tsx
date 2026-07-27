import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { Button, Checkbox, Collapse, Empty, Input, InputNumber, Select, Typography } from 'antd';
import ReactFlow, {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
} from 'reactflow';
import type { Connection, Edge, EdgeChange, Node, NodeChange, NodeProps } from 'reactflow';
import 'reactflow/dist/style.css';
import LoopbackEdge from './LoopbackEdge';

export interface AgentManifest {
  id: string;
  name: string;
  description: string;
  tags: string[];
  tools: string[];
  skills: string[];
  model?: string;
  health?: AgentHealth;
}

type AgentHealthState = 'unknown' | 'checking' | 'healthy' | 'unhealthy';

interface AgentHealth {
  state: AgentHealthState;
  ready: boolean;
  last_checked_at?: string | null;
  last_success_at?: string | null;
  latency_ms?: number | null;
  error?: string | null;
}

export interface ModelProfile {
  id: string;
  name: string;
  config: { provider: string; name: string };
}

export interface PlatformNode {
  id: string;
  node_type: 'agent' | 'discovery';
  agent_id: string | null;
  label: string;
  position: { x: number; y: number };
  is_entry: boolean;
  config: Record<string, unknown>;
}

export interface PlatformEdge {
  id: string;
  source: string;
  target: string;
  condition?: { field: string; operator: string; value: unknown } | null;
  payload_mapping?: { include?: string[]; task_field?: string | null } | null;
  routing?: 'default' | 'loopback';
}

interface AgentNodeData {
  nodeType: 'agent' | 'discovery';
  label: string;
  agentId: string;
  isEntry: boolean;
  responsibility: string;
  terminateOnSuccess: boolean;
  modelProfileId: string;
  discoveryDescription: string;
  requiredSkills: string[];
  requiredTools: string[];
  timeoutSeconds: number;
  fallbackEnabled: boolean;
  fallbackDescription: string;
  healthState: AgentHealthState;
}

const healthPresentation: Record<AgentHealthState, {
  label: string;
  border: string;
  background: string;
  text: string;
}> = {
  healthy: { label: '可用', border: '#52c41a', background: '#f6ffed', text: '#237804' },
  unhealthy: { label: '不可用', border: '#ff7875', background: '#fff2f0', text: '#a8071a' },
  checking: { label: '检查中', border: '#faad14', background: '#fffbe6', text: '#ad6800' },
  unknown: { label: '未知', border: '#bfbfbf', background: '#fafafa', text: '#595959' },
};

function agentHealth(agent?: AgentManifest) {
  return healthPresentation[agent?.health?.state || 'unknown'];
}

const connectorStyle = {
  width: 24,
  height: 24,
  background: '#1677ff',
  border: '3px solid #fff',
  borderRadius: '50%',
  boxShadow: '0 1px 5px rgba(0, 80, 179, 0.45)',
  cursor: 'crosshair',
  zIndex: 2,
};

function AgentNode({ data }: NodeProps<AgentNodeData>) {
  const dynamic = data.nodeType === 'discovery';
  const health = healthPresentation[data.healthState];
  const border = dynamic ? '#b37feb' : health.border;
  const background = dynamic ? '#f9f0ff' : health.background;
  const text = dynamic ? '#531dab' : health.text;
  return (
    <div style={{ minWidth: 170, border: `1px solid ${border}`, borderRadius: 6, background: '#fff', overflow: 'visible' }}>
      <Handle
        id="input"
        type="target"
        position={Position.Left}
        title="拖入一条连接"
        style={{ ...connectorStyle, left: -12 }}
      />
      <div style={{ padding: '8px 10px', background, color: text, fontWeight: 600, fontSize: 13 }}>
        {data.label}
      </div>
      <div style={{ padding: '7px 10px', color: '#595959', fontSize: 12 }}>
        {dynamic ? '运行时动态选择' : <>
          <span style={{ display: 'inline-block', width: 7, height: 7, borderRadius: '50%', background: health.border, marginRight: 5 }} />
          {health.label} · {data.agentId}
        </>}
        {data.isEntry && <span style={{ marginLeft: 8, color: '#389e0d' }}>入口</span>}
        {data.terminateOnSuccess && <span style={{ marginLeft: 8, color: '#d46b08' }}>结束</span>}
      </div>
      {(dynamic ? data.discoveryDescription : data.responsibility) && (
        <div style={{ padding: '0 10px 8px', color: '#595959', fontSize: 12, lineHeight: 1.45 }}>
          {(dynamic ? data.discoveryDescription : data.responsibility).length > 84
            ? `${(dynamic ? data.discoveryDescription : data.responsibility).slice(0, 84)}...`
            : (dynamic ? data.discoveryDescription : data.responsibility)}
        </div>
      )}
      <Handle
        id="output"
        type="source"
        position={Position.Right}
        title="拖向下一个 Agent"
        style={{ ...connectorStyle, right: -12 }}
      />
    </div>
  );
}

const nodeTypes = { agent: AgentNode };
const edgeTypes = { loopback: LoopbackEdge };

function createsCycle(edges: Array<Pick<Edge, 'source' | 'target'>>, source: string, target: string) {
  const adjacency = new Map<string, string[]>();
  edges.forEach((edge) => adjacency.set(edge.source, [...(adjacency.get(edge.source) || []), edge.target]));
  const pending = [target];
  const visited = new Set<string>();
  while (pending.length) {
    const current = pending.pop()!;
    if (current === source) return true;
    if (visited.has(current)) continue;
    visited.add(current);
    pending.push(...(adjacency.get(current) || []));
  }
  return false;
}

function routeLabel(
  condition?: PlatformEdge['condition'],
  mapping?: PlatformEdge['payload_mapping'],
) {
  const parts: string[] = [];
  if (condition) parts.push(`${condition.field} ${condition.operator} ${String(condition.value)}`);
  if (mapping?.include?.length) parts.push(`字段：${mapping.include.join(', ')}`);
  if (mapping?.task_field) parts.push(`任务 ← ${mapping.task_field}`);
  return parts.length ? parts.join(' · ') : undefined;
}

function toFlowNodes(nodes: PlatformNode[], agents: AgentManifest[] = []): Node<AgentNodeData>[] {
  return nodes.map((node) => {
    const discovery = (node.config?.discovery || {}) as Record<string, unknown>;
    const fallback = (node.config?.fallback_discovery || {}) as Record<string, unknown>;
    const nodeType = node.node_type || (node.agent_id ? 'agent' : 'discovery');
    const manifest = agents.find((agent) => agent.id === node.agent_id);
    return {
      id: node.id,
      type: 'agent',
      position: node.position,
      data: {
        nodeType,
        label: node.label === node.agent_id && node.agent_id
          ? manifest?.name || node.label
          : node.label,
        agentId: node.agent_id || '',
        isEntry: node.is_entry,
        responsibility: typeof node.config?.responsibility === 'string' ? node.config.responsibility : '',
        terminateOnSuccess: node.config?.terminate_on_success === true,
        modelProfileId: typeof node.config?.model_profile_id === 'string' ? node.config.model_profile_id : '',
        discoveryDescription: typeof discovery.description === 'string' ? discovery.description : '',
        requiredSkills: Array.isArray(discovery.required_skills) ? discovery.required_skills as string[] : [],
        requiredTools: Array.isArray(discovery.required_tools) ? discovery.required_tools as string[] : [],
        timeoutSeconds: typeof discovery.timeout_seconds === 'number'
          ? discovery.timeout_seconds
          : (typeof fallback.timeout_seconds === 'number' ? fallback.timeout_seconds : 300),
        fallbackEnabled: Boolean(node.config?.fallback_discovery),
        fallbackDescription: typeof fallback.description === 'string' ? fallback.description : '',
        healthState: manifest?.health?.state || 'unknown',
      },
    };
  });
}

function toFlowEdges(edges: PlatformEdge[]): Edge[] {
  const converted: Edge[] = [];
  edges.forEach((edge) => {
    const routing = edge.routing === 'loopback' || createsCycle(converted, edge.source, edge.target)
      ? 'loopback'
      : 'default';
    converted.push({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: routing === 'loopback' ? 'loopback' : undefined,
      data: {
        condition: edge.condition || null,
        payloadMapping: edge.payload_mapping || null,
        routing,
      },
      label: routeLabel(edge.condition, edge.payload_mapping),
      markerEnd: { type: MarkerType.ArrowClosed },
    });
  });
  return converted;
}

function toPlatformNodes(nodes: Node<AgentNodeData>[]): PlatformNode[] {
  return nodes.map((node) => ({
    id: node.id,
    node_type: node.data.nodeType,
    agent_id: node.data.nodeType === 'agent' ? node.data.agentId : null,
    label: node.data.label,
    position: node.position,
    is_entry: node.data.isEntry,
    config: {
      ...(node.data.responsibility.trim() ? { responsibility: node.data.responsibility.trim() } : {}),
      ...(node.data.modelProfileId ? { model_profile_id: node.data.modelProfileId } : {}),
      ...(node.data.nodeType === 'discovery' ? {
        discovery: {
          description: node.data.discoveryDescription.trim(),
          required_skills: node.data.requiredSkills,
          required_tools: node.data.requiredTools,
          timeout_seconds: node.data.timeoutSeconds,
          fallback_on_error: true,
          fallback_on_timeout: true,
        },
      } : {}),
      ...(node.data.nodeType === 'agent' && node.data.fallbackEnabled ? {
        fallback_discovery: {
          description: node.data.fallbackDescription.trim() || node.data.responsibility.trim() || node.data.label,
          timeout_seconds: node.data.timeoutSeconds,
          fallback_on_error: true,
          fallback_on_timeout: true,
        },
      } : {}),
      terminate_on_success: node.data.terminateOnSuccess,
    },
  }));
}

function toPlatformEdges(edges: Edge[]): PlatformEdge[] {
  return edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    condition: (edge.data?.condition as PlatformEdge['condition']) || null,
    payload_mapping: (edge.data?.payloadMapping as PlatformEdge['payload_mapping']) || null,
    routing: edge.type === 'loopback' || edge.data?.routing === 'loopback' ? 'loopback' : 'default',
  }));
}

interface Props {
  initialNodes: PlatformNode[];
  initialEdges: PlatformEdge[];
  agents: AgentManifest[];
  modelProfiles?: ModelProfile[];
  onGraphChange?: (graph: { nodes: PlatformNode[]; edges: PlatformEdge[] }) => void;
}

export interface WorkflowBuilderHandle {
  getGraph: () => { nodes: PlatformNode[]; edges: PlatformEdge[] };
}

const WorkflowBuilder = forwardRef<WorkflowBuilderHandle, Props>(function WorkflowBuilder(
  { initialNodes, initialEdges, agents, modelProfiles = [], onGraphChange },
  ref,
) {
  const [nodes, setNodes] = useState<Node<AgentNodeData>[]>(() => toFlowNodes(initialNodes, agents));
  const [edges, setEdges] = useState<Edge[]>(() => toFlowEdges(initialEdges));
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [agentQuery, setAgentQuery] = useState('');
  const canvasRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setNodes(toFlowNodes(initialNodes, agents));
    setEdges(toFlowEdges(initialEdges));
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
  // The editor is remounted for each workflow route; do not reset on every drag.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialNodes.length === 0 ? '' : initialNodes[0].id]);

  useEffect(() => {
    const healthById = new Map(agents.map((agent) => [agent.id, agent.health?.state || 'unknown']));
    setNodes((current) => current.map((node) => (
      node.data.nodeType === 'agent'
        ? { ...node, data: { ...node.data, healthState: healthById.get(node.data.agentId) || 'unknown' } }
        : node
    )));
  }, [agents]);

  useImperativeHandle(ref, () => ({
    getGraph: () => ({ nodes: toPlatformNodes(nodes), edges: toPlatformEdges(edges) }),
  }), [nodes, edges]);

  useEffect(() => {
    onGraphChange?.({ nodes: toPlatformNodes(nodes), edges: toPlatformEdges(edges) });
  }, [nodes, edges, onGraphChange]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setNodes((current) => {
      const next = applyNodeChanges(changes, current) as Node<AgentNodeData>[];
      return next;
    });
  }, []);

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges((current) => {
      const next = applyEdgeChanges(changes, current);
      return next;
    });
  }, []);

  const onConnect = useCallback((connection: Connection) => {
    if (!connection.source || !connection.target || connection.source === connection.target) return;
    setEdges((current) => {
      const routing = createsCycle(current, connection.source!, connection.target!)
        ? 'loopback'
        : 'default';
      const next = addEdge({
        ...connection,
        type: routing === 'loopback' ? 'loopback' : undefined,
        data: { condition: null, payloadMapping: null, routing },
        markerEnd: { type: MarkerType.ArrowClosed },
      }, current);
      return next;
    });
    setNodes((current) => current.map((node) => {
      if (node.id === connection.source) {
        return { ...node, data: { ...node.data, terminateOnSuccess: false } };
      }
      if (node.id === connection.target && !edges.some((edge) => edge.source === node.id)) {
        return { ...node, data: { ...node.data, terminateOnSuccess: true } };
      }
      return node;
    }));
  }, [edges]);

  const selectedNode = useMemo(
    () => nodes.find((node) => node.id === selectedNodeId) || null,
    [nodes, selectedNodeId],
  );
  const selectedEdge = useMemo(
    () => edges.find((edge) => edge.id === selectedEdgeId) || null,
    [edges, selectedEdgeId],
  );
  const selectedRoutePreset = useMemo(() => {
    const condition = selectedEdge?.data?.condition as PlatformEdge['condition'];
    if (!condition) return 'always';
    if (condition.field === 'status' && condition.operator === 'eq' && condition.value === 'success') {
      return 'success';
    }
    if (condition.field === 'status' && condition.operator === 'eq' && condition.value === 'error') {
      return 'error';
    }
    return 'custom';
  }, [selectedEdge]);
  const filteredAgents = useMemo(() => {
    const query = agentQuery.trim().toLocaleLowerCase();
    if (!query) return agents;
    return agents.filter((agent) => (
      agent.name.toLocaleLowerCase().includes(query)
      || agent.description.toLocaleLowerCase().includes(query)
    ));
  }, [agentQuery, agents]);

  const updateNode = (patch: Partial<AgentNodeData>) => {
    if (!selectedNode) return;
    setNodes((current) => {
      const next = current.map((node) => {
        if (node.id !== selectedNode.id) return node;
        if (patch.isEntry) {
          return { ...node, data: { ...node.data, ...patch, isEntry: true } };
        }
        return { ...node, data: { ...node.data, ...patch } };
      }).map((node) => (
        patch.isEntry && node.id !== selectedNode.id
          ? { ...node, data: { ...node.data, isEntry: false } }
          : node
      ));
      return next;
    });
  };

  const updateEdgeCondition = (condition: PlatformEdge['condition']) => {
    if (!selectedEdge) return;
    setEdges((current) => {
      const next = current.map((edge) => edge.id === selectedEdge.id ? {
        ...edge,
        data: { ...edge.data, condition },
        label: routeLabel(
          condition,
          edge.data?.payloadMapping as PlatformEdge['payload_mapping'],
        ),
      } : edge);
      return next;
    });
  };

  const updateEdgePayloadMapping = (mapping: PlatformEdge['payload_mapping']) => {
    if (!selectedEdge) return;
    setEdges((current) => current.map((edge) => edge.id === selectedEdge.id ? {
      ...edge,
      data: { ...edge.data, payloadMapping: mapping },
      label: routeLabel(edge.data?.condition as PlatformEdge['condition'], mapping),
    } : edge));
  };

  const onDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    const agentId = event.dataTransfer.getData('application/axonflow-agent');
    const isDiscovery = event.dataTransfer.getData('application/axonflow-discovery') === 'true';
    const agent = agents.find((item) => item.id === agentId);
    const bounds = canvasRef.current?.getBoundingClientRect();
    if ((!agent && !isDiscovery) || !bounds) return;
    const id = `node-${isDiscovery ? 'discovery' : agent!.id}-${crypto.randomUUID().slice(0, 8)}`;
    const nextNode: Node<AgentNodeData> = {
      id,
      type: 'agent',
      position: { x: event.clientX - bounds.left - 85, y: event.clientY - bounds.top - 35 },
      data: {
        nodeType: isDiscovery ? 'discovery' : 'agent',
        label: isDiscovery ? '动态 Agent' : agent!.name,
        agentId: isDiscovery ? '' : agent!.id,
        isEntry: nodes.length === 0,
        responsibility: '',
        terminateOnSuccess: true,
        modelProfileId: '',
        discoveryDescription: '',
        requiredSkills: [],
        requiredTools: [],
        timeoutSeconds: 300,
        fallbackEnabled: false,
        fallbackDescription: '',
        healthState: isDiscovery ? 'unknown' : agent!.health?.state || 'unknown',
      },
    };
    const next = [...nodes, nextNode];
    setNodes(next);
  };

  const deleteSelectedNode = () => {
    if (!selectedNode) return;
    setNodes((current) => {
      const remaining = current.filter((node) => node.id !== selectedNode.id);
      if (selectedNode.data.isEntry && remaining.length) {
        return remaining.map((node, index) => ({
          ...node,
          data: { ...node.data, isEntry: index === 0 },
        }));
      }
      return remaining;
    });
    setEdges((current) => current.filter((edge) => (
      edge.source !== selectedNode.id && edge.target !== selectedNode.id
    )));
    setSelectedNodeId(null);
  };

  const deleteSelectedEdge = () => {
    if (!selectedEdge) return;
    setEdges((current) => current.filter((edge) => edge.id !== selectedEdge.id));
    setSelectedEdgeId(null);
  };

  return (
    <div style={{ overflowX: 'auto', border: '1px solid #d9d9d9', background: '#fff' }}>
      <div style={{ height: 640, minWidth: 820, display: 'grid', gridTemplateColumns: '220px minmax(0, 1fr) 300px' }}>
      <aside style={{ borderRight: '1px solid #f0f0f0', padding: 14, overflowY: 'auto' }}>
        <Typography.Text strong>Agent 模板</Typography.Text>
        <Input.Search
          allowClear
          value={agentQuery}
          onChange={(event) => setAgentQuery(event.target.value)}
          placeholder="搜索名称或说明"
          style={{ marginTop: 10 }}
        />
        <div style={{ marginTop: 12, display: 'grid', gap: 8 }}>
          <div
            draggable
            onDragStart={(event) => {
              event.dataTransfer.setData('application/axonflow-discovery', 'true');
              event.dataTransfer.effectAllowed = 'move';
            }}
            title="运行工作流时根据能力说明选择合适的 Agent"
            style={{ padding: '9px 10px', border: '1px dashed #9254de', borderRadius: 4, cursor: 'grab', background: '#f9f0ff' }}
          >
            <div style={{ fontSize: 13, fontWeight: 600, color: '#531dab' }}>动态 Agent</div>
            <div style={{ color: '#8c8c8c', fontSize: 11, marginTop: 2 }}>运行时按能力自动选择</div>
          </div>
          {filteredAgents.map((agent) => {
            const health = agentHealth(agent);
            return (
            <div
              key={agent.id}
              draggable
              onDragStart={(event) => {
                event.dataTransfer.setData('application/axonflow-agent', agent.id);
                event.dataTransfer.effectAllowed = 'move';
              }}
              title={agent.description}
              style={{ padding: '9px 10px', border: `1px solid ${health.border}`, borderRadius: 4, cursor: 'grab', background: health.background }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 6, alignItems: 'center' }}>
                <span style={{ fontSize: 13, fontWeight: 600 }}>{agent.name}</span>
                <span style={{ color: health.text, fontSize: 10, whiteSpace: 'nowrap' }}>
                  <span style={{ display: 'inline-block', width: 7, height: 7, borderRadius: '50%', background: health.border, marginRight: 4 }} />
                  {health.label}
                </span>
              </div>
              <div style={{ color: '#8c8c8c', fontSize: 11, marginTop: 2 }}>{agent.id}</div>
              {agent.description && <div style={{ color: '#595959', fontSize: 11, marginTop: 4, lineHeight: 1.35, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                {agent.description}
              </div>}
            </div>
            );
          })}
          {filteredAgents.length === 0 && (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的 Agent" />
          )}
        </div>
      </aside>
      <div
        ref={canvasRef}
        onDrop={onDrop}
        onDragOver={(event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = 'move';
        }}
        style={{ minWidth: 0 }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={(_, node) => { setSelectedNodeId(node.id); setSelectedEdgeId(null); }}
          onEdgeClick={(_, edge) => { setSelectedEdgeId(edge.id); setSelectedNodeId(null); }}
          onPaneClick={() => { setSelectedNodeId(null); setSelectedEdgeId(null); }}
          fitView
        >
          <Background gap={18} size={1} color="#e8e8e8" />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </div>
      <aside style={{ borderLeft: '1px solid #f0f0f0', padding: 14, overflowY: 'auto' }}>
        {selectedNode && (
          <div style={{ display: 'grid', gap: 10 }}>
            <div>
              <Typography.Text strong>节点设置</Typography.Text>
              <div style={{ color: '#8c8c8c', fontSize: 12, marginTop: 3 }}>
                先完成常用设置，其他能力可在高级设置中调整。
              </div>
            </div>
            {selectedNode.data.nodeType === 'agent' && <div>
              <Typography.Text type="secondary">Agent 模板</Typography.Text>
              <Select
                style={{ width: '100%', marginTop: 4 }}
                value={selectedNode.data.agentId}
                options={agents.map((agent) => ({
                  value: agent.id,
                  label: `${agent.name} · ${agentHealth(agent).label}`,
                }))}
                onChange={(agentId) => {
                  const agent = agents.find((item) => item.id === agentId);
                  updateNode({
                    agentId,
                    label: agent?.name || agentId,
                    healthState: agent?.health?.state || 'unknown',
                  });
                }}
              />
            </div>}
            <div>
              <Typography.Text type="secondary">节点名称</Typography.Text>
              <Input value={selectedNode.data.label} onChange={(event) => updateNode({ label: event.target.value })} style={{ marginTop: 4 }} />
            </div>
            {selectedNode.data.nodeType === 'discovery' && <>
              <div>
                <Typography.Text type="secondary">需要完成的任务</Typography.Text>
                <Input.TextArea
                  rows={3}
                  value={selectedNode.data.discoveryDescription}
                  placeholder="描述运行时 Agent 需要完成什么"
                  onChange={(event) => updateNode({ discoveryDescription: event.target.value })}
                  style={{ marginTop: 4 }}
                />
              </div>
            </>}
            {selectedNode.data.nodeType === 'agent' && <div>
              <Typography.Text type="secondary">节点职责</Typography.Text>
              <Input.TextArea
                rows={3}
                value={selectedNode.data.responsibility}
                placeholder="描述此 Agent 在当前工作流中的职责"
                onChange={(event) => updateNode({ responsibility: event.target.value })}
                style={{ marginTop: 4 }}
              />
            </div>}
            <div style={{ display: 'grid', gap: 6, padding: '8px 10px', background: '#fafafa', borderRadius: 6 }}>
              <Checkbox checked={selectedNode.data.isEntry} onChange={(event) => event.target.checked && updateNode({ isEntry: true })}>
                作为工作流入口
              </Checkbox>
              <Checkbox
                checked={selectedNode.data.terminateOnSuccess}
                onChange={(event) => updateNode({ terminateOnSuccess: event.target.checked })}
              >
                成功后结束工作流
              </Checkbox>
            </div>
            <Collapse
              size="small"
              ghost
              items={[{
                key: 'node-advanced',
                label: '高级设置',
                children: <div style={{ display: 'grid', gap: 12 }}>
                  {selectedNode.data.nodeType === 'agent' && <div>
                    <Typography.Text type="secondary">模型配置</Typography.Text>
                    <Select
                      allowClear
                      style={{ width: '100%', marginTop: 4 }}
                      value={selectedNode.data.modelProfileId || undefined}
                      placeholder="使用 Agent 模板默认模型"
                      options={modelProfiles.map((profile) => ({
                        value: profile.id,
                        label: `${profile.name} (${profile.config.provider}/${profile.config.name})`,
                      }))}
                      onChange={(modelProfileId) => updateNode({ modelProfileId: modelProfileId || '' })}
                    />
                  </div>}
                  {selectedNode.data.nodeType === 'discovery' && <>
                    <div>
                      <Typography.Text type="secondary">必需技能</Typography.Text>
                      <Select
                        mode="tags"
                        style={{ width: '100%', marginTop: 4 }}
                        value={selectedNode.data.requiredSkills}
                        placeholder="可选"
                        options={Array.from(new Set(agents.flatMap((agent) => agent.skills || []))).map((value) => ({ value }))}
                        onChange={(requiredSkills) => updateNode({ requiredSkills })}
                      />
                    </div>
                    <div>
                      <Typography.Text type="secondary">必需工具</Typography.Text>
                      <Select
                        mode="multiple"
                        style={{ width: '100%', marginTop: 4 }}
                        value={selectedNode.data.requiredTools}
                        placeholder="可选"
                        options={Array.from(new Set(agents.flatMap((agent) => agent.tools || []))).map((value) => ({ value }))}
                        onChange={(requiredTools) => updateNode({ requiredTools })}
                      />
                    </div>
                  </>}
                  {selectedNode.data.nodeType === 'agent' && <>
                    <Checkbox
                      checked={selectedNode.data.fallbackEnabled}
                      onChange={(event) => updateNode({ fallbackEnabled: event.target.checked })}
                    >
                      失败或超时后自动寻找替补 Agent
                    </Checkbox>
                    {selectedNode.data.fallbackEnabled && <div>
                      <Typography.Text type="secondary">替补能力要求</Typography.Text>
                      <Input.TextArea
                        rows={3}
                        value={selectedNode.data.fallbackDescription}
                        placeholder="留空时使用当前节点职责和名称"
                        onChange={(event) => updateNode({ fallbackDescription: event.target.value })}
                        style={{ marginTop: 4 }}
                      />
                    </div>}
                  </>}
                  {(selectedNode.data.nodeType === 'discovery' || selectedNode.data.fallbackEnabled) && <div>
                    <Typography.Text type="secondary">候选 Agent 超时（秒）</Typography.Text>
                    <InputNumber
                      min={0.1}
                      max={86400}
                      value={selectedNode.data.timeoutSeconds}
                      onChange={(value) => updateNode({ timeoutSeconds: Number(value || 300) })}
                      style={{ width: '100%', marginTop: 4 }}
                    />
                  </div>}
                </div>,
              }]}
            />
            <Button danger onClick={deleteSelectedNode}>删除节点</Button>
          </div>
        )}
        {selectedEdge && (
          <div style={{ display: 'grid', gap: 10 }}>
            <div>
              <Typography.Text strong>路由设置</Typography.Text>
              <div style={{ color: '#8c8c8c', fontSize: 12, marginTop: 3 }}>
                选择何时进入下一个节点。
              </div>
            </div>
            <div>
              <Typography.Text type="secondary">执行条件</Typography.Text>
              <Select
                value={selectedRoutePreset}
                options={[
                  { value: 'always', label: '始终执行' },
                  { value: 'success', label: '上游成功时执行' },
                  { value: 'error', label: '上游失败时执行' },
                  { value: 'custom', label: '自定义条件' },
                ]}
                onChange={(preset) => {
                  if (preset === 'always') updateEdgeCondition(null);
                  if (preset === 'success') updateEdgeCondition({ field: 'status', operator: 'eq', value: 'success' });
                  if (preset === 'error') updateEdgeCondition({ field: 'status', operator: 'eq', value: 'error' });
                  if (preset === 'custom') updateEdgeCondition(
                    (selectedEdge.data?.condition as PlatformEdge['condition'])
                    || { field: 'status', operator: 'eq', value: 'success' },
                  );
                }}
                style={{ width: '100%', marginTop: 4 }}
              />
            </div>
            {selectedRoutePreset === 'custom' && <>
              <div>
                <Typography.Text type="secondary">结果字段</Typography.Text>
                <Input
                  aria-label="路由条件字段"
                  value={(selectedEdge.data?.condition as PlatformEdge['condition'])?.field || ''}
                  placeholder="status"
                  onChange={(event) => updateEdgeCondition({
                    ...(selectedEdge.data?.condition as NonNullable<PlatformEdge['condition']>),
                    field: event.target.value,
                  })}
                  style={{ marginTop: 4 }}
                />
              </div>
              <div>
                <Typography.Text type="secondary">判断方式</Typography.Text>
                <Select
                  aria-label="路由条件运算符"
                  value={(selectedEdge.data?.condition as PlatformEdge['condition'])?.operator || 'eq'}
                  options={[
                    { value: 'eq', label: '等于' },
                    { value: 'neq', label: '不等于' },
                    { value: 'contains', label: '包含' },
                    { value: 'gt', label: '大于' },
                    { value: 'lt', label: '小于' },
                  ]}
                  onChange={(operator) => updateEdgeCondition({
                    ...(selectedEdge.data?.condition as NonNullable<PlatformEdge['condition']>),
                    operator,
                  })}
                  style={{ width: '100%', marginTop: 4 }}
                />
              </div>
              <div>
                <Typography.Text type="secondary">目标值</Typography.Text>
                <Input
                  aria-label="路由条件目标值"
                  value={String((selectedEdge.data?.condition as PlatformEdge['condition'])?.value ?? '')}
                  placeholder="success"
                  onChange={(event) => updateEdgeCondition({
                    ...(selectedEdge.data?.condition as NonNullable<PlatformEdge['condition']>),
                    value: event.target.value,
                  })}
                  style={{ marginTop: 4 }}
                />
              </div>
            </>}
            <Collapse
              size="small"
              ghost
              items={[{
                key: 'route-advanced',
                label: '高级设置',
                children: <div style={{ display: 'grid', gap: 10 }}>
                  <Typography.Text type="secondary">
                    默认传递全部上游结果。只有需要缩小上下文时才设置以下字段。
                  </Typography.Text>
                  <div>
                    <Typography.Text type="secondary">传递字段</Typography.Text>
                    <Select
                      aria-label="传递字段"
                      mode="tags"
                      value={(selectedEdge.data?.payloadMapping as PlatformEdge['payload_mapping'])?.include || []}
                      placeholder="全部字段"
                      options={['task', 'content', 'status', 'feedback', 'evidence', 'task_result'].map((value) => ({ value }))}
                      onChange={(include) => updateEdgePayloadMapping({
                        ...((selectedEdge.data?.payloadMapping as PlatformEdge['payload_mapping']) || {}),
                        include,
                      })}
                      style={{ width: '100%', marginTop: 4 }}
                    />
                  </div>
                  <div>
                    <Typography.Text type="secondary">作为下游任务的字段</Typography.Text>
                    <Input
                      aria-label="下游任务来源字段"
                      value={(selectedEdge.data?.payloadMapping as PlatformEdge['payload_mapping'])?.task_field || ''}
                      placeholder="可选，例如 content"
                      onChange={(event) => updateEdgePayloadMapping({
                        ...((selectedEdge.data?.payloadMapping as PlatformEdge['payload_mapping']) || {}),
                        task_field: event.target.value || null,
                      })}
                      style={{ marginTop: 4 }}
                    />
                  </div>
                </div>,
              }]}
            />
            <Button danger onClick={deleteSelectedEdge}>删除路由</Button>
          </div>
        )}
        {!selectedNode && !selectedEdge && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择一个节点或路由后进行设置" />
        )}
      </aside>
      </div>
    </div>
  );
});

export default WorkflowBuilder;
