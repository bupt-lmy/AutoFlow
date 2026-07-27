import type { PlatformNode } from './WorkflowBuilder';

export function validateWorkflowNodes(nodes: PlatformNode[]): string | null {
  const invalidDiscovery = nodes.find((node) => {
    if (node.node_type !== 'discovery') return false;
    const discovery = node.config.discovery as Record<string, unknown> | undefined;
    return typeof discovery?.description !== 'string' || !discovery.description.trim();
  });
  return invalidDiscovery
    ? `动态 Agent“${invalidDiscovery.label}”需要填写能力说明。`
    : null;
}

export function validateSupervisorConfiguration(
  mode: string,
  supervisor: Record<string, unknown> | null | undefined,
  nodes: PlatformNode[],
): string | null {
  if (mode !== 'supervisor') return null;
  if (!supervisor) return 'Supervisor 模式需要配置 Supervisor。';
  const supervisorNodeId = supervisor.agent_id;
  if (typeof supervisorNodeId !== 'string' || !supervisorNodeId) {
    return '请选择 Supervisor 节点。';
  }
  const supervisorNode = nodes.find((node) => node.id === supervisorNodeId);
  if (!supervisorNode || supervisorNode.node_type === 'discovery') {
    return 'Supervisor 必须引用当前工作流中的具体 Agent 节点。';
  }
  if (typeof supervisor.responsibility !== 'string' || !supervisor.responsibility.trim()) {
    return '请说明 Supervisor 在当前工作流中的职责。';
  }
  if (!Array.isArray(supervisor.capabilities) || supervisor.capabilities.length === 0) {
    return '请至少添加一项 Supervisor 能力。';
  }
  if (
    typeof supervisor.max_attempts_per_agent !== 'number'
    || supervisor.max_attempts_per_agent < 1
  ) {
    return '单 Agent 最大执行次数必须大于 0。';
  }
  if (
    typeof supervisor.max_repeated_decisions !== 'number'
    || supervisor.max_repeated_decisions < 1
  ) {
    return '相同决策最大连续次数必须大于 0。';
  }
  return null;
}
