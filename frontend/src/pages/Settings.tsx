import { useEffect, useState } from 'react';
import {
  Alert,
  AutoComplete,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Switch,
  Table,
  Tabs,
  Typography,
  message,
} from 'antd';
import { DeleteOutlined, EditOutlined, PlusOutlined, SaveOutlined } from '@ant-design/icons';
import YamlEditor from '../components/YamlEditor';
import { fetchApi } from '../api/client';

interface Credential {
  id: string;
  name: string;
  provider: string;
  source: 'encrypted' | 'environment';
  env_var?: string;
  masked_value?: string;
}

interface Provider {
  id: string;
  label: string;
  default_key_env?: string;
  common_models?: string[];
}

interface ModelProfile {
  id: string;
  name: string;
  config: {
    provider: string;
    name: string;
    temperature: number;
    max_tokens: number;
    timeout: number;
    api_base?: string;
    api_key_env?: string;
    credential_id?: string;
  };
  api_key_env_invalid?: boolean;
}

interface ObservabilitySettings {
  langsmith_enabled: boolean;
  langsmith_project: string;
  langsmith_endpoint?: string;
  langsmith_credential_id?: string;
  content_policy: 'metadata_only' | 'masked_content' | 'full_content';
}

export default function Settings() {
  const [yaml, setYaml] = useState('');
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [observability, setObservability] = useState<ObservabilitySettings | null>(null);
  const [credentialModalOpen, setCredentialModalOpen] = useState(false);
  const [profileModalOpen, setProfileModalOpen] = useState(false);
  const [editingCredential, setEditingCredential] = useState<Credential | null>(null);
  const [editingProfile, setEditingProfile] = useState<ModelProfile | null>(null);
  const [credentialForm] = Form.useForm();
  const [profileForm] = Form.useForm();
  const [observabilityForm] = Form.useForm<ObservabilitySettings>();
  const selectedProfileProvider = Form.useWatch(['config', 'provider'], profileForm);
  const suggestedModels = providers.find((provider) => provider.id === selectedProfileProvider)?.common_models || [];

  const load = async () => {
    const [config, credentialData, providerData, profileData, observabilityData] = await Promise.all([
      fetchApi<{ raw_yaml?: string }>('/api/config'),
      fetchApi<Credential[]>('/api/credentials'),
      fetchApi<Provider[]>('/api/credentials/catalog/providers'),
      fetchApi<ModelProfile[]>('/api/model-profiles'),
      fetchApi<ObservabilitySettings>('/api/observability/settings'),
    ]);
    setYaml(config.raw_yaml || '');
    setCredentials(credentialData);
    setProviders(providerData);
    setModelProfiles(profileData);
    setObservability(observabilityData);
  };

  useEffect(() => {
    // Settings are loaded from the backend once when this page is mounted.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load().catch((error: Error) => message.error(error.message));
  }, []);

  const saveYaml = async () => {
    await fetchApi('/api/config', { method: 'PUT', body: JSON.stringify({ yaml_content: yaml }) });
    message.success('全局配置已保存');
  };

  const openCredentialModal = (credential?: Credential) => {
    setEditingCredential(credential || null);
    credentialForm.resetFields();
    credentialForm.setFieldsValue(credential ? {
      name: credential.name,
      provider: credential.provider,
      source: credential.source,
      env_var: credential.env_var,
      secret: '',
    } : { source: 'encrypted' });
    setCredentialModalOpen(true);
  };

  const saveCredential = async () => {
    const values = await credentialForm.validateFields();
    await fetchApi(
      editingCredential ? `/api/credentials/${editingCredential.id}` : '/api/credentials',
      { method: editingCredential ? 'PUT' : 'POST', body: JSON.stringify(values) },
    );
    setCredentialModalOpen(false);
    setEditingCredential(null);
    credentialForm.resetFields();
    await load();
    message.success(editingCredential ? '凭据已更新' : '凭据已加密保存');
  };

  const deleteCredential = async (id: string) => {
    await fetchApi(`/api/credentials/${id}`, { method: 'DELETE' });
    await load();
    message.success('凭据已删除');
  };

  const openProfileModal = (profile?: ModelProfile) => {
    setEditingProfile(profile || null);
    profileForm.resetFields();
    profileForm.setFieldsValue(profile ? {
      name: profile.name,
      config: { ...profile.config },
    } : {
      name: '',
      config: {
        provider: 'openai',
        name: '',
        temperature: 0.7,
        max_tokens: 4096,
        timeout: 60,
      },
    });
    setProfileModalOpen(true);
  };

  const saveModelProfile = async () => {
    const values = await profileForm.validateFields();
    await fetchApi(
      editingProfile ? `/api/model-profiles/${editingProfile.id}` : '/api/model-profiles',
      { method: editingProfile ? 'PUT' : 'POST', body: JSON.stringify(values) },
    );
    setProfileModalOpen(false);
    setEditingProfile(null);
    profileForm.resetFields();
    await load();
    message.success(editingProfile ? '模型配置已更新' : '模型配置已保存');
  };

  const deleteModelProfile = async (id: string) => {
    await fetchApi(`/api/model-profiles/${id}`, { method: 'DELETE' });
    await load();
    message.success('模型配置已删除');
  };

  const saveObservability = async () => {
    const values = await observabilityForm.validateFields();
    const result = await fetchApi<ObservabilitySettings>('/api/observability/settings', {
      method: 'PUT', body: JSON.stringify(values),
    });
    setObservability(result);
    message.success('可观测性设置已保存');
  };

  return (
    <>
      <Typography.Title level={3}>系统设置</Typography.Title>
      <Tabs items={[
        {
          key: 'credentials',
          label: '凭据管理',
          children: <>
            <Alert
              type="info"
              showIcon
              message="密钥会在服务端加密保存，且不会返回到浏览器。"
              style={{ marginBottom: 16 }}
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openCredentialModal()} style={{ marginBottom: 16 }}>
              添加凭据
            </Button>
            <Table<Credential>
              dataSource={credentials}
              rowKey="id"
              pagination={false}
              columns={[
                { title: '名称', dataIndex: 'name' },
                { title: '服务商', dataIndex: 'provider' },
                { title: '来源', dataIndex: 'source', render: (source: string) => source === 'environment' ? '环境变量' : '加密存储' },
                { title: '值', dataIndex: 'masked_value' },
                {
                  title: '', key: 'actions', width: 70,
                  render: (_, credential) => <>
                    <Button type="text" icon={<EditOutlined />} aria-label="编辑凭据" onClick={() => openCredentialModal(credential)} />
                    <Popconfirm title="确定删除此凭据吗？" onConfirm={() => deleteCredential(credential.id)}>
                      <Button danger type="text" icon={<DeleteOutlined />} aria-label="删除凭据" />
                    </Popconfirm>
                  </>,
                },
              ]}
            />
          </>,
        },
        {
          key: 'observability',
          label: '可观测性',
          children: <Form form={observabilityForm} layout="vertical" initialValues={observability || undefined} style={{ maxWidth: 680 }}>
            <Form.Item name="langsmith_enabled" label="启用 LangSmith" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="langsmith_project" label="项目名称" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <Form.Item name="langsmith_credential_id" label="LangSmith 凭据">
              <Select allowClear options={credentials.map((credential) => ({ value: credential.id, label: `${credential.name} (${credential.masked_value})` }))} />
            </Form.Item>
            <Form.Item name="langsmith_endpoint" label="服务地址">
              <Input placeholder="https://api.smith.langchain.com" />
            </Form.Item>
            <Form.Item name="content_policy" label="追踪内容保留策略" rules={[{ required: true }]}>
              <Select options={[
                { value: 'metadata_only', label: '仅元数据' },
                { value: 'masked_content', label: '脱敏内容（推荐）' },
                { value: 'full_content', label: '完整内容' },
              ]} />
            </Form.Item>
            <Button type="primary" icon={<SaveOutlined />} onClick={saveObservability}>保存可观测性设置</Button>
          </Form>,
        },
        {
          key: 'profiles',
          label: '模型配置',
          children: <>
            <Alert
              type="info"
              showIcon
              message="模型配置组合了服务商、模型、请求限制和凭据；创建 Agent 时可直接选择。"
              style={{ marginBottom: 16 }}
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openProfileModal()} style={{ marginBottom: 16 }}>
              添加模型配置
            </Button>
            <Table<ModelProfile>
              dataSource={modelProfiles}
              rowKey="id"
              pagination={false}
              columns={[
                { title: '名称', dataIndex: 'name' },
                { title: '服务商', render: (_, profile) => providers.find((provider) => provider.id === profile.config.provider)?.label || profile.config.provider },
                { title: '模型', render: (_, profile) => profile.config.name },
                {
                  title: '凭据',
                  render: (_, profile) => {
                    const credential = credentials.find((item) => item.id === profile.config.credential_id);
                    if (credential) return `${credential.name} (${credential.masked_value})`;
                    if (profile.api_key_env_invalid) return '环境变量无效';
                    return profile.config.api_key_env || '未设置';
                  },
                },
                {
                  title: '', key: 'actions', width: 70,
                  render: (_, profile) => <>
                    <Button type="text" icon={<EditOutlined />} aria-label="编辑模型配置" onClick={() => openProfileModal(profile)} />
                    <Popconfirm title="确定删除此模型配置吗？" onConfirm={() => deleteModelProfile(profile.id)}>
                      <Button danger type="text" icon={<DeleteOutlined />} aria-label="删除模型配置" />
                    </Popconfirm>
                  </>,
                },
              ]}
            />
          </>,
        },
        {
          key: 'global',
          label: '全局 YAML',
          children: <>
            <YamlEditor value={yaml} onChange={setYaml} height="520px" />
            <Button type="primary" icon={<SaveOutlined />} onClick={saveYaml} style={{ marginTop: 12 }}>保存全局配置</Button>
          </>,
        },
      ]} />

      <Modal
        title={editingCredential ? '编辑凭据' : '添加凭据'}
        open={credentialModalOpen}
        onCancel={() => { setCredentialModalOpen(false); setEditingCredential(null); }}
        onOk={saveCredential}
        okText={editingCredential ? '保存修改' : '加密并保存'}
        cancelText="取消"
      >
        <Form form={credentialForm} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input placeholder="qwen-production" /></Form.Item>
          <Form.Item name="provider" label="服务商" rules={[{ required: true }]}>
            <Select options={providers.map((provider) => ({ value: provider.id, label: provider.label }))} />
          </Form.Item>
          <Form.Item name="source" label="密钥来源" rules={[{ required: true }]}>
            <Select options={[{ value: 'encrypted', label: '粘贴并加密保存' }, { value: 'environment', label: '读取环境变量' }]} />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(previous, current) => previous.source !== current.source}>
            {({ getFieldValue }) => getFieldValue('source') === 'environment'
              ? <Form.Item name="env_var" label="环境变量名" rules={[{ required: true }]}><Input placeholder="DASHSCOPE_API_KEY" /></Form.Item>
              : <Form.Item
                name="secret"
                label={editingCredential ? '新 API Key' : 'API Key'}
                extra={editingCredential?.source === 'encrypted' ? '留空可继续使用现有的加密密钥。' : undefined}
                rules={[{ required: !editingCredential || editingCredential.source !== 'encrypted' }]}
              ><Input.Password autoComplete="new-password" /></Form.Item>}
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editingProfile ? '编辑模型配置' : '添加模型配置'}
        open={profileModalOpen}
        onCancel={() => { setProfileModalOpen(false); setEditingProfile(null); }}
        onOk={saveModelProfile}
        okText={editingProfile ? '保存修改' : '保存模型配置'}
        cancelText="取消"
      >
        <Form form={profileForm} layout="vertical">
          <Form.Item name="name" label="配置名称" rules={[{ required: true }]}>
            <Input placeholder="minimax-m3-production" />
          </Form.Item>
          <Form.Item name={['config', 'provider']} label="服务商" rules={[{ required: true }]}>
            <Select
              options={providers.map((provider) => ({ value: provider.id, label: provider.label }))}
              onChange={() => profileForm.setFieldValue(['config', 'name'], '')}
            />
          </Form.Item>
          <Form.Item
            name={['config', 'name']}
            label="模型"
            extra="可选择常用模型，也可输入准确的自定义模型 ID。"
            rules={[{ required: true }]}
          >
            <AutoComplete
              options={suggestedModels.map((model) => ({ value: model }))}
              placeholder={suggestedModels[0] || '输入模型 ID'}
              filterOption={(inputValue, option) => (
                String(option?.value || '').toLocaleLowerCase().includes(inputValue.toLocaleLowerCase())
              )}
            />
          </Form.Item>
          <Form.Item name={['config', 'credential_id']} label="凭据">
            <Select allowClear options={credentials.map((credential) => ({ value: credential.id, label: `${credential.name} (${credential.masked_value})` }))} />
          </Form.Item>
          <Form.Item
            name={['config', 'api_key_env']}
            label="备用环境变量名"
            rules={[{ pattern: /^[A-Za-z_][A-Za-z0-9_]*$/, message: '请输入 MINIMAX_API_KEY 这类变量名，不要直接填写密钥。' }]}
          >
            <Input placeholder="MINIMAX_API_KEY" />
          </Form.Item>
          <Form.Item name={['config', 'api_base']} label="API 基础地址">
            <Input placeholder="https://api.minimaxi.com/v1" />
          </Form.Item>
          <Form.Item name={['config', 'temperature']} label="温度" rules={[{ required: true }]}>
            <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name={['config', 'max_tokens']} label="最大输出令牌数" rules={[{ required: true }]}>
            <InputNumber min={1} max={1000000} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name={['config', 'timeout']} label="请求超时（秒）" rules={[{ required: true }]}>
            <InputNumber min={1} max={3600} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
