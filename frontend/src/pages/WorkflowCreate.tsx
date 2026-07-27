import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, Checkbox, Collapse, Form, Input, InputNumber, Select, Space, Spin, Typography, message } from 'antd';
import { ArrowLeftOutlined, SaveOutlined } from '@ant-design/icons';
import WorkflowBuilder from '../components/WorkflowBuilder';
import type { AgentManifest, ModelProfile, PlatformNode, WorkflowBuilderHandle } from '../components/WorkflowBuilder';
import { validateSupervisorConfiguration, validateWorkflowNodes } from '../components/workflowValidation';
import { fetchApi } from '../api/client';

interface WorkflowCreateValues {
  id: string;
  name: string;
  description?: string;
  max_iterations: number;
  timeout: number;
  trigger_type: 'manual' | 'cron';
  trigger_cron?: string;
  trigger_timezone: string;
  trigger_input?: string;
  mode: 'flat' | 'supervisor';
  supervisor_agent_id?: string;
  supervisor_responsibility?: string;
  supervisor_capabilities?: string[];
  supervisor_planning_enabled: boolean;
  supervisor_intervention_on_failure: boolean;
  supervisor_acceptance_criteria?: string[];
  supervisor_require_terminal_candidate: boolean;
  supervisor_require_evidence: boolean;
  supervisor_max_attempts_per_agent: number;
  supervisor_max_repeated_decisions: number;
  supervisor_review_history_limit: number;
  hosting_enabled: boolean;
  hosting_input?: string;
  hosting_max_cycles: number;
  hosting_interval_seconds: number;
  hosting_stop_on_error: boolean;
  hosting_condition_enabled: boolean;
  hosting_condition_field?: string;
  hosting_condition_operator?: 'eq' | 'neq' | 'contains' | 'gt' | 'gte' | 'lt' | 'lte';
  hosting_condition_value?: string;
}

interface WorkflowResponse {
  id: string;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function parseConditionValue(value: string | undefined): unknown {
  const normalized = (value || '').trim();
  if (normalized === 'true') return true;
  if (normalized === 'false') return false;
  if (normalized === 'null') return null;
  if (normalized !== '' && Number.isFinite(Number(normalized))) return Number(normalized);
  return value || '';
}

function completionConditions(nodes: PlatformNode[]) {
  return nodes
    .filter((node) => node.config.terminate_on_success === true)
    .map((node) => ({ agent: node.agent_id || node.id, status: 'success' }));
}

export default function WorkflowCreate() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [graphNodes, setGraphNodes] = useState<PlatformNode[]>([]);
  const [form] = Form.useForm<WorkflowCreateValues>();
  const builderRef = useRef<WorkflowBuilderHandle>(null);
  const triggerType = Form.useWatch('trigger_type', form);
  const mode = Form.useWatch('mode', form);
  const hostingEnabled = Form.useWatch('hosting_enabled', form);
  const hostingConditionEnabled = Form.useWatch('hosting_condition_enabled', form);
  const handleGraphChange = useCallback(
    (graph: { nodes: PlatformNode[] }) => setGraphNodes(graph.nodes),
    [],
  );

  useEffect(() => {
    Promise.all([
      fetchApi<AgentManifest[]>('/api/agents/manifests'),
      fetchApi<ModelProfile[]>('/api/model-profiles'),
    ])
      .then(([manifests, profiles]) => {
        setAgents(manifests);
        setModelProfiles(profiles);
      })
      .catch((error: Error) => message.error(error.message))
      .finally(() => setLoading(false));

    const healthRefresh = window.setInterval(() => {
      fetchApi<AgentManifest[]>('/api/agents/manifests')
        .then(setAgents)
        .catch(() => undefined);
    }, 10_000);
    return () => window.clearInterval(healthRefresh);
  }, []);

  const createWorkflow = async () => {
    try {
      const values = await form.validateFields();
      const graph = builderRef.current?.getGraph();
      if (!graph?.nodes.length) {
        message.error('请至少拖入一个 Agent 到画布中。');
        return;
      }
      const validationError = validateWorkflowNodes(graph.nodes);
      if (validationError) {
        message.error(validationError);
        return;
      }
      const supervisor = values.mode === 'supervisor' ? {
        agent_id: values.supervisor_agent_id || '',
        responsibility: values.supervisor_responsibility || '',
        capabilities: values.supervisor_capabilities || [],
        planning_enabled: values.supervisor_planning_enabled,
        intervention_on_failure: values.supervisor_intervention_on_failure,
        acceptance_criteria: values.supervisor_acceptance_criteria || [],
        require_terminal_candidate: values.supervisor_require_terminal_candidate,
        require_evidence: values.supervisor_require_evidence,
        max_attempts_per_agent: values.supervisor_max_attempts_per_agent,
        max_repeated_decisions: values.supervisor_max_repeated_decisions,
        review_history_limit: values.supervisor_review_history_limit,
      } : null;
      const supervisorError = validateSupervisorConfiguration(
        values.mode,
        supervisor,
        graph.nodes,
      );
      if (supervisorError) {
        message.error(supervisorError);
        return;
      }
      const terminateOn = completionConditions(graph.nodes);
      if (!terminateOn.length) {
        message.error('请至少选择一个成功后结束工作流的 Agent。');
        return;
      }
      setSaving(true);
      const workflow = await fetchApi<WorkflowResponse>('/api/workflows', {
        method: 'POST',
        body: JSON.stringify({
          workflow: {
            id: values.id,
            name: values.name,
            description: values.description || '',
            nodes: graph.nodes,
            edges: graph.edges,
            trigger: {
              type: values.trigger_type,
              cron: values.trigger_type === 'cron' ? values.trigger_cron : null,
              timezone: values.trigger_timezone,
              input: values.trigger_input || '',
            },
            context: {},
            max_iterations: values.max_iterations,
            timeout: values.timeout,
            mode: values.mode,
            terminate_on: terminateOn,
            supervisor,
            hosting: {
              enabled: values.hosting_enabled,
              input: values.hosting_input || '',
              max_cycles: values.hosting_max_cycles,
              interval_seconds: values.hosting_interval_seconds,
              stop_on_error: values.hosting_stop_on_error,
              stop_condition: values.hosting_condition_enabled ? {
                field: values.hosting_condition_field || 'output.stop',
                operator: values.hosting_condition_operator || 'eq',
                value: parseConditionValue(values.hosting_condition_value),
              } : null,
            },
          },
        }),
      });
      message.success('工作流已创建');
      navigate(`/workflows/${workflow.id}`);
    } catch (error: unknown) {
      message.error(errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin size="large" style={{ display: 'block', margin: '100px auto' }} />;

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12, marginBottom: 16 }}>
        <div>
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/workflows')} style={{ marginLeft: -8 }}>
            工作流
          </Button>
          <Typography.Title level={3} style={{ margin: 0 }}>新建工作流</Typography.Title>
        </div>
        <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={createWorkflow}>
          创建工作流
        </Button>
      </div>

      <Form form={form} layout="vertical" initialValues={{
        max_iterations: 10,
        timeout: 3600,
        trigger_type: 'manual',
        trigger_timezone: 'UTC',
        mode: 'flat',
        supervisor_responsibility: '审核每个 Agent 的结果，执行质量门禁，并决定继续、返工、改派或结束。',
        supervisor_capabilities: ['执行规划', '结果审核', '路由控制', '失败恢复'],
        supervisor_planning_enabled: true,
        supervisor_intervention_on_failure: true,
        supervisor_acceptance_criteria: [],
        supervisor_require_terminal_candidate: true,
        supervisor_require_evidence: false,
        supervisor_max_attempts_per_agent: 4,
        supervisor_max_repeated_decisions: 2,
        supervisor_review_history_limit: 20,
        hosting_enabled: false,
        hosting_max_cycles: 100,
        hosting_interval_seconds: 0,
        hosting_stop_on_error: true,
        hosting_condition_enabled: false,
        hosting_condition_field: 'output.stop',
        hosting_condition_operator: 'eq',
        hosting_condition_value: 'true',
      }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 260px), 1fr))', gap: 12 }}>
          <Form.Item name="id" label="工作流 ID" rules={[{ required: true }, { pattern: /^[a-z][a-z0-9-]{2,63}$/, message: '请使用小写字母、数字和连字符，并以字母开头。' }]}>
            <Input placeholder="content-review-flow" />
          </Form.Item>
          <Form.Item name="name" label="工作流名称" rules={[{ required: true }]}>
            <Input placeholder="内容审核工作流" />
          </Form.Item>
        </div>
        <Form.Item name="description" label="工作流说明">
          <Input.TextArea rows={2} placeholder="简要说明这个工作流要完成什么。" />
        </Form.Item>
        <Collapse
          size="small"
          style={{ marginBottom: 16 }}
          items={[{
            key: 'advanced',
            label: '运行与编排设置',
            children: <>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 220px), 1fr))', gap: 12 }}>
                <Form.Item name="max_iterations" label="最大执行步数" rules={[{ required: true }]}>
                  <InputNumber min={1} max={1000} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="timeout" label="超时时间（秒）" rules={[{ required: true }]}>
                  <InputNumber min={1} max={86400} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="trigger_type" label="触发方式" rules={[{ required: true }]}>
                  <Select options={[
                    { value: 'manual', label: '仅手动运行' },
                    { value: 'cron', label: '定时运行（Cron）' },
                  ]} />
                </Form.Item>
                <Form.Item name="mode" label="编排模式" rules={[{ required: true }]}>
                  <Select options={[
                    { value: 'flat', label: '标准模式 · 按固定路由执行' },
                    { value: 'supervisor', label: 'Supervisor 模式 · 审核每一步结果' },
                  ]} />
                </Form.Item>
              </div>
              {triggerType === 'cron' && <>
                <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 12 }}>
                  <Form.Item
                    name="trigger_cron"
                    label="Cron 表达式"
                    extra="使用五段式 Cron。上一次定时运行未结束时不会重复启动。"
                    rules={[{ required: true }]}
                  >
                    <Input placeholder="0 * * * *" />
                  </Form.Item>
                  <Form.Item name="trigger_timezone" label="时区" rules={[{ required: true }]}>
                    <Input placeholder="Asia/Shanghai" />
                  </Form.Item>
                </div>
                <Form.Item name="trigger_input" label="定时运行输入">
                  <Input.TextArea rows={2} placeholder="每次定时运行时发送给工作流的任务" />
                </Form.Item>
              </>}
              <div style={{ border: '1px solid #d9d9d9', borderRadius: 8, padding: 16, marginBottom: 16 }}>
                <Form.Item name="hosting_enabled" valuePropName="checked" style={{ marginBottom: hostingEnabled ? 16 : 0 }}>
                  <Checkbox>启用托管模式</Checkbox>
                </Form.Item>
                {hostingEnabled && <>
                  <Typography.Paragraph type="secondary">
                    创建后可在工作流详情页启动托管；每轮结束后自动开始下一轮。
                  </Typography.Paragraph>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 220px), 1fr))', gap: 12 }}>
                    <Form.Item name="hosting_max_cycles" label="最大循环次数" rules={[{ required: true }]}>
                      <InputNumber min={1} max={1_000_000} style={{ width: '100%' }} />
                    </Form.Item>
                    <Form.Item name="hosting_interval_seconds" label="轮次间隔（秒）" rules={[{ required: true }]}>
                      <InputNumber min={0} max={86_400} style={{ width: '100%' }} />
                    </Form.Item>
                  </div>
                  <Form.Item name="hosting_input" label="每轮运行输入">
                    <Input.TextArea rows={2} placeholder="每一轮发送给工作流的任务" />
                  </Form.Item>
                  <Space size="large" wrap>
                    <Form.Item name="hosting_stop_on_error" valuePropName="checked">
                      <Checkbox>任一轮异常时停止</Checkbox>
                    </Form.Item>
                    <Form.Item name="hosting_condition_enabled" valuePropName="checked">
                      <Checkbox>启用结果终止条件</Checkbox>
                    </Form.Item>
                  </Space>
                  {hostingConditionEnabled && <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr', gap: 12 }}>
                    <Form.Item name="hosting_condition_field" label="结果字段路径" rules={[{ required: true }]}>
                      <Input placeholder="output.done" />
                    </Form.Item>
                    <Form.Item name="hosting_condition_operator" label="判断方式" rules={[{ required: true }]}>
                      <Select options={[
                        { value: 'eq', label: '等于' },
                        { value: 'neq', label: '不等于' },
                        { value: 'contains', label: '包含' },
                        { value: 'gt', label: '大于' },
                        { value: 'gte', label: '大于等于' },
                        { value: 'lt', label: '小于' },
                        { value: 'lte', label: '小于等于' },
                      ]} />
                    </Form.Item>
                    <Form.Item name="hosting_condition_value" label="目标值" rules={[{ required: true }]}>
                      <Input placeholder="true" />
                    </Form.Item>
                  </div>}
                </>}
              </div>
              {mode === 'supervisor' && <div style={{ border: '1px solid #d9d9d9', borderRadius: 8, padding: 16 }}>
                <Typography.Title level={5} style={{ marginTop: 0 }}>Supervisor 控制</Typography.Title>
                <Typography.Paragraph type="secondary">
                  Supervisor 会审核每个节点的完整结果，并决定继续、改派、返工或结束。
                </Typography.Paragraph>
                <Form.Item name="supervisor_agent_id" label="Supervisor 节点" rules={[{ required: true }]}>
                  <Select
                    placeholder="先在画布添加具体 Agent，再在此选择"
                    options={graphNodes
                      .filter((node) => node.node_type !== 'discovery')
                      .map((node) => ({ value: node.id, label: `${node.label} · ${node.agent_id}` }))}
                  />
                </Form.Item>
                <Form.Item name="supervisor_responsibility" label="Supervisor 职责" rules={[{ required: true }]}>
                  <Input.TextArea rows={3} placeholder="审核结果、执行质量门禁，并决定继续、返工、改派或结束。" />
                </Form.Item>
                <Form.Item name="supervisor_capabilities" label="Supervisor 能力" rules={[{ required: true }]}>
                  <Select mode="tags" placeholder="执行规划、质量审核、失败恢复" />
                </Form.Item>
                <Form.Item
                  name="supervisor_acceptance_criteria"
                  label="完成验收标准"
                  extra="逐项填写可验证的完成条件；Supervisor 未确认全部通过时不能结束。"
                >
                  <Select mode="tags" placeholder="例如：测试全部通过、产物路径可访问" />
                </Form.Item>
                <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
                  <Form.Item name="supervisor_planning_enabled" valuePropName="checked">
                    <Checkbox>开始时创建执行计划</Checkbox>
                  </Form.Item>
                  <Form.Item name="supervisor_intervention_on_failure" valuePropName="checked">
                    <Checkbox>Agent 失败时自动介入</Checkbox>
                  </Form.Item>
                  <Form.Item name="supervisor_require_terminal_candidate" valuePropName="checked">
                    <Checkbox>命中终止节点后才允许完成</Checkbox>
                  </Form.Item>
                  <Form.Item name="supervisor_require_evidence" valuePropName="checked">
                    <Checkbox>要求 Agent 返回结构化证据</Checkbox>
                  </Form.Item>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 180px), 1fr))', gap: 12 }}>
                  <Form.Item name="supervisor_max_attempts_per_agent" label="单 Agent 最大执行次数" rules={[{ required: true }]}>
                    <InputNumber min={1} max={100} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="supervisor_max_repeated_decisions" label="相同决策最大连续次数" rules={[{ required: true }]}>
                    <InputNumber min={1} max={20} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="supervisor_review_history_limit" label="复核历史步数" rules={[{ required: true }]}>
                    <InputNumber min={1} max={200} style={{ width: '100%' }} />
                  </Form.Item>
                </div>
              </div>}
            </>,
          }]}
        />
      </Form>

      <WorkflowBuilder
        initialNodes={[]}
        initialEdges={[]}
        agents={agents}
        modelProfiles={modelProfiles}
        onGraphChange={handleGraphChange}
        ref={builderRef}
      />
    </>
  );
}
