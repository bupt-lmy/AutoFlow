import { useState } from 'react';
import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import { Layout, Menu, Typography } from 'antd';
import {
  DashboardOutlined,
  ApartmentOutlined,
  RobotOutlined,
  ReadOutlined,
  FileTextOutlined,
  LineChartOutlined,
  SettingOutlined,
} from '@ant-design/icons';

const { Sider, Content } = Layout;

const menuItems = [
  { key: '/', icon: <DashboardOutlined />, label: '概览' },
  { key: '/workflows', icon: <ApartmentOutlined />, label: '工作流' },
  { key: '/agents', icon: <RobotOutlined />, label: 'Agent' },
  { key: '/skills', icon: <ReadOutlined />, label: '技能' },
  { key: '/logs', icon: <FileTextOutlined />, label: '执行日志' },
  { key: '/observability', icon: <LineChartOutlined />, label: '模型追踪' },
  { key: '/settings', icon: <SettingOutlined />, label: '系统设置' },
];

export default function MainLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [isCompact, setIsCompact] = useState(false);

  const selectedKey = menuItems.reduce((best, item) => {
    if (location.pathname.startsWith(item.key) && item.key.length > best.length) return item.key;
    return best;
  }, '/');

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        width={220}
        theme="dark"
        breakpoint="lg"
        collapsedWidth={0}
        onBreakpoint={setIsCompact}
        style={{ position: 'fixed', left: 0, top: 0, bottom: 0, zIndex: 10 }}
      >
        <div style={{ padding: '20px 24px', display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 20 }}>⚡</span>
          <Typography.Title level={4} style={{ margin: 0, color: '#fff' }}>AxonFlow</Typography.Title>
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout style={{ marginLeft: isCompact ? 0 : 220, minWidth: 0 }}>
        <Content style={{ padding: isCompact ? 16 : 24, background: '#f0f2f5', minHeight: '100vh', minWidth: 0 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
