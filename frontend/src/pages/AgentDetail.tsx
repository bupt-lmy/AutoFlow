import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert, Typography, Tabs, Button, message, Spin, Descriptions, Tag, Form, Input, InputNumber, Select } from 'antd';
import { ArrowLeftOutlined, SaveOutlined } from '@ant-design/icons';
import YamlEditor from '../components/YamlEditor';
import { fetchApi } from '../api/client';
import Editor from '@monaco-editor/react';

interface AgentDetailData {
  id: string;
  name?: string;
  role?: string;
  raw_yaml?: string;
  persona?: { soul?: string; user?: string; workflow?: string };
  tools?: string[];
  skills?: string[];
  model?: Record<string, unknown> & { name?: string };
  max_concurrent?: number;
  runtime?: {
    state: string;
    max_concurrent: number;
    active_tasks: number;
    queued_tasks: number;
    active_workflows: string[];
  } | null;
  [key: string]: unknown;
}

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error);

export default function AgentDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [agent, setAgent] = useState<AgentDetailData | null>(null);
  const [yaml, setYaml] = useState('');
  const [persona, setPersona] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [credentials, setCredentials] = useState<Array<{ id: string; name: string; masked_value?: string }>>([]);
  const [providers, setProviders] = useState<Array<{ id: string; label: string }>>([]);
  const [availableTools, setAvailableTools] = useState<Array<{ id: string; description: string }>>([]);
  const [availableSkills, setAvailableSkills] = useState<Array<{
    id: string;
    has_scripts: boolean;
    has_references?: boolean;
    has_assets?: boolean;
    file_count?: number;
  }>>([]);
  const [modelForm] = Form.useForm();
  const [capabilitiesForm] = Form.useForm();
  const [concurrencyForm] = Form.useForm();

  useEffect(() => {
    if (!id) return;
    fetchApi<AgentDetailData>(`/api/agents/${id}`)
      .then((a) => {
        setAgent(a);
        setYaml(a.raw_yaml || JSON.stringify(a, null, 2));
        setPersona({
          'soul.md': a.persona?.soul || '',
          'user.md': a.persona?.user || '',
          'workflow.md': a.persona?.workflow || '',
        });
        capabilitiesForm.setFieldsValue({ tools: a.tools || [], skills: a.skills || [] });
        concurrencyForm.setFieldsValue({ max_concurrent: a.max_concurrent || 1 });
      })
      .catch((error: Error) => message.error(error.message))
      .finally(() => setLoading(false));
  }, [capabilitiesForm, concurrencyForm, id]);

  useEffect(() => {
    Promise.all([
      fetchApi<Array<{ id: string; name: string; masked_value?: string }>>('/api/credentials'),
      fetchApi<Array<{ id: string; label: string }>>('/api/credentials/catalog/providers'),
      fetchApi<Array<{ id: string; description: string }>>('/api/agents/catalog/tools'),
      fetchApi<Array<{
        id: string;
        has_scripts: boolean;
        has_references?: boolean;
        has_assets?: boolean;
        file_count?: number;
      }>>('/api/agents/catalog/skills'),
    ]).then(([credentialData, providerData, toolData, skillData]) => {
      setCredentials(credentialData);
      setProviders(providerData);
      setAvailableTools(toolData);
      setAvailableSkills(skillData);
    }).catch((error: Error) => message.error(error.message));
  }, []);

  const handleSaveConfig = async () => {
    try {
      await fetchApi(`/api/agents/${id}`, {
        method: 'PUT',
        body: JSON.stringify({ yaml_content: yaml }),
      });
      message.success('Agent 配置已保存');
    } catch (error: unknown) {
      message.error(errorMessage(error));
    }
  };

  const handleSavePersona = async (fileName: string) => {
    try {
      await fetchApi(`/api/agents/${id}/persona/${fileName}`, {
        method: 'PUT',
        body: JSON.stringify({ content: persona[fileName] }),
      });
      message.success(`${fileName} 已保存`);
    } catch (error: unknown) {
      message.error(errorMessage(error));
    }
  };

  const handleSaveModel = async () => {
    try {
      const model = await modelForm.validateFields();
      await fetchApi(`/api/agents/${id}/model`, {
        method: 'PUT',
        body: JSON.stringify({ model }),
      });
      setAgent((current) => current ? ({ ...current, model }) : current);
      message.success('模型设置将在后续运行中生效');
    } catch (error: unknown) {
      message.error(errorMessage(error));
    }
  };

  const handleSaveCapabilities = async () => {
    try {
      const capabilities = await capabilitiesForm.validateFields();
      const saved = await fetchApi<{ tools: string[]; skills: string[] }>(`/api/agents/${id}/capabilities`, {
        method: 'PUT',
        body: JSON.stringify(capabilities),
      });
      setAgent((current) => current ? ({ ...current, ...saved }) : current);
      message.success('工具和技能已保存');
    } catch (error: unknown) {
      message.error(errorMessage(error));
    }
  };

  const handleSaveConcurrency = async () => {
    try {
      const values = await concurrencyForm.validateFields();
      const saved = await fetchApi<{ runtime: AgentDetailData['runtime'] }>(
        `/api/agents/${id}/concurrency`,
        {
          method: 'PUT',
          body: JSON.stringify(values),
        },
      );
      setAgent((current) => current ? ({
        ...current,
        max_concurrent: values.max_concurrent,
        runtime: saved.runtime,
      }) : current);
      message.success('并发上限已立即生效');
    } catch (error: unknown) {
      message.error(errorMessage(error));
    }
  };

  if (loading) return <Spin size="large" style={{ display: 'block', margin: '100px auto' }} />;

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/agents')} aria-label="返回 Agent 列表" />
        <Typography.Title level={3} style={{ margin: 0 }}>Agent：{agent?.name || id}</Typography.Title>
      </div>

      <Descriptions bordered size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="ID">{agent?.id}</Descriptions.Item>
        <Descriptions.Item label="职责">{agent?.role}</Descriptions.Item>
        <Descriptions.Item label="模型">
          <Tag>{agent?.model?.name || '默认'}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="工具">
          {agent?.tools?.map((t: string) => <Tag key={t}>{t}</Tag>)}
        </Descriptions.Item>
        <Descriptions.Item label="并发占用">
          {agent?.runtime
            ? `${agent.runtime.active_tasks} / ${agent.runtime.max_concurrent}`
            : `0 / ${agent?.max_concurrent || 1}`}
        </Descriptions.Item>
      </Descriptions>

      <Tabs items={[
        {
          key: 'config',
          label: '配置（YAML）',
          children: (
            <>
              <YamlEditor value={yaml} onChange={setYaml} height="400px" />
              <Button type="primary" icon={<SaveOutlined />} onClick={handleSaveConfig} style={{ marginTop: 12 }}>
                保存配置
              </Button>
            </>
          ),
        },
        {
          key: 'model',
          label: '模型',
          children: (
            <Form form={modelForm} initialValues={agent?.model} layout="vertical" style={{ maxWidth: 680 }}>
              <Form.Item name="provider" label="服务商" rules={[{ required: true }]}>
                <Select options={providers.map((provider) => ({ value: provider.id, label: provider.label }))} />
              </Form.Item>
              <Form.Item name="name" label="模型" rules={[{ required: true }]}><Input placeholder="qwen-plus / MiniMax-M2.5 / gpt-4o" /></Form.Item>
              <Form.Item name="credential_id" label="凭据">
                <Select allowClear placeholder="使用下方环境变量" options={credentials.map((credential) => ({
                  value: credential.id,
                  label: `${credential.name} (${credential.masked_value || '已隐藏'})`,
                }))} />
              </Form.Item>
              <Form.Item name="api_key_env" label="备用环境变量"><Input placeholder="DASHSCOPE_API_KEY" /></Form.Item>
              <Form.Item name="api_base" label="自定义 API 地址"><Input placeholder="可选的 OpenAI 兼容端点" /></Form.Item>
              <Form.Item name="temperature" label="温度"><InputNumber min={0} max={2} step={0.1} style={{ width: 160 }} /></Form.Item>
              <Form.Item name="max_tokens" label="最大输出 Token"><InputNumber min={1} max={100000} style={{ width: 160 }} /></Form.Item>
              <Button type="primary" icon={<SaveOutlined />} onClick={handleSaveModel}>应用模型设置</Button>
            </Form>
          ),
        },
        {
          key: 'concurrency',
          label: '并发控制',
          children: (
            <Form
              form={concurrencyForm}
              layout="vertical"
              style={{ maxWidth: 680 }}
            >
              <Alert
                type="info"
                showIcon
                message="不同工作流可以并行使用同一个 Agent；同一工作流内仍保持串行。"
                description="涉及同一仓库、同一输出文件或其他共享资源的 Agent 建议保持为 1。"
                style={{ marginBottom: 16 }}
              />
              <Form.Item
                name="max_concurrent"
                label="最大并发任务数"
                rules={[{ required: true }]}
              >
                <InputNumber min={1} max={128} precision={0} style={{ width: 180 }} />
              </Form.Item>
              <Typography.Paragraph type="secondary">
                当前执行 {agent?.runtime?.active_tasks || 0} 个，等待调度 {agent?.runtime?.queued_tasks || 0} 个。
              </Typography.Paragraph>
              <Button type="primary" icon={<SaveOutlined />} onClick={handleSaveConcurrency}>
                应用并发设置
              </Button>
            </Form>
          ),
        },
        {
          key: 'capabilities',
          label: '工具与技能',
          children: (
            <Form form={capabilitiesForm} layout="vertical" style={{ maxWidth: 720 }}>
              <Form.Item name="tools" label="工具">
                <Select
                  mode="multiple"
                  options={availableTools.map((tool) => ({ value: tool.id, label: `${tool.id} - ${tool.description}` }))}
                />
              </Form.Item>
              <Form.Item name="skills" label="技能">
                <Select
                  mode="multiple"
                  options={availableSkills.map((skill) => ({
                    value: skill.id,
                    label: `${skill.id}（${skill.file_count || 1} 个文件${skill.has_scripts ? '，含脚本' : ''}${skill.has_references ? '，含参考资料' : ''}${skill.has_assets ? '，含资源' : ''}）`,
                  }))}
                />
              </Form.Item>
              <Button type="primary" icon={<SaveOutlined />} onClick={handleSaveCapabilities}>
                保存工具与技能
              </Button>
            </Form>
          ),
        },
        ...['soul.md', 'user.md', 'workflow.md'].map(f => ({
          key: f,
          label: f,
          children: (
            <>
              <Editor
                height="300px"
                language="markdown"
                theme="vs-dark"
                value={persona[f] || ''}
                onChange={(v) => setPersona(p => ({ ...p, [f]: v || '' }))}
                options={{ minimap: { enabled: false }, fontSize: 13, wordWrap: 'on' }}
              />
              <Button type="primary" icon={<SaveOutlined />} onClick={() => handleSavePersona(f)} style={{ marginTop: 12 }}>
                保存 {f}
              </Button>
            </>
          ),
        })),
      ]} />
    </>
  );
}
