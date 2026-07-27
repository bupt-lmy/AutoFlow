import { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Tree,
  Typography,
  message,
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  FileAddOutlined,
  FolderOpenOutlined,
  ImportOutlined,
  PlusOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import { fetchApi } from '../api/client';

interface SkillFile {
  path: string;
  size: number;
  kind: 'entry' | 'scripts' | 'references' | 'assets' | 'file';
  binary: boolean;
}

interface Skill {
  id: string;
  title: string;
  description: string;
  content: string;
  entrypoint: string;
  files: SkillFile[];
  file_count: number;
  total_size: number;
  components: string[];
  has_scripts: boolean;
  has_references: boolean;
  has_assets: boolean;
}

interface SkillFileContent {
  path: string;
  size: number;
  binary: boolean;
  content: string | null;
}

interface SkillTreeNode {
  title: string;
  key: string;
  isLeaf?: boolean;
  children?: SkillTreeNode[];
}

interface SelectedFolderFile {
  file: File;
  relativePath: string;
}

const formatBytes = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

const encodeFilePath = (path: string) => path.split('/').map(encodeURIComponent).join('/');

const slugify = (value: string) => value
  .toLowerCase()
  .replace(/[^a-z0-9]+/g, '-')
  .replace(/^-+|-+$/g, '')
  .slice(0, 64);

const buildTree = (files: SkillFile[]): SkillTreeNode[] => {
  const roots: SkillTreeNode[] = [];
  files.forEach((file) => {
    const parts = file.path.split('/');
    let level = roots;
    let key = '';
    parts.forEach((part, index) => {
      key = key ? `${key}/${part}` : part;
      let node = level.find((candidate) => candidate.key === key);
      if (!node) {
        node = {
          title: part,
          key,
          isLeaf: index === parts.length - 1,
          children: index === parts.length - 1 ? undefined : [],
        };
        level.push(node);
      }
      if (node.children) level = node.children;
    });
  });
  return roots;
};

const readAsBase64 = (file: File) => new Promise<string>((resolve, reject) => {
  const reader = new FileReader();
  reader.onerror = () => reject(reader.error || new Error(`无法读取 ${file.name}`));
  reader.onload = () => {
    const result = String(reader.result || '');
    resolve(result.includes(',') ? result.slice(result.indexOf(',') + 1) : result);
  };
  reader.readAsDataURL(file);
});

export default function Skills() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [manageSkill, setManageSkill] = useState<Skill | null>(null);
  const [selectedFile, setSelectedFile] = useState<SkillFileContent | null>(null);
  const [fileContent, setFileContent] = useState('');
  const [fileLoading, setFileLoading] = useState(false);
  const [fileSaving, setFileSaving] = useState(false);
  const [newFileOpen, setNewFileOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importFiles, setImportFiles] = useState<SelectedFolderFile[]>([]);
  const [importFolderName, setImportFolderName] = useState('');
  const [importReplaceId, setImportReplaceId] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [createForm] = Form.useForm<{ id: string; content: string }>();
  const [importForm] = Form.useForm<{ id: string; overwrite: boolean }>();
  const [newFileForm] = Form.useForm<{ path: string; content: string }>();
  const folderInputRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    const result = await fetchApi<Skill[]>('/api/skills');
    setSkills(result);
    return result;
  };

  useEffect(() => {
    load().catch((error: Error) => message.error(error.message)).finally(() => setLoading(false));
  }, []);

  const refreshManagedSkill = async (skillId: string) => {
    const updated = await fetchApi<Skill>(`/api/skills/${skillId}`);
    setManageSkill(updated);
    setSkills((current) => current.map((skill) => skill.id === updated.id ? updated : skill));
    return updated;
  };

  const selectFile = async (skill: Skill, path: string) => {
    setFileLoading(true);
    try {
      const result = await fetchApi<SkillFileContent>(
        `/api/skills/${skill.id}/files/${encodeFilePath(path)}`,
      );
      setSelectedFile(result);
      setFileContent(result.content || '');
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setFileLoading(false);
    }
  };

  const openManage = (skill: Skill) => {
    setManageSkill(skill);
    setSelectedFile(null);
    void selectFile(skill, skill.entrypoint);
  };

  const openCreate = () => {
    createForm.setFieldsValue({
      id: '',
      content: '---\nname: 新技能\ndescription: 说明 Agent 应在什么场景、为什么使用此技能。\n---\n\n# 工作流程\n\n1. 定义可复用的执行步骤。\n2. 按需引用脚本、参考资料或资源文件。\n',
    });
    setCreateOpen(true);
  };

  const createSkill = async () => {
    const values = await createForm.validateFields();
    const created = await fetchApi<Skill>(`/api/skills/${values.id}`, {
      method: 'POST',
      body: JSON.stringify({ content: values.content }),
    });
    setCreateOpen(false);
    await load();
    openManage(created);
    message.success('技能包已创建');
  };

  const saveFile = async () => {
    if (!manageSkill || !selectedFile || selectedFile.binary || selectedFile.content === null) return;
    setFileSaving(true);
    try {
      const updated = await fetchApi<Skill>(
        `/api/skills/${manageSkill.id}/files/${encodeFilePath(selectedFile.path)}`,
        { method: 'PUT', body: JSON.stringify({ content: fileContent }) },
      );
      setManageSkill(updated);
      setSkills((current) => current.map((skill) => skill.id === updated.id ? updated : skill));
      setSelectedFile({ ...selectedFile, content: fileContent, size: new Blob([fileContent]).size });
      message.success(`${selectedFile.path} 已保存`);
    } finally {
      setFileSaving(false);
    }
  };

  const createTextFile = async () => {
    if (!manageSkill) return;
    const values = await newFileForm.validateFields();
    const updated = await fetchApi<Skill>(
      `/api/skills/${manageSkill.id}/files/${encodeFilePath(values.path)}`,
      { method: 'PUT', body: JSON.stringify({ content: values.content }) },
    );
    setNewFileOpen(false);
    setManageSkill(updated);
    setSkills((current) => current.map((skill) => skill.id === updated.id ? updated : skill));
    await selectFile(updated, values.path);
    message.success(`${values.path} 已创建`);
  };

  const deleteFile = async () => {
    if (!manageSkill || !selectedFile || selectedFile.path === 'SKILL.md') return;
    await fetchApi(`/api/skills/${manageSkill.id}/files/${encodeFilePath(selectedFile.path)}`, {
      method: 'DELETE',
    });
    const updated = await refreshManagedSkill(manageSkill.id);
    await selectFile(updated, updated.entrypoint);
    message.success(`${selectedFile.path} 已删除`);
  };

  const remove = async (skill: Skill) => {
    await fetchApi(`/api/skills/${skill.id}`, { method: 'DELETE' });
    await load();
    if (manageSkill?.id === skill.id) setManageSkill(null);
    message.success('技能包已删除');
  };

  const chooseFolder = (replaceId: string | null = null) => {
    const input = folderInputRef.current;
    if (!input) return;
    setImportReplaceId(replaceId);
    input.value = '';
    input.setAttribute('webkitdirectory', '');
    input.setAttribute('directory', '');
    input.click();
  };

  const onFolderSelected = (files: FileList | null) => {
    const selected = Array.from(files || []).filter((file) => {
      const relative = file.webkitRelativePath || file.name;
      return !relative.split('/').some((part) => part === '.DS_Store' || part === '__MACOSX');
    });
    if (!selected.length) return;
    const rootNames = new Set(selected.map((file) => {
      const relative = file.webkitRelativePath || file.name;
      return relative.includes('/') ? relative.split('/')[0] : '';
    }));
    if (rootNames.size > 1) {
      message.error('请选择一个完整的技能文件夹');
      return;
    }
    const rootName = Array.from(rootNames)[0] || 'imported-skill';
    const mapped = selected.map((file) => {
      const relative = file.webkitRelativePath || file.name;
      const parts = relative.split('/');
      return { file, relativePath: parts.length > 1 ? parts.slice(1).join('/') : relative };
    });
    if (!mapped.some((item) => item.relativePath.toLowerCase() === 'skill.md')) {
      message.error('所选文件夹根目录必须包含 SKILL.md');
      return;
    }
    const id = importReplaceId || slugify(rootName) || 'imported-skill';
    setImportFolderName(rootName);
    setImportFiles(mapped);
    importForm.setFieldsValue({ id, overwrite: Boolean(importReplaceId) });
    setImportOpen(true);
  };

  const importFolder = async () => {
    const values = await importForm.validateFields();
    const totalSize = importFiles.reduce((total, item) => total + item.file.size, 0);
    if (importFiles.length > 500 || totalSize > 20 * 1024 * 1024) {
      message.error('技能文件夹最多包含 500 个文件，总大小不得超过 20 MB');
      return;
    }
    setImporting(true);
    try {
      const files = await Promise.all(importFiles.map(async (item) => ({
        path: item.relativePath,
        content_base64: await readAsBase64(item.file),
      })));
      const imported = await fetchApi<Skill>('/api/skills/import', {
        method: 'POST',
        body: JSON.stringify({ id: values.id, overwrite: values.overwrite, files }),
      });
      setImportOpen(false);
      await load();
      openManage(imported);
      message.success(`已从 ${importFolderName} 导入 ${imported.file_count} 个文件`);
    } finally {
      setImporting(false);
    }
  };

  if (loading) return <Spin size="large" style={{ display: 'block', margin: '100px auto' }} />;

  const treeData = manageSkill ? buildTree(manageSkill.files) : [];
  const importSize = importFiles.reduce((total, item) => total + item.file.size, 0);

  return (
    <>
      <input
        ref={(node) => {
          folderInputRef.current = node;
          if (node) node.webkitdirectory = true;
        }}
        type="file"
        multiple
        hidden
        aria-label="技能文件夹"
        onChange={(event) => onFolderSelected(event.target.files)}
      />
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>技能</Typography.Title>
          <Typography.Text type="secondary">
            可复用的技能包，以 SKILL.md 为入口，可按需包含脚本、参考资料和资源文件。
          </Typography.Text>
        </div>
        <Space>
          <Button icon={<ImportOutlined />} onClick={() => chooseFolder()}>导入文件夹</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建技能</Button>
        </Space>
      </Space>
      <Table<Skill>
        dataSource={skills}
        rowKey="id"
        pagination={false}
        columns={[
          {
            title: '技能',
            key: 'skill',
            render: (_, skill) => <Space direction="vertical" size={0}>
              <Typography.Text strong>{skill.title}</Typography.Text>
              <Typography.Text type="secondary">{skill.id}</Typography.Text>
            </Space>,
          },
          {
            title: '能力说明',
            dataIndex: 'description',
            render: (description: string) => <Typography.Paragraph ellipsis={{ rows: 2 }} style={{ margin: 0, maxWidth: 520 }}>
              {description || 'SKILL.md 中暂无说明'}
            </Typography.Paragraph>,
          },
          {
            title: '组成',
            key: 'package',
            render: (_, skill) => <Space wrap>
              <Tag color="purple">SKILL.md</Tag>
              {skill.has_scripts && <Tag color="blue">scripts</Tag>}
              {skill.has_references && <Tag color="cyan">references</Tag>}
              {skill.has_assets && <Tag color="gold">assets</Tag>}
              {skill.components.filter((item) => !['scripts', 'references', 'assets'].includes(item)).map((item) => <Tag key={item}>{item}</Tag>)}
            </Space>,
          },
          { title: '文件数', dataIndex: 'file_count', width: 80 },
          { title: '大小', dataIndex: 'total_size', width: 100, render: formatBytes },
          {
            title: '',
            key: 'actions',
            width: 150,
            render: (_, skill) => <Space size={0}>
              <Button type="text" icon={<EditOutlined />} onClick={() => openManage(skill)}>管理</Button>
              <Popconfirm title="确定删除整个技能包吗？" onConfirm={() => remove(skill)}>
                <Button danger type="text" icon={<DeleteOutlined />} aria-label={`删除 ${skill.id}`} />
              </Popconfirm>
            </Space>,
          },
        ]}
      />

      <Modal title="新建技能包" open={createOpen} onCancel={() => setCreateOpen(false)} onOk={createSkill} okText="创建" cancelText="取消">
        <Form form={createForm} layout="vertical">
          <Form.Item name="id" label="技能 ID" rules={[{ required: true }, { pattern: /^[a-z][a-z0-9-]{2,63}$/ }]}>
            <Input placeholder="release-checklist" />
          </Form.Item>
          <Form.Item name="content" label="SKILL.md" rules={[{ required: true }]}>
            <Input.TextArea rows={16} spellCheck={false} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="导入本地技能文件夹"
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        onOk={importFolder}
        okText="导入"
        cancelText="取消"
        confirmLoading={importing}
      >
        <Alert
          type="info"
          showIcon
          message={`${importFolderName}：${importFiles.length} 个文件，${formatBytes(importSize)}`}
          description="文件夹层级和二进制资源会完整保留；所选文件夹根目录必须包含 SKILL.md。"
          style={{ marginBottom: 16 }}
        />
        <Form form={importForm} layout="vertical">
          <Form.Item name="id" label="技能 ID" rules={[{ required: true }, { pattern: /^[a-z][a-z0-9-]{2,63}$/ }]}>
            <Input />
          </Form.Item>
          <Form.Item name="overwrite" valuePropName="checked">
            <Checkbox>如果技能 ID 已存在，则替换整个技能包</Checkbox>
          </Form.Item>
        </Form>
        <Typography.Text type="secondary">文件</Typography.Text>
        <div style={{ maxHeight: 180, overflow: 'auto', marginTop: 8, padding: 8, background: '#fafafa', borderRadius: 6 }}>
          {importFiles.map((item) => <div key={item.relativePath}>
            <Typography.Text code>{item.relativePath}</Typography.Text>{' '}
            <Typography.Text type="secondary">{formatBytes(item.file.size)}</Typography.Text>
          </div>)}
        </div>
      </Modal>

      <Drawer
        title={manageSkill ? `${manageSkill.title} · ${manageSkill.id}` : '技能包'}
        open={Boolean(manageSkill)}
        onClose={() => setManageSkill(null)}
        width="86vw"
        extra={<Space>
          <Button icon={<FileAddOutlined />} onClick={() => {
            newFileForm.setFieldsValue({ path: 'references/new-file.md', content: '# New file\n' });
            setNewFileOpen(true);
          }}>新建文本文件</Button>
          <Button icon={<FolderOpenOutlined />} onClick={() => chooseFolder(manageSkill?.id || null)}>从文件夹替换</Button>
        </Space>}
      >
        {manageSkill && <>
          <Descriptions size="small" column={4} style={{ marginBottom: 16 }}>
            <Descriptions.Item label="入口文件">{manageSkill.entrypoint}</Descriptions.Item>
            <Descriptions.Item label="文件数">{manageSkill.file_count}</Descriptions.Item>
            <Descriptions.Item label="技能包大小">{formatBytes(manageSkill.total_size)}</Descriptions.Item>
            <Descriptions.Item label="组成">{manageSkill.components.join(', ') || '仅入口文件'}</Descriptions.Item>
          </Descriptions>
          <Row gutter={16} style={{ minHeight: 560 }}>
            <Col span={6} style={{ borderRight: '1px solid #f0f0f0' }}>
              <Typography.Title level={5}>技能包文件</Typography.Title>
              <Tree
                showLine
                defaultExpandAll
                treeData={treeData}
                selectedKeys={selectedFile ? [selectedFile.path] : []}
                onSelect={(keys) => {
                  const path = String(keys[0] || '');
                  if (manageSkill.files.some((file) => file.path === path)) void selectFile(manageSkill, path);
                }}
              />
            </Col>
            <Col span={18}>
              {fileLoading ? <Spin /> : selectedFile ? <>
                <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 12 }}>
                  <Space>
                    <Typography.Title level={5} style={{ margin: 0 }}>{selectedFile.path}</Typography.Title>
                    <Tag>{formatBytes(selectedFile.size)}</Tag>
                    {selectedFile.binary && <Tag color="gold">二进制资源</Tag>}
                  </Space>
                  <Space>
                    {selectedFile.path !== 'SKILL.md' && <Popconfirm title={`确定删除 ${selectedFile.path} 吗？`} onConfirm={deleteFile}>
                      <Button danger icon={<DeleteOutlined />}>删除</Button>
                    </Popconfirm>}
                    {!selectedFile.binary && selectedFile.content !== null && <Button type="primary" icon={<SaveOutlined />} loading={fileSaving} onClick={saveFile}>保存文件</Button>}
                  </Space>
                </Space>
                {selectedFile.binary || selectedFile.content === null
                  ? <Empty description="二进制文件或超大文件会被保留，但不能在文本编辑器中修改。" />
                  : <Input.TextArea
                    value={fileContent}
                    onChange={(event) => setFileContent(event.target.value)}
                    rows={28}
                    spellCheck={false}
                    style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}
                  />}
              </> : <Empty description="请选择一个技能包文件" />}
            </Col>
          </Row>
        </>}
      </Drawer>

      <Modal title="新建技能文本文件" open={newFileOpen} onCancel={() => setNewFileOpen(false)} onOk={createTextFile} okText="创建文件" cancelText="取消">
        <Form form={newFileForm} layout="vertical">
          <Form.Item
            name="path"
            label="技能包内相对路径"
            rules={[
              { required: true },
              { pattern: /^(?!\/)(?!.*(?:^|\/)\.\.(?:\/|$))[A-Za-z0-9._/-]+$/, message: '请输入技能包内部的安全相对路径。' },
            ]}
          >
            <Input placeholder="references/guide.md" />
          </Form.Item>
          <Form.Item name="content" label="内容">
            <Input.TextArea rows={14} spellCheck={false} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
