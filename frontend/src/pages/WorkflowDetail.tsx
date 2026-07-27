import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Button, Card, Checkbox, Col, Input, InputNumber, Modal, Row, Select, Space, Spin, Switch, Table, Tag, Typography, message } from 'antd';
import { CloudServerOutlined, PauseCircleOutlined, PlayCircleOutlined, SaveOutlined } from '@ant-design/icons';
import WorkflowBuilder from '../components/WorkflowBuilder';
import type {
  AgentManifest,
  ModelProfile,
  PlatformEdge,
  PlatformNode,
  WorkflowBuilderHandle,
} from '../components/WorkflowBuilder';
import { validateSupervisorConfiguration, validateWorkflowNodes } from '../components/workflowValidation';
import { fetchApi } from '../api/client';

interface Workflow {
  id: string;
  name: string;
  description: string;
  nodes: PlatformNode[];
  edges: PlatformEdge[];
  trigger: {
    type: string;
    cron?: string | null;
    timezone?: string;
    input?: string;
  };
  context: Record<string, unknown>;
  max_iterations: number;
  timeout: number;
  mode: 'flat' | 'supervisor';
  terminate_on: Array<Record<string, unknown>>;
  supervisor?: SupervisorSettings | null;
  hosting: HostingSettings;
}

interface HostingStopCondition {
  field: string;
  operator: 'eq' | 'neq' | 'contains' | 'gt' | 'gte' | 'lt' | 'lte';
  value: unknown;
}

interface HostingSettings {
  enabled: boolean;
  input: string;
  max_cycles: number;
  interval_seconds: number;
  stop_on_error: boolean;
  stop_condition?: HostingStopCondition | null;
}

interface HostingState {
  workflow_id: string;
  status: 'idle' | 'running' | 'stopping' | 'stopped' | 'condition_met' | 'limit_reached' | 'error';
  completed_cycles: number;
  current_cycle?: number | null;
  last_run_id?: string | null;
  last_run_status?: string | null;
  last_error?: string | null;
}

interface SupervisorSettings extends Record<string, unknown> {
  agent_id: string;
  responsibility: string;
  capabilities: string[];
  planning_enabled: boolean;
  intervention_on_failure: boolean;
}

interface WorkflowRunRecord {
  run_id: string;
  started_at: string;
  status: string;
  result?: {
    iterations?: number;
    duration_seconds?: number;
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const statusColor: Record<string, string> = {
  completed: 'green',
  error: 'red',
  timeout: 'orange',
  max_iterations_reached: 'orange',
  running: 'blue',
};
const statusText: Record<string, string> = {
  completed: '已完成',
  error: '失败',
  timeout: '超时',
  max_iterations_reached: '达到最大步数',
  running: '运行中',
};
const hostingStatusPresentation: Record<HostingState['status'], { text: string; color: string }> = {
  idle: { text: '未启动', color: 'default' },
  running: { text: '托管运行中', color: 'processing' },
  stopping: { text: '正在停止', color: 'orange' },
  stopped: { text: '已手动停止', color: 'default' },
  condition_met: { text: '已满足终止条件', color: 'green' },
  limit_reached: { text: '已达到循环上限', color: 'gold' },
  error: { text: '因异常停止', color: 'red' },
};

function parseConditionValue(value: string): unknown {
  const normalized = value.trim();
  if (normalized === 'true') return true;
  if (normalized === 'false') return false;
  if (normalized === 'null') return null;
  if (normalized !== '' && Number.isFinite(Number(normalized))) return Number(normalized);
  return value;
}

export default function WorkflowDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([]);
  const [runs, setRuns] = useState<WorkflowRunRecord[]>([]);
  const [graphNodes, setGraphNodes] = useState<PlatformNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [runModalOpen, setRunModalOpen] = useState(false);
  const [runInput, setRunInput] = useState('');
  const [hostingState, setHostingState] = useState<HostingState | null>(null);
  const [hostingAction, setHostingAction] = useState(false);
  const builderRef = useRef<WorkflowBuilderHandle>(null);
  const handleGraphChange = useCallback(
    (graph: { nodes: PlatformNode[] }) => setGraphNodes(graph.nodes),
    [],
  );

  useEffect(() => {
    if (!id) return;
    Promise.all([
      fetchApi<Workflow>(`/api/workflows/${id}`),
      fetchApi<AgentManifest[]>('/api/agents/manifests'),
      fetchApi<ModelProfile[]>('/api/model-profiles'),
      fetchApi<WorkflowRunRecord[]>(`/api/workflows/${id}/runs`),
      fetchApi<HostingState>(`/api/workflows/${id}/hosting`),
    ])
      .then(([definition, manifests, profiles, history, hostedState]) => {
        setWorkflow(definition);
        setGraphNodes(definition.nodes);
        setAgents(manifests);
        setModelProfiles(profiles);
        setRuns(history);
        setHostingState(hostedState);
      })
      .catch((error) => message.error(error.message))
      .finally(() => setLoading(false));

    const healthRefresh = window.setInterval(() => {
      fetchApi<AgentManifest[]>('/api/agents/manifests')
        .then(setAgents)
        .catch(() => undefined);
    }, 10_000);
    const hostingRefresh = window.setInterval(() => {
      fetchApi<HostingState>(`/api/workflows/${id}/hosting`)
        .then((state) => {
          setHostingState((current) => {
            if (current?.completed_cycles !== state.completed_cycles) {
              void fetchApi<WorkflowRunRecord[]>(`/api/workflows/${id}/runs`).then(setRuns);
            }
            return state;
          });
        })
        .catch(() => undefined);
    }, 2_000);
    return () => {
      window.clearInterval(healthRefresh);
      window.clearInterval(hostingRefresh);
    };
  }, [id]);

  const handleSave = async (): Promise<boolean> => {
    if (!workflow || !id) return false;
    setSaving(true);
    try {
      const graph = builderRef.current?.getGraph();
      const validationError = graph ? validateWorkflowNodes(graph.nodes) : null;
      if (validationError) {
        message.error(validationError);
        return false;
      }
      const supervisorError = validateSupervisorConfiguration(
        workflow.mode,
        workflow.supervisor,
        graph?.nodes || workflow.nodes,
      );
      if (supervisorError) {
        message.error(supervisorError);
        return false;
      }
      const saved = await fetchApi<Workflow>(`/api/workflows/${id}`, {
        method: 'PUT',
        body: JSON.stringify({ workflow: { ...workflow, ...graph } }),
      });
      setWorkflow(saved);
      message.success('工作流已保存');
      return true;
    } catch (error: unknown) {
      message.error(errorMessage(error));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const handleStartHosting = async () => {
    if (!id || !workflow || !workflow.hosting.enabled) return;
    const saved = await handleSave();
    if (!saved) return;
    setHostingAction(true);
    try {
      const state = await fetchApi<HostingState>(`/api/workflows/${id}/hosting/start`, {
        method: 'POST',
        body: JSON.stringify({}),
      });
      setHostingState(state);
      message.success('托管运行已启动');
    } catch (error: unknown) {
      message.error(errorMessage(error));
    } finally {
      setHostingAction(false);
    }
  };

  const handleStopHosting = async () => {
    if (!id) return;
    setHostingAction(true);
    try {
      const state = await fetchApi<HostingState>(`/api/workflows/${id}/hosting/stop`, {
        method: 'POST',
        body: JSON.stringify({}),
      });
      setHostingState(state);
      message.success(state.status === 'stopping' ? '将在当前轮结束后停止' : '托管运行已停止');
    } catch (error: unknown) {
      message.error(errorMessage(error));
    } finally {
      setHostingAction(false);
    }
  };

  const handleRun = async () => {
    if (!id) return;
    try {
      const response = await fetchApi<{ run_id: string }>(`/api/workflows/${id}/run`, {
        method: 'POST',
        body: JSON.stringify({ input: runInput }),
      });
      setRunModalOpen(false);
      navigate(`/workflows/${id}/runs/${response.run_id}`);
    } catch (error: unknown) {
      message.error(errorMessage(error));
    }
  };

  if (loading || !workflow) return <Spin size="large" style={{ display: 'block', margin: '100px auto' }} />;

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>{workflow.name}</Typography.Title>
          <Typography.Text type="secondary">
            {workflow.id} · {workflow.nodes.length} 个 Agent · {workflow.mode === 'supervisor' ? 'Supervisor 模式' : '标准模式'} · {workflow.trigger.type === 'cron' ? `定时 ${workflow.trigger.cron}` : '手动运行'}
          </Typography.Text>
        </div>
        <Space>
          <Button icon={<SaveOutlined />} loading={saving} onClick={() => void handleSave()}>保存</Button>
          <Button type="primary" icon={<PlayCircleOutlined />} onClick={() => setRunModalOpen(true)}>运行</Button>
        </Space>
      </div>

      <Card size="small" title="运行与编排设置" style={{ marginBottom: 16 }}>
        <Row gutter={[12, 12]}>
          <Col xs={24} md={8}>
            <Typography.Text type="secondary">编排模式</Typography.Text>
            <Select
              aria-label="编排模式"
              value={workflow.mode}
              options={[
                { value: 'flat', label: '标准模式 · 按固定路由执行' },
                { value: 'supervisor', label: 'Supervisor 模式 · 审核每一步结果' },
              ]}
              onChange={(mode: 'flat' | 'supervisor') => setWorkflow({
                ...workflow,
                mode,
                supervisor: mode === 'supervisor'
                  ? workflow.supervisor || {
                    agent_id: '',
                    responsibility: '',
                    capabilities: [],
                    planning_enabled: true,
                    intervention_on_failure: true,
                  }
                  : null,
              })}
              style={{ width: '100%', marginTop: 4 }}
            />
          </Col>
          <Col xs={24} md={8}>
            <Typography.Text type="secondary">触发方式</Typography.Text>
            <Select
              aria-label="触发方式"
              value={workflow.trigger.type}
              options={[
                { value: 'manual', label: '仅手动运行' },
                { value: 'cron', label: '定时运行（Cron）' },
              ]}
              onChange={(type) => setWorkflow({
                ...workflow,
                trigger: {
                  ...workflow.trigger,
                  type,
                  cron: type === 'cron' ? workflow.trigger.cron || '0 * * * *' : null,
                  timezone: workflow.trigger.timezone || 'UTC',
                },
              })}
              style={{ width: '100%', marginTop: 4 }}
            />
          </Col>
          <Col xs={12} md={8}>
            <Typography.Text type="secondary">最大执行步数</Typography.Text>
            <InputNumber
              min={1}
              max={1000}
              value={workflow.max_iterations}
              onChange={(value) => setWorkflow({ ...workflow, max_iterations: Number(value || 10) })}
              style={{ width: '100%', marginTop: 4 }}
            />
          </Col>
          <Col xs={12} md={8}>
            <Typography.Text type="secondary">超时时间（秒）</Typography.Text>
            <InputNumber
              min={1}
              max={86400}
              value={workflow.timeout}
              onChange={(value) => setWorkflow({ ...workflow, timeout: Number(value || 3600) })}
              style={{ width: '100%', marginTop: 4 }}
            />
          </Col>
          {workflow.trigger.type === 'cron' && <>
            <Col xs={24} md={16}>
              <Typography.Text type="secondary">Cron 表达式</Typography.Text>
              <Input
                aria-label="Cron 表达式"
                value={workflow.trigger.cron || ''}
                placeholder="0 * * * *"
                onChange={(event) => setWorkflow({
                  ...workflow,
                  trigger: { ...workflow.trigger, cron: event.target.value },
                })}
                style={{ marginTop: 4 }}
              />
            </Col>
            <Col xs={24} md={8}>
              <Typography.Text type="secondary">时区</Typography.Text>
              <Input
                aria-label="Cron 时区"
                value={workflow.trigger.timezone || 'UTC'}
                placeholder="UTC"
                onChange={(event) => setWorkflow({
                  ...workflow,
                  trigger: { ...workflow.trigger, timezone: event.target.value },
                })}
                style={{ marginTop: 4 }}
              />
            </Col>
            <Col span={24}>
              <Typography.Text type="secondary">定时运行输入</Typography.Text>
              <Input.TextArea
                aria-label="定时运行输入"
                rows={2}
                value={workflow.trigger.input || ''}
                placeholder="每次定时运行时发送给工作流的任务"
                onChange={(event) => setWorkflow({
                  ...workflow,
                  trigger: { ...workflow.trigger, input: event.target.value },
                })}
                style={{ marginTop: 4 }}
              />
              <Typography.Text type="secondary">
                Cron 使用所选时区；上一次定时运行尚未结束时不会重复启动。
              </Typography.Text>
            </Col>
          </>}
        </Row>
      </Card>

      <Card
        size="small"
        title={<Space><CloudServerOutlined />托管运行</Space>}
        extra={hostingState && (
          <Tag color={hostingStatusPresentation[hostingState.status].color}>
            {hostingStatusPresentation[hostingState.status].text}
          </Tag>
        )}
        style={{ marginBottom: 16 }}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Switch
              aria-label="启用托管模式"
              checked={workflow.hosting.enabled}
              onChange={(enabled) => setWorkflow({
                ...workflow,
                hosting: { ...workflow.hosting, enabled },
              })}
            />
            <Typography.Text strong>启用托管模式</Typography.Text>
            <Typography.Text type="secondary">
              每轮完成后自动开始下一轮，直到达到循环上限、满足终止条件、出现异常或手动停止。
            </Typography.Text>
          </Space>
          {workflow.hosting.enabled && <>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={6}>
                <Typography.Text type="secondary">最大循环次数</Typography.Text>
                <InputNumber
                  aria-label="托管最大循环次数"
                  min={1}
                  max={1_000_000}
                  value={workflow.hosting.max_cycles}
                  onChange={(value) => setWorkflow({
                    ...workflow,
                    hosting: { ...workflow.hosting, max_cycles: Number(value || 100) },
                  })}
                  style={{ width: '100%', marginTop: 4 }}
                />
              </Col>
              <Col xs={12} md={6}>
                <Typography.Text type="secondary">轮次间隔（秒）</Typography.Text>
                <InputNumber
                  aria-label="托管轮次间隔"
                  min={0}
                  max={86_400}
                  value={workflow.hosting.interval_seconds}
                  onChange={(value) => setWorkflow({
                    ...workflow,
                    hosting: { ...workflow.hosting, interval_seconds: Number(value || 0) },
                  })}
                  style={{ width: '100%', marginTop: 4 }}
                />
              </Col>
              <Col xs={24} md={12}>
                <Typography.Text type="secondary">每轮运行输入</Typography.Text>
                <Input
                  aria-label="托管运行输入"
                  value={workflow.hosting.input}
                  placeholder="每一轮发送给工作流的任务"
                  onChange={(event) => setWorkflow({
                    ...workflow,
                    hosting: { ...workflow.hosting, input: event.target.value },
                  })}
                  style={{ marginTop: 4 }}
                />
              </Col>
            </Row>
            <Space size="large" wrap>
              <Checkbox
                checked={workflow.hosting.stop_on_error}
                onChange={(event) => setWorkflow({
                  ...workflow,
                  hosting: { ...workflow.hosting, stop_on_error: event.target.checked },
                })}
              >
                任一轮异常时停止
              </Checkbox>
              <Checkbox
                checked={Boolean(workflow.hosting.stop_condition)}
                onChange={(event) => setWorkflow({
                  ...workflow,
                  hosting: {
                    ...workflow.hosting,
                    stop_condition: event.target.checked
                      ? { field: 'output.stop', operator: 'eq', value: true }
                      : null,
                  },
                })}
              >
                启用结果终止条件
              </Checkbox>
            </Space>
            {workflow.hosting.stop_condition && <Row gutter={[12, 12]}>
              <Col xs={24} md={10}>
                <Typography.Text type="secondary">结果字段路径</Typography.Text>
                <Input
                  aria-label="托管终止字段"
                  value={workflow.hosting.stop_condition.field}
                  placeholder="例如 output.done"
                  onChange={(event) => setWorkflow({
                    ...workflow,
                    hosting: {
                      ...workflow.hosting,
                      stop_condition: {
                        ...workflow.hosting.stop_condition!,
                        field: event.target.value,
                      },
                    },
                  })}
                  style={{ marginTop: 4 }}
                />
              </Col>
              <Col xs={12} md={6}>
                <Typography.Text type="secondary">判断方式</Typography.Text>
                <Select
                  aria-label="托管终止运算符"
                  value={workflow.hosting.stop_condition.operator}
                  options={[
                    { value: 'eq', label: '等于' },
                    { value: 'neq', label: '不等于' },
                    { value: 'contains', label: '包含' },
                    { value: 'gt', label: '大于' },
                    { value: 'gte', label: '大于等于' },
                    { value: 'lt', label: '小于' },
                    { value: 'lte', label: '小于等于' },
                  ]}
                  onChange={(operator: HostingStopCondition['operator']) => setWorkflow({
                    ...workflow,
                    hosting: {
                      ...workflow.hosting,
                      stop_condition: { ...workflow.hosting.stop_condition!, operator },
                    },
                  })}
                  style={{ width: '100%', marginTop: 4 }}
                />
              </Col>
              <Col xs={12} md={8}>
                <Typography.Text type="secondary">目标值</Typography.Text>
                <Input
                  aria-label="托管终止目标值"
                  value={String(workflow.hosting.stop_condition.value ?? '')}
                  placeholder="true"
                  onChange={(event) => setWorkflow({
                    ...workflow,
                    hosting: {
                      ...workflow.hosting,
                      stop_condition: {
                        ...workflow.hosting.stop_condition!,
                        value: parseConditionValue(event.target.value),
                      },
                    },
                  })}
                  style={{ marginTop: 4 }}
                />
              </Col>
            </Row>}
            <Space wrap>
              {hostingState?.status === 'running' || hostingState?.status === 'stopping' ? (
                <Button
                  danger
                  icon={<PauseCircleOutlined />}
                  loading={hostingAction}
                  disabled={hostingState.status === 'stopping'}
                  onClick={() => void handleStopHosting()}
                >
                  {hostingState.status === 'stopping' ? '正在停止' : '停止托管'}
                </Button>
              ) : (
                <Button
                  type="primary"
                  icon={<PlayCircleOutlined />}
                  loading={hostingAction || saving}
                  onClick={() => void handleStartHosting()}
                >
                  启动托管
                </Button>
              )}
              {hostingState && hostingState.completed_cycles > 0 && (
                <Typography.Text type="secondary">
                  已完成 {hostingState.completed_cycles} / {workflow.hosting.max_cycles} 轮
                  {hostingState.last_run_status ? ` · 最近一轮：${statusText[hostingState.last_run_status] || hostingState.last_run_status}` : ''}
                </Typography.Text>
              )}
            </Space>
            {hostingState?.last_error && (
              <Typography.Text type="danger">停止原因：{hostingState.last_error}</Typography.Text>
            )}
          </>}
        </Space>
      </Card>

      {workflow.mode === 'supervisor' && <Card size="small" title="Supervisor 控制" style={{ marginBottom: 16 }}>
        <Typography.Paragraph type="secondary">
          Supervisor 会接收每个节点的完整结果和交互历史，并决定继续、返工、改派或结束。
        </Typography.Paragraph>
        <Row gutter={[12, 12]}>
          <Col xs={24} md={8}>
            <Typography.Text type="secondary">Supervisor 节点</Typography.Text>
            <Select
              aria-label="Supervisor 节点"
              value={workflow.supervisor?.agent_id || undefined}
              placeholder="选择具体 Agent 节点"
              options={graphNodes
                .filter((node) => node.node_type !== 'discovery')
                .map((node) => ({ value: node.id, label: `${node.label} · ${node.agent_id}` }))}
              onChange={(agent_id) => setWorkflow({
                ...workflow,
                supervisor: { ...workflow.supervisor!, agent_id },
              })}
              style={{ width: '100%', marginTop: 4 }}
            />
          </Col>
          <Col xs={24} md={16}>
            <Typography.Text type="secondary">Supervisor 能力</Typography.Text>
            <Select
              aria-label="Supervisor 能力"
              mode="tags"
              value={workflow.supervisor?.capabilities || []}
              placeholder="执行规划、质量审核、失败恢复"
              onChange={(capabilities) => setWorkflow({
                ...workflow,
                supervisor: { ...workflow.supervisor!, capabilities },
              })}
              style={{ width: '100%', marginTop: 4 }}
            />
          </Col>
          <Col span={24}>
            <Typography.Text type="secondary">Supervisor 职责</Typography.Text>
            <Input.TextArea
              aria-label="Supervisor 职责"
              rows={3}
              value={workflow.supervisor?.responsibility || ''}
              placeholder="审核结果、执行质量门禁，并决定继续、返工、改派或结束。"
              onChange={(event) => setWorkflow({
                ...workflow,
                supervisor: { ...workflow.supervisor!, responsibility: event.target.value },
              })}
              style={{ marginTop: 4 }}
            />
          </Col>
          <Col span={24}>
            <Space size="large" wrap>
              <Checkbox
                checked={workflow.supervisor?.planning_enabled ?? true}
                onChange={(event) => setWorkflow({
                  ...workflow,
                  supervisor: { ...workflow.supervisor!, planning_enabled: event.target.checked },
                })}
              >
                开始时创建执行计划
              </Checkbox>
              <Checkbox
                checked={workflow.supervisor?.intervention_on_failure ?? true}
                onChange={(event) => setWorkflow({
                  ...workflow,
                  supervisor: {
                    ...workflow.supervisor!,
                    intervention_on_failure: event.target.checked,
                  },
                })}
              >
                Agent 失败时自动介入
              </Checkbox>
            </Space>
          </Col>
        </Row>
      </Card>}

      <WorkflowBuilder
        initialNodes={workflow.nodes}
        initialEdges={workflow.edges}
        agents={agents}
        modelProfiles={modelProfiles}
        onGraphChange={handleGraphChange}
        ref={builderRef}
      />

      <Typography.Title level={5} style={{ marginTop: 24 }}>运行记录</Typography.Title>
      <Table
        size="small"
        dataSource={runs}
        rowKey="run_id"
        pagination={false}
        columns={[
          { title: '运行 ID', dataIndex: 'run_id', key: 'run_id' },
          { title: '开始时间', dataIndex: 'started_at', key: 'started_at', render: (value: string) => new Date(value).toLocaleString() },
          { title: '状态', dataIndex: 'status', key: 'status', render: (value: string) => <Tag color={statusColor[value] || 'default'}>{statusText[value] || value}</Tag> },
          { title: '执行步数', key: 'iterations', render: (_: unknown, run: WorkflowRunRecord) => run.result?.iterations ?? '-' },
          { title: '耗时', key: 'duration', render: (_: unknown, run: WorkflowRunRecord) => run.result?.duration_seconds ? `${run.result.duration_seconds} 秒` : '-' },
          { title: '操作', key: 'action', render: (_: unknown, run: WorkflowRunRecord) => <Button size="small" onClick={() => navigate(`/workflows/${id}/runs/${run.run_id}`)}>查看</Button> },
        ]}
      />

      <Modal title="运行工作流" open={runModalOpen} onOk={handleRun} onCancel={() => setRunModalOpen(false)} okText="运行" cancelText="取消">
        <Input.TextArea value={runInput} onChange={(event) => setRunInput(event.target.value)} rows={5} placeholder="描述本次要交给工作流的任务" />
      </Modal>
    </>
  );
}
