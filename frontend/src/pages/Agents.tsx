import { useEffect, useState } from 'react';
import { Alert, Button, Form, Input, InputNumber, Modal, Select, Space, Spin, Table, Tag, Typography, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { fetchApi } from '../api/client';

interface Agent {
  id: string;
  name: string;
  role: string;
  tools?: string[];
  model?: { provider?: string; name?: string };
  model_profile_id?: string;
  agent_type?: 'base' | 'remote' | 'codex';
  max_concurrent?: number;
  runtime?: { active_tasks: number; max_concurrent: number } | null;
}

interface ModelProfile {
  id: string;
  name: string;
  config: { provider: string; name: string };
}

interface Credential { id: string; name: string; masked_value?: string; }

export default function Agents() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  const navigate = useNavigate();

  const load = async () => {
    const [agentData, profileData, credentialData] = await Promise.all([
      fetchApi<Agent[]>('/api/agents'),
      fetchApi<ModelProfile[]>('/api/model-profiles'),
      fetchApi<Credential[]>('/api/credentials'),
    ]);
    setAgents(agentData);
    setProfiles(profileData);
    setCredentials(credentialData);
  };

  useEffect(() => {
    // Initial data loading intentionally updates page state after the request resolves.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load()
      .catch((error: Error) => message.error(error.message))
      .finally(() => setLoading(false));
  }, []);

  const openCreate = () => {
    createForm.resetFields();
    setCreateOpen(true);
  };

  const createAgent = async () => {
    const values = await createForm.validateFields();
    const agent = await fetchApi<Agent>('/api/agents', {
      method: 'POST',
      body: JSON.stringify(values),
    });
    setCreateOpen(false);
    await load();
    message.success('Agent 已创建并启动');
    navigate(`/agents/${agent.id}`);
  };

  if (loading) return <Spin size="large" style={{ display: 'block', margin: '100px auto' }} />;

  return (
    <>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>Agent 管理</Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建 Agent
        </Button>
      </Space>
      {!profiles.length && <Alert type="warning" showIcon message="创建基础 Agent 前，请先在系统设置中添加模型配置。" style={{ marginBottom: 16 }} />}
      <Table<Agent>
        dataSource={agents}
        rowKey="id"
        columns={[
          { title: 'ID', dataIndex: 'id', key: 'id' },
          { title: '名称', dataIndex: 'name', key: 'name' },
          { title: '职责', dataIndex: 'role', key: 'role' },
          {
            title: '模型',
            key: 'model',
            render: (_, agent) => <Space size={4}>
              <Tag>{agent.model?.name || '默认'}</Tag>
              {agent.model_profile_id && <Tag color="blue">{profiles.find((profile) => profile.id === agent.model_profile_id)?.name || '模型配置'}</Tag>}
            </Space>,
          },
          {
            title: '工具',
            key: 'tools',
            render: (_, agent) => agent.tools?.length || 0,
          },
          {
            title: '并发',
            key: 'concurrency',
            render: (_, agent) => (
              <Tag color={agent.runtime?.active_tasks ? 'processing' : 'default'}>
                {agent.runtime?.active_tasks || 0} / {agent.runtime?.max_concurrent || agent.max_concurrent || 1}
              </Tag>
            ),
          },
          {
            title: '操作',
            key: 'actions',
            render: (_, agent) => (
              <Button size="small" onClick={() => navigate(`/agents/${agent.id}`)}>
                详情
              </Button>
            ),
          },
        ]}
      />

      <Modal title="新建 Agent" open={createOpen} onCancel={() => setCreateOpen(false)} onOk={createAgent} okText="创建 Agent" cancelText="取消">
        <Form
          form={createForm}
          layout="vertical"
          initialValues={{
            agent_type: 'base',
            codex_sandbox: 'workspace-write',
            codex_timeout_seconds: 1800,
            codex_health_check: 'exec',
            max_concurrent: 1,
          }}
        >
          <Form.Item
            name="id"
            label="Agent ID"
            rules={[
              { required: true },
              { pattern: /^[a-z][a-z0-9-]{2,63}$/, message: '请使用小写字母、数字和连字符，并以字母开头。' },
            ]}
          >
            <Input placeholder="research-writer" />
          </Form.Item>
          <Form.Item name="name" label="显示名称" rules={[{ required: true }]}>
            <Input placeholder="研究写作助手" />
          </Form.Item>
          <Form.Item name="role" label="职责">
            <Input.TextArea rows={3} placeholder="例如：生成简洁、准确的研究摘要。" />
          </Form.Item>
          <Form.Item
            name="max_concurrent"
            label="最大并发任务数"
            tooltip="不同工作流可并行调用该 Agent；涉及共享文件或代码修改时建议保持为 1。"
            rules={[{ required: true }]}
          >
            <InputNumber min={1} max={128} precision={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="agent_type" label="执行类型" rules={[{ required: true }]}>
            <Select options={[
              { value: 'base', label: '基础 Agent' },
              { value: 'codex', label: 'Codex 编码 Agent' },
              { value: 'remote', label: '远程 Agent' },
            ]} />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(previous, current) => previous.agent_type !== current.agent_type}>
            {({ getFieldValue }) => {
              const agentType = getFieldValue('agent_type');
              if (agentType === 'remote') return <>
                <Form.Item name="remote_endpoint" label="远程服务地址" rules={[{ required: true }]}>
                  <Input placeholder="https://generation.example.com/v1/jobs" />
                </Form.Item>
                <Form.Item name="remote_credential_id" label="远程服务凭据">
                  <Select allowClear options={credentials.map((credential) => ({ value: credential.id, label: `${credential.name} (${credential.masked_value || '已隐藏'})` }))} />
                </Form.Item>
                <Form.Item name="remote_api_key_env" label="备用环境变量"><Input placeholder="GENERATION_API_KEY" /></Form.Item>
              </>;
              if (agentType === 'codex') return <>
                <Alert
                  type="info"
                  showIcon
                  message="使用本机已安装并完成认证的 Codex CLI。Agent 只能编辑指定目录中的文件。"
                  style={{ marginBottom: 16 }}
                />
                <Form.Item name="codex_working_directory" label="仓库工作目录" rules={[{ required: true }]}>
                  <Input placeholder="/仓库的绝对路径" />
                </Form.Item>
                <Form.Item name="codex_model" label="Codex 模型（可选）">
                  <Input placeholder="留空使用 Codex 默认配置" />
                </Form.Item>
                <Form.Item name="codex_profile" label="Codex 配置文件（可选）">
                  <Input placeholder="CODEX_HOME 中的配置名称" />
                </Form.Item>
                <Form.Item name="codex_sandbox" label="Codex 沙箱">
                  <Select options={[
                    { value: 'workspace-write', label: '允许写入工作区' },
                    { value: 'read-only', label: '只读' },
                  ]} />
                </Form.Item>
                <Form.Item name="codex_timeout_seconds" label="任务超时（秒）">
                  <Input type="number" min={30} max={86400} />
                </Form.Item>
                <Form.Item name="codex_health_check" label="健康检查">
                  <Select options={[
                    { value: 'exec', label: '执行真实的只读 Codex 请求' },
                    { value: 'auth', label: '仅检查本地登录状态' },
                    { value: 'binary', label: '仅检查 CLI 是否存在' },
                  ]} />
                </Form.Item>
              </>;
              return <Form.Item name="model_profile_id" label="模型配置" rules={[{ required: true, message: '请选择模型配置。' }]}>
                <Select
                  placeholder="选择服务商和模型"
                  options={profiles.map((profile) => ({
                    value: profile.id,
                    label: `${profile.name} - ${profile.config.provider} / ${profile.config.name}`,
                  }))}
                />
              </Form.Item>;
            }}
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
